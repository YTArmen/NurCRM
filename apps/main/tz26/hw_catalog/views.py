import uuid

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.permissions_flags import denied, flag_permission, is_privileged
from core.exceptions import ApiError

from . import services as svc
from .models import CategoryRelation, ProductAttributeKey, ProductRelation
from .profiles import all_profiles


class SphereProfilesAPIView(APIView):
    """Б-54: GET api/public/sphere-profiles/ — каталог профилей магазина (публично)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response(all_profiles())


# ======================================================================
# Б-57. Справочник ключей атрибутов; Б-59. Правила по категориям
# ======================================================================
def _company_or_400(request):
    company = svc.company_of(request)
    if company is None:
        raise ApiError("company_required", "Компания не определена.", status_code=400)
    return company


def _owner_only(request):
    if not is_privileged(request.user):
        raise denied("owner", "Справочник ключей редактирует владелец.")


def _key_json(k):
    return {
        "key": k.key,
        "name": svc.key_names(k),
        "type": k.type,
        "unit": k.unit or None,
        "options": k.options if k.type == "enum" else None,
        "filterable": k.filterable,
    }


def _bad_key(field, msg):
    raise ApiError("invalid_attribute_key", msg, status_code=400, fields={field: [msg]})


def _parse_key_body(data, partial=False, instance=None):
    """Проверка тела справочника. Возвращает dict полей модели."""
    out = {}
    if not partial or "key" in data:
        key = data.get("key")
        if not isinstance(key, str) or not svc.KEY_RE.match(key):
            _bad_key("key", "key — латиница, цифры и «_», до 32 символов, начинается с буквы.")
        out["key"] = key
    if not partial or "name" in data:
        name = data.get("name")
        if isinstance(name, str):
            name = {"ru": name}
        if not isinstance(name, dict) or not any(str(v).strip() for v in name.values()):
            _bad_key("name", "Укажите название (ru, ky, en, tr, uz).")
        out["name"] = {lang: str(name[lang]).strip()[:64] for lang in svc.LANGS if name.get(lang)}
    if not partial or "type" in data:
        typ = data.get("type") or "text"
        if typ not in ProductAttributeKey.Type.values:
            _bad_key("type", "type: number, text или enum.")
        out["type"] = typ
    if "unit" in data:
        unit = data.get("unit") or ""
        if not isinstance(unit, str) or len(unit) > 16:
            _bad_key("unit", "unit — строка до 16 символов.")
        out["unit"] = unit.strip()
    if "options" in data:
        opts = data.get("options") or []
        if not isinstance(opts, list) or len(opts) > 200 or any(isinstance(o, (dict, list)) for o in opts):
            _bad_key("options", "options — массив значений (до 200).")
        out["options"] = [str(o)[:64] for o in opts]
    if "filterable" in data:
        raw = data.get("filterable")
        out["filterable"] = (raw.lower() in ("1", "true", "yes")) if isinstance(raw, str) else bool(raw)
    final_type = out.get("type") or (instance.type if instance else "text")
    final_opts = out["options"] if "options" in out else (instance.options if instance else [])
    if final_type == "enum" and not final_opts:
        _bad_key("options", "Для типа enum нужен список options.")
    return out


_READ_PERMS = [permissions.IsAuthenticated, flag_permission("can_view_products", read=True)]


class AttributeKeyListCreateAPIView(APIView):
    """GET/POST api/main/attribute-keys/ — справочник ключей атрибутов компании (запись — владелец)."""

    permission_classes = _READ_PERMS

    def get(self, request):
        company = _company_or_400(request)
        rows = ProductAttributeKey.objects.filter(company=company).order_by("key")
        return Response([_key_json(k) for k in rows])

    def post(self, request):
        _owner_only(request)
        company = _company_or_400(request)
        fields = _parse_key_body(request.data)
        if ProductAttributeKey.objects.filter(company=company, key=fields["key"]).exists():
            raise ApiError("attribute_key_exists", "Такой ключ уже есть.", status_code=409, fields={"key": ["Уже есть."]})
        k = ProductAttributeKey.objects.create(company=company, **fields)
        return Response(_key_json(k), status=status.HTTP_201_CREATED)


class AttributeKeyDetailAPIView(APIView):
    """GET/PATCH/DELETE api/main/attribute-keys/<key>/ (запись — владелец)."""

    permission_classes = _READ_PERMS

    def _get(self, request, key):
        company = _company_or_400(request)
        k = ProductAttributeKey.objects.filter(company=company, key=key).first()
        if k is None:
            raise ApiError("not_found", "Ключ не найден.", status_code=404)
        return k

    def get(self, request, key):
        return Response(_key_json(self._get(request, key)))

    def patch(self, request, key):
        _owner_only(request)
        k = self._get(request, key)
        body = {kk: vv for kk, vv in request.data.items() if kk != "key"}
        for f, v in _parse_key_body(body, partial=True, instance=k).items():
            setattr(k, f, v)
        k.save()
        return Response(_key_json(k))

    def delete(self, request, key):
        """Ключ из справочника убирается; значения в товарах остаются (справочник — подсказка)."""
        _owner_only(request)
        self._get(request, key).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AttributeKeyDefaultsAPIView(APIView):
    """POST api/main/attribute-keys/defaults/ — добавить типовые ключи хозмага (diameter_mm, length_m …)."""

    permission_classes = _READ_PERMS

    def post(self, request):
        _owner_only(request)
        company = _company_or_400(request)
        created = svc.ensure_default_keys(company)
        rows = ProductAttributeKey.objects.filter(company=company).order_by("key")
        return Response({"created": created, "results": [_key_json(k) for k in rows]})


def _cat_rel_json(r):
    return {"id": str(r.pk), "category": str(r.category_id), "related_category": str(r.related_category_id), "kind": r.kind}


class CategoryRelationListCreateAPIView(APIView):
    """GET/POST api/main/category-relations/ — правила «к категории подходит категория» (Б-59)."""

    permission_classes = _READ_PERMS

    def get(self, request):
        company = _company_or_400(request)
        rows = CategoryRelation.objects.filter(company=company).order_by("category_id", "related_category_id")
        cat = request.query_params.get("category")
        if cat:
            rows = rows.filter(category_id=cat)
        return Response([_cat_rel_json(r) for r in rows])

    def post(self, request):
        from apps.main.models import ProductCategory

        company = _company_or_400(request)
        data = request.data
        kind = data.get("kind") or ProductRelation.Kind.ACCESSORY
        if kind not in ProductRelation.Kind.values:
            raise ApiError("invalid_category_relation", "kind: accessory или analog.", status_code=400,
                           fields={"kind": ["Неверный вид."]})
        ids = []
        for name in ("category", "related_category"):
            try:
                ids.append(str(uuid.UUID(str(data.get(name)))))
            except ValueError:
                raise ApiError("category_not_found", "Категория не найдена в вашей компании.", status_code=400,
                               fields={name: ["Неверный id категории."]})
        found = {str(c.pk): c for c in ProductCategory.objects.filter(company=company, pk__in=ids)}
        if ids[0] not in found or ids[1] not in found:
            raise ApiError("category_not_found", "Категория не найдена в вашей компании.", status_code=400,
                           fields={"category": ["Категория не найдена."]})
        r, created = CategoryRelation.objects.get_or_create(
            company=company, category=found[ids[0]], related_category=found[ids[1]], kind=kind
        )
        return Response(_cat_rel_json(r), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class CategoryRelationDetailAPIView(APIView):
    """DELETE api/main/category-relations/<uuid>/."""

    permission_classes = _READ_PERMS

    def delete(self, request, pk):
        company = _company_or_400(request)
        n, _ = CategoryRelation.objects.filter(company=company, pk=pk).delete()
        if not n:
            raise ApiError("not_found", "Правило не найдено.", status_code=404)
        return Response(status=status.HTTP_204_NO_CONTENT)
