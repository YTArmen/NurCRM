"""
Корзина удалённого (ТЗ п.9) без новых колонок в apps/main.

record()   — до удаления: снимок строки + всего, что удалится каскадом (Collector Django),
             плюс id связанных строк, у которых ссылка обнулится (SET_NULL).
finalize() — после удаления: если товар остался (архив из-за истории продаж) — освобождаем штрихкод.
restore()  — пересоздаёт строки из снимка (или снимает архив у товара).
"""
import json
import logging
from datetime import timedelta

from django.apps import apps
from django.core import serializers
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import CASCADE, SET_NULL, Collector
from django.utils import timezone

from apps.construction.models import TrashEntry

log = logging.getLogger(__name__)

RETENTION_DAYS = 30
MAX_SNAPSHOT_OBJECTS = 5000   # больше — каскад слишком большой, снимок только самой строки
MAX_NULLED_IDS = 20000

TYPES = {
    "product": "main.Product",
    "client": "main.Client",
    "category": "main.ProductCategory",
    "brand": "main.ProductBrand",
}


def model_for(type_):
    return apps.get_model(TYPES[type_])


def purge_expired(company=None):
    qs = TrashEntry.objects.filter(deleted_at__lt=timezone.now() - timedelta(days=RETENTION_DAYS))
    if company is not None:
        qs = qs.filter(company=company)
    return qs.delete()[0]


def _object_name(obj):
    return str(getattr(obj, "name", None) or getattr(obj, "full_name", None) or obj)[:255]


def _nulled_links(obj):
    """Строки, у которых ссылка на obj станет NULL: [{model, field, ids}] — чтобы вернуть связь."""
    out = []
    for rel in obj._meta.related_objects:
        if not (rel.one_to_many or rel.one_to_one) or rel.on_delete is not SET_NULL:
            continue
        ids = list(rel.related_model._base_manager.filter(**{rel.field.name: obj.pk}).values_list("pk", flat=True)[:MAX_NULLED_IDS])
        if ids:
            out.append({"model": rel.related_model._meta.label, "field": rel.field.name, "ids": [str(i) for i in ids]})
    return out


def _snapshot(obj):
    collector = Collector(using="default")
    collector.collect([obj])
    collector.sort()
    objs = []
    for model, instances in collector.data.items():
        objs.extend(instances)
    for qs in collector.fast_deletes:
        objs.extend(list(qs[: MAX_SNAPSHOT_OBJECTS + 1]))
    if len(objs) > MAX_SNAPSHOT_OBJECTS:
        return {"truncated": True, "objects": json.loads(serializers.serialize("json", [obj]))}
    return {"truncated": False, "objects": json.loads(serializers.serialize("json", objs))}


def record(request, type_, obj):
    """Создаёт запись корзины. Вызывать внутри transaction.atomic ДО удаления."""
    payload = _snapshot(obj)
    payload["root"] = {"model": obj._meta.label, "pk": str(obj.pk)}
    payload["links"] = _nulled_links(obj)
    if type_ == "product":
        payload["product"] = {
            "status": getattr(obj, "status", None), "barcode": obj.barcode, "code": obj.code, "plu": getattr(obj, "plu", None),
        }
    user = getattr(request, "user", None)
    return TrashEntry.objects.create(
        company_id=obj.company_id, type=type_, object_id=str(obj.pk), object_name=_object_name(obj),
        payload=payload, deleted_by=user if getattr(user, "pk", None) else None,
    )


def finalize(entry, type_, obj):
    """После удаления: товар с историей остаётся архивным — штрихкод/код/PLU освобождаем."""
    if type_ != "product":
        return
    Product = model_for("product")
    if Product.objects.filter(pk=obj.pk).exists():
        Product.objects.filter(pk=obj.pk).update(barcode=None, code="", plu=None)
        entry.payload["archived"] = True
        entry.save(update_fields=["payload"])


def _free(Model, field, value, exclude_pk, company_id):
    if value in (None, ""):
        return True
    return not Model.objects.filter(**{"company_id": company_id, field: value}).exclude(pk=exclude_pk).exists()


