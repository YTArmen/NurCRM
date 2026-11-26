"""
mrg2111 (21.11.2026, просьба Бекназара): слияние товаров («Асу», «Асу клубника», «Асу лимон» → один товар с доп. штрихкодами).

POST /api/main/products/merge/
  {"target": "<id>", "sources": ["<id>", ...], "variant_names": {"<source_id>": "Клубничный"}, "dry_run": false}
  Idempotency-Key — обязателен (кроме dry_run). Права: как у правки карточки + остатка (владелец/админ или
  can_edit_products), PIN.
  Всё в одной транзакции:
    1) штрихкоды источника (основной и доп.) → доп. штрихкоды цели; name = variant_names[id] или название
       источника; quantity доп. кода сохраняется;
    2) остаток источника → цели одним документом склада kind=count_fix, reason_code=regrade («Пересорт»),
       comment «Объединение товаров …»: строка источника −остаток, строка цели +остаток (дельтой, без перетирания
       продаж; журнал остатков пишется как у обычного документа). Это не списание: в отчёт списаний и потерь не идёт;
    3) партии сроков годности (ProductExpiryBatch) источника с остатком переходят к цели;
    4) источник уходит в архив (как POST products/<id>/archive/), его штрихкоды и PLU освобождены;
       история продаж, приходов и ценников остаётся за источником (в строках чеков штрихкод сохранён снимком).
  → 200 {merge_id, target, moved_barcodes:[...], moved_qty:{source_id: qty}, stock_document, archived:[...]}
     dry_run → то же без записи, merge_id = null.
  Ошибки: 400 invalid | 404 not_found | 409 merge_not_supported (варианты размер/цвет, партии-оттенки, упаковки
  sale_units, весовой ↔ штучный, разные единицы) | 409 barcode_duplicate.

POST /api/main/products/merge/<merge_id>/undo/   (Idempotency-Key необязателен; повтор → 200 already_undone)
  Возвращает всё назад: штрихкоды и PLU источникам, остаток обратным документом count_fix, партии, разархивирует.
  409 undo_blocked — если после слияния штрихкод источника уже отдан другому товару или у цели не хватает остатка
  (тогда data.lines). Откат возможен 30 суток.
GET  /api/main/products/merge/<merge_id>/  — что было сделано (снимок).
Снимок хранится в construction.AuditLog (object_type=product_merge), новых таблиц нет.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.users.permissions import read_only_guard
from apps.users.permissions_flags import flag_guard
from apps.users.pin import pin_guard
from core.exceptions import ApiError

from .models import Product, ProductAlternateBarcode, ProductDeletion, ProductExpiryBatch, ProductVariant
from .views import CompanyBranchRestrictedMixin, _filter_products_company_only

OBJ = "product_merge"
UNDO_DAYS = 30
MAX_SOURCES = 50
Q3 = Decimal("0.001")


def _d(v):
    return Decimal(str(v or 0)).quantize(Q3)


def _audit_model():
    from apps.construction.models import AuditLog
    return AuditLog


def _blockers(p):
    from apps.main.tz26.hw_batches.models import ProductBatch
    from apps.main.tz26.hw_units.models import ProductSaleUnit
    out = []
    if ProductVariant.objects.filter(product=p).exists():
        out.append("variants")
    if ProductBatch.objects.filter(product=p, remaining__gt=0).exists():
        out.append("batches")
    if ProductSaleUnit.objects.filter(product=p).exists():
        out.append("sale_units")
    return out


def _stock_doc(view, request, company, lines, comment, key):
    """Документ склада count_fix через тот же код, что POST stock/documents/ (журнал остатков, номер, кэш)."""
    import hashlib
    import json

    from apps.main.stock_docs_views import StockDocumentListCreateAPIView

    v = StockDocumentListCreateAPIView()
    v.request, v.args, v.kwargs, v.format_kwarg = request, (), {}, None
    body_hash = hashlib.sha256(json.dumps(lines, default=str, sort_keys=True).encode()).hexdigest()
    rows = [{"i": i, "product": str(ln["product"]), "delta": ln["delta"], "cost": ln.get("cost"), "note": ln.get("note", "")}
            for i, ln in enumerate(lines)]
    resp = v._create(request, company, "count_fix", "regrade", comment, timezone.now(), key, body_hash, rows,
                     allow_negative=True)
    if resp.status_code >= 300:
        raise ApiError("stock_document_failed", "Не удалось провести перенос остатка.", status_code=409,
                       data=resp.data)
    return resp.data.get("id")


def _touch(company_id, products, codes):
    now = timezone.now()
    Product.objects.filter(pk__in=[p.pk for p in products]).update(updated_at=now)
    for c in codes:
        if c:
            cache.delete(f"product_barcode:{company_id}:{c}")
    try:
        from apps.main.cache_utils import invalidate_cache_pattern
        invalidate_cache_pattern(f"products:list:{company_id}:")
    except Exception:  # noqa: BLE001
        pass


def _may_merge(user):
    from apps.main.pos_serializers import _is_owner_like
    return _is_owner_like(user) or bool(getattr(user, "can_edit_products", False))


@flag_guard("can_view_products")
@read_only_guard("can_edit_products")
@pin_guard()
class ProductMergeAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        d = request.data if hasattr(request.data, "get") else {}
        dry = str(d.get("dry_run", "")).lower() in ("1", "true", "yes")
        if not dry and not (request.headers.get("Idempotency-Key") or d.get("idempotency_key")):
            raise ApiError("idempotency_key_required", "Передайте Idempotency-Key.", status_code=400)
        if dry:
            return self._run(request, d, dry=True)
        return self._post_real(request)

    @idempotent(body_key=True, replay_flag=True)
    def _post_real(self, request):
        return self._run(request, request.data, dry=False)

    def _run(self, request, d, dry):
        if not _may_merge(request.user):
            raise ApiError("permission_denied", "Объединять товары может владелец, админ или сотрудник с правом правки товаров.",
                           status_code=403)
        target_id = str(d.get("target") or "").strip()
        sources = d.get("sources") or []
        names = d.get("variant_names") or {}
        if not target_id or not isinstance(sources, list) or not sources or not isinstance(names, dict):
            raise ApiError("invalid", "Нужны target, sources (список id) и variant_names (объект, необязательно).")
        sources = list(dict.fromkeys(str(s).strip() for s in sources if str(s).strip()))
        if target_id in sources:
            raise ApiError("invalid", "Цель не может быть в списке источников.", fields={"sources": ["Есть target."]})
        if len(sources) > MAX_SOURCES:
            raise ApiError("invalid", f"Не больше {MAX_SOURCES} источников за раз.")
        for k, v in names.items():
            if len(str(v or "")) > 255:
                raise ApiError("invalid", "Название варианта длиннее 255 знаков.", fields={f"variant_names.{k}": ["До 255."]})

        with transaction.atomic():
            qs = _filter_products_company_only(self, Product.objects.all()).select_for_update()
            try:
                found = {str(p.pk): p for p in qs.filter(pk__in=[target_id, *sources])}
            except Exception:  # noqa: BLE001 — неверный uuid
                found = {}
            missing = [x for x in [target_id, *sources] if x not in found]
            if missing:
                raise ApiError("not_found", "Товар не найден (или в архиве).", status_code=404, data={"missing": missing})
            target = found[target_id]
            srcs = [found[s] for s in sources]
            company_id = target.company_id

            problems = []
            for p in [target, *srcs]:
                b = _blockers(p)
                if b:
                    problems.append({"product_id": str(p.pk), "name": p.name, "reasons": b})
            for p in srcs:
                if bool(p.is_weight) != bool(target.is_weight) or (p.unit or "шт") != (target.unit or "шт"):
                    problems.append({"product_id": str(p.pk), "name": p.name, "reasons": ["unit_mismatch"]})
            if problems:
                raise ApiError("merge_not_supported",
                               "Эти товары пока нельзя объединить автоматически (варианты, партии-оттенки, упаковки или "
                               "разные единицы).", status_code=409, data={"products": problems})

            existing = set(ProductAlternateBarcode.objects.filter(product=target).values_list("barcode", flat=True))
            main_t = (target.barcode or "").strip()
            moved, snap_sources, moved_qty, stock_lines = [], [], {}, []
            for p in srcs:
                alts = list(ProductAlternateBarcode.objects.filter(product=p).order_by("barcode"))
                vname = str(names.get(str(p.pk)) or "").strip() or (p.name or "")[:255]
                codes = []
                if (p.barcode or "").strip():
                    codes.append({"barcode": p.barcode.strip(), "name": vname, "quantity": None, "was": "main"})
                for a in alts:
                    codes.append({"barcode": a.barcode, "name": a.name or vname, "quantity": a.quantity, "was": "alt",
                                  "orig_name": a.name})
                for c in codes:
                    if c["barcode"] == main_t or c["barcode"] in existing:
                        c["skip"] = True  # уже у цели
                    else:
                        existing.add(c["barcode"])
                        moved.append({"barcode": c["barcode"], "name": c["name"], "quantity": c["quantity"],
                                      "from": str(p.pk)})
                qty = _d(p.quantity)
                moved_qty[str(p.pk)] = format(qty, "f")
                if qty != 0:
                    cost = p.purchase_price
                    stock_lines.append({"product": p.pk, "delta": -qty, "cost": cost, "note": f"→ {target.name}"[:255]})
                    stock_lines.append({"product": target.pk, "delta": qty, "cost": cost, "note": f"← {p.name}"[:255]})
                exp = list(ProductExpiryBatch.objects.filter(product=p, remaining_quantity__gt=0).values_list("id", flat=True))
                snap_sources.append({
                    "id": str(p.pk), "name": p.name, "barcode": p.barcode, "plu": p.plu, "status": p.status,
                    "alternate_barcodes": [{"barcode": a.barcode, "name": a.name, "quantity": a.quantity} for a in alts],
                    "quantity": format(qty, "f"), "expiry_batches": [str(x) for x in exp],
                })

            result = {
                "merge_id": None, "target": str(target.pk), "target_name": target.name,
                "moved_barcodes": moved, "moved_qty": moved_qty, "archived": [str(p.pk) for p in srcs],
                "stock_document": None, "dry_run": dry,
            }
            if dry:
                transaction.set_rollback(True)
                return Response(result)

            merge_id = uuid.uuid4()
            all_codes = []
            for p, s in zip(srcs, snap_sources):
                ProductAlternateBarcode.objects.filter(product=p).delete()
                Product.objects.filter(pk=p.pk).update(barcode=None, plu=None, status=Product.Status.ARCHIVED)
                ProductDeletion.objects.create(company_id=company_id, product_id=p.pk)
                if s["plu"] is not None:
                    cache.delete(f"product_plu:{company_id}:{s['plu']}")
                all_codes += [s["barcode"], *[a["barcode"] for a in s["alternate_barcodes"]]]
                if s["expiry_batches"]:
                    ProductExpiryBatch.objects.filter(id__in=s["expiry_batches"]).update(product_id=target.pk)
            ProductAlternateBarcode.objects.bulk_create([
                ProductAlternateBarcode(product=target, company_id=company_id, barcode=m["barcode"],
                                        name=(m["name"] or "")[:255], quantity=m["quantity"])
                for m in moved
            ])
            doc_id = None
            if stock_lines:
                names_s = ", ".join(p.name for p in srcs)[:300]
                doc_id = _stock_doc(self, request, target.company,
                                    stock_lines, f"Объединение товаров → «{target.name}»: {names_s}"[:1000],
                                    f"merge:{merge_id}")
            _touch(company_id, [target, *srcs], all_codes)
            result.update({"merge_id": str(merge_id), "stock_document": doc_id})
            _audit_model().objects.create(
                company_id=company_id, user=request.user if request.user.is_authenticated else None,
                action="merge", object_type=OBJ, object_id=str(merge_id), object_name=(target.name or "")[:255],
                before={"target": str(target.pk), "sources": snap_sources},
                after={**result, "undone": False},
            )
            return Response(result)


@flag_guard("can_view_products")
@read_only_guard("can_edit_products")
@pin_guard()
class ProductMergeUndoAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _log(self, merge_id):
        company = self._company()
        log = _audit_model().objects.filter(company_id=getattr(company, "id", None), object_type=OBJ,
                                            object_id=str(merge_id), action="merge").first()
        if log is None:
            raise ApiError("not_found", "Слияние не найдено.", status_code=404)
        return company, log

    def get(self, request, merge_id):
        _, log = self._log(merge_id)
        undone = _audit_model().objects.filter(company_id=log.company_id, object_type=OBJ, object_id=str(merge_id),
                                               action="undo").first()
        return Response({**(log.after or {}), "sources": (log.before or {}).get("sources"),
                         "created_at": log.created_at.isoformat(), "undone": undone is not None,
                         "undone_at": undone.created_at.isoformat() if undone else None})

    def post(self, request, merge_id):
        if not _may_merge(request.user):
            raise ApiError("permission_denied", "Откатить слияние может владелец, админ или сотрудник с правом правки товаров.",
                           status_code=403)
        AuditLog = _audit_model()
        with transaction.atomic():
            company, log = self._log(merge_id)
            log = AuditLog.objects.select_for_update().get(pk=log.pk)
            if AuditLog.objects.filter(company_id=log.company_id, object_type=OBJ, object_id=str(merge_id),
                                       action="undo").exists():
                return Response({"merge_id": str(merge_id), "already_undone": True})
            if log.created_at < timezone.now() - timedelta(days=UNDO_DAYS):
                raise ApiError("undo_expired", f"Откат возможен {UNDO_DAYS} суток.", status_code=409)
            before = log.before or {}
            after = log.after or {}
            ids = [before.get("target"), *[s["id"] for s in before.get("sources", [])]]
            prods = {str(p.pk): p for p in Product.objects.select_for_update().filter(company_id=log.company_id, pk__in=ids)}
            target = prods.get(before.get("target"))
            if target is None or any(s["id"] not in prods for s in before.get("sources", [])):
                raise ApiError("undo_blocked", "Товар слияния удалён — откат невозможен.", status_code=409)
            company_id = log.company_id
            # проверки: коды источников свободны (кроме тех, что сейчас у цели), у цели хватает остатка
            blocked = []
            moved_codes = {m["barcode"] for m in after.get("moved_barcodes", [])}
            for s in before["sources"]:
                for code in [s["barcode"], *[a["barcode"] for a in s["alternate_barcodes"]]]:
                    if not code:
                        continue
                    other = Product.objects.filter(company_id=company_id, barcode=code).exclude(pk=s["id"]).first()
                    alt = (ProductAlternateBarcode.objects.filter(company_id=company_id, barcode=code)
                           .exclude(product_id=s["id"]).exclude(product_id=target.pk if code in moved_codes else None)
                           .select_related("product").first())
                    owner = other or (alt.product if alt else None)
                    if owner is not None:
                        blocked.append({"barcode": code, "product_id": str(owner.pk), "name": owner.name})
                if s.get("plu") is not None and Product.objects.filter(company_id=company_id, plu=s["plu"]).exclude(pk=s["id"]).exists():
                    blocked.append({"plu": s["plu"], "source": s["id"]})
            if blocked:
                raise ApiError("undo_blocked", "Штрихкоды/PLU источников уже заняты другими товарами.", status_code=409,
                               data={"lines": blocked})
            total_back = sum((_d(s["quantity"]) for s in before["sources"]), Decimal("0"))
            if total_back > 0 and _d(target.quantity) < total_back:
                raise ApiError("undo_blocked", "У товара-цели меньше остатка, чем было перенесено.", status_code=409,
                               data={"lines": [{"product_id": str(target.pk), "available": format(_d(target.quantity), "f"),
                                                "needed": format(total_back, "f")}]})

            ProductAlternateBarcode.objects.filter(product=target, barcode__in=moved_codes).delete()
            stock_lines, codes = [], []
            for s in before["sources"]:
                Product.objects.filter(pk=s["id"]).update(barcode=s["barcode"], plu=s["plu"], status=s["status"])
                ProductAlternateBarcode.objects.bulk_create([
                    ProductAlternateBarcode(product_id=s["id"], company_id=company_id, barcode=a["barcode"],
                                            name=a["name"] or "", quantity=a["quantity"])
                    for a in s["alternate_barcodes"]
                ])
                ProductDeletion.objects.filter(company_id=company_id, product_id=s["id"]).delete()
                if s["expiry_batches"]:
                    ProductExpiryBatch.objects.filter(id__in=s["expiry_batches"]).update(product_id=s["id"])
                q = _d(s["quantity"])
                if q != 0:
                    src = prods[s["id"]]
                    stock_lines.append({"product": target.pk, "delta": -q, "cost": src.purchase_price, "note": f"откат → {s['name']}"[:255]})
                    stock_lines.append({"product": src.pk, "delta": q, "cost": src.purchase_price, "note": "откат слияния"})
                codes += [s["barcode"], *[a["barcode"] for a in s["alternate_barcodes"]]]
            doc_id = None
            if stock_lines:
                doc_id = _stock_doc(self, request, target.company, stock_lines,
                                    f"Откат объединения товаров → «{target.name}»"[:1000], f"merge-undo:{merge_id}")
            _touch(company_id, list(prods.values()), codes)
            AuditLog.objects.create(company_id=company_id, user=request.user if request.user.is_authenticated else None,
                                    action="undo", object_type=OBJ, object_id=str(merge_id),
                                    object_name=(target.name or "")[:255], before=None,
                                    after={"stock_document": doc_id})
            return Response({"merge_id": str(merge_id), "undone": True, "stock_document": doc_id,
                             "restored": [s["id"] for s in before["sources"]]})