def restore(entry):
    """Возвращает объект. Бросает ValueError с текстом для пользователя, если нельзя."""
    payload = entry.payload or {}
    Model = model_for(entry.type)
    notes = []
    with transaction.atomic():
        if payload.get("archived"):
            prod = Model.objects.filter(pk=entry.object_id, company_id=entry.company_id).first()
            if prod is None:
                raise ValueError("Товар уже удалён окончательно.")
            info = payload.get("product") or {}
            fields = {"status": info.get("status") or "accepted"}
            for f in ("barcode", "code", "plu"):
                if _free(Model, f, info.get(f), prod.pk, entry.company_id):
                    fields[f] = info.get(f) if info.get(f) is not None or f != "code" else ""
                elif info.get(f) not in (None, ""):
                    notes.append(f"{f} «{info.get(f)}» уже занят другим товаром — оставлен пустым")
            Model.objects.filter(pk=prod.pk).update(**fields)
            prod.refresh_from_db()
            entry.delete()
            return prod, notes

        if Model.objects.filter(pk=entry.object_id).exists():
            raise ValueError("Такая запись уже существует.")
        deserialized = list(serializers.deserialize("python", payload.get("objects") or []))
        root = None
        # снимок отсортирован «сначала зависимые»; возвращаем в обратном порядке (сначала главный)
        def _is_root(ds):
            return ds.object._meta.label == payload["root"]["model"] and str(ds.object.pk) == payload["root"]["pk"]

        ordered = [d for d in deserialized if _is_root(d)] + [d for d in reversed(deserialized) if not _is_root(d)]
        for ds in ordered:
            is_root = _is_root(ds)
            obj = ds.object
            if is_root:
                _null_missing_fks(obj)
                if entry.type == "product":
                    for f in ("barcode", "code", "plu"):
                        if not _free(Model, f, getattr(obj, f, None), obj.pk, entry.company_id):
                            notes.append(f"{f} «{getattr(obj, f)}» уже занят другим товаром — оставлен пустым")
                            setattr(obj, f, "" if f == "code" else None)
                    if getattr(obj, "seq", None) is not None and Model.objects.filter(company_id=entry.company_id, seq=obj.seq).exists():
                        top = Model.objects.filter(company_id=entry.company_id).aggregate(m=models.Max("seq"))["m"] or 0
                        obj.seq = top + 1
                try:
                    with transaction.atomic():
                        ds.save()
                except IntegrityError as exc:
                    raise ValueError(f"Нельзя вернуть: конфликт данных ({exc.__class__.__name__}).")
                root = obj
            else:
                if obj.__class__.objects.filter(pk=obj.pk).exists():
                    continue
                _null_missing_fks(obj)
                try:
                    with transaction.atomic():
                        ds.save()
                except IntegrityError:
                    notes.append(f"{obj._meta.verbose_name}: не удалось вернуть связанную запись")
        if root is None:
            raise ValueError("В снимке нет главной записи — вернуть нельзя.")
        for link in payload.get("links") or []:
            try:
                M = apps.get_model(link["model"])
                M._base_manager.filter(pk__in=link["ids"], **{f"{link['field']}__isnull": True}).update(**{link["field"]: root.pk})
            except Exception:
                log.warning("trash relink failed", exc_info=True)
        if entry.type in ("category", "brand"):
            Model.objects.rebuild()  # MPTT: пересчитать lft/rght после вставки
            root = Model.objects.get(pk=root.pk)
        entry.delete()
        return root, notes


def _null_missing_fks(obj):
    """Ссылки на уже отсутствующие строки (родитель-категория, бренд...) обнуляем, чтобы не упасть на FK."""
    for f in obj._meta.concrete_fields:
        if not f.is_relation or f.primary_key:
            continue
        val = getattr(obj, f.attname, None)
        if val is None:
            continue
        if not f.related_model._base_manager.filter(pk=val).exists():
            if f.null:
                setattr(obj, f.attname, None)


class TrashDestroyMixin:
    """
    Для RetrieveUpdateDestroyAPIView: DELETE кладёт снимок в корзину и пишет в журнал.
    Если само удаление упало (например ProtectedError) — запись корзины откатывается вместе с ним.
    """

    trash_type = None

    def destroy(self, request, *args, **kwargs):
        from apps.construction.audit import log_request

        obj = self.get_object()
        name, pk = _object_name(obj), obj.pk
        with transaction.atomic():
            entry = record(request, self.trash_type, obj)
            response = super().destroy(request, *args, **kwargs)
            finalize(entry, self.trash_type, obj)
        log_request(request, "delete", self.trash_type, pk, name, company=getattr(obj, "company", None))
        return response
