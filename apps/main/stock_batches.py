"""19.11.2026 К-4/К-5 (k458): партии товара, FIFO/FEFO-себестоимость, сроки по партиям.

Как работает. Каждое изменение остатка уже пишется в журнал (main_stockledger, триггер на main_product).
Второй триггер (nur_stock_batches, миграция 0196) по строке журнала ведёт партии main_stockbatch:
  + приход  -> новая партия (цена строки прихода, срок, номер партии, поставщик — из подсказки ниже);
  + возврат покупателя -> обратно в партии этой продажи; прочий плюс -> партия по закупке карточки;
  − расход  -> из партий: сначала партии документа (удаление/правка прихода), потом раньше срок (FEFO),
               потом раньше приход (FIFO); не хватило — движение без партии (минус).
Первое движение товара раскладывает текущий остаток по последним приходам (nur_batches_init).
Включено, если в settings/stock/ costing_method = "fifo" или batches = true. Сбой партий не ломает продажу.

Себестоимость продажи (costing_method = "fifo"): после списания checkout_cart вызывает apply_sale_cost(sale) —
SaleItem.purchase_price_snapshot пересчитывается по ценам партий (отчёты прибыли считают по нему).
В строке чека: cogs, unit_cost, allocations[{batch_id, qty, unit_cost, expires_at, lot_no}].

API:
  GET products/<id>/batches/?open=1        партии товара
  GET stock/expiring/?days=7               (для компаний с партиями — по партиям)
  settings/stock/: costing_method last|average|fifo, batches true|false, expired_sale_policy allow|warn|block
  Строка прихода: expires_at (YYYY-MM-DD), produced_at + shelf_life_days, lot_no.
"""
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

log = logging.getLogger("nurcrm.stock_batches")
EXPIRED_POLICIES = ("allow", "warn", "block")
_Q4 = Decimal("0.0001")


# ------------------------------------------------------------------ настройки

def _settings(company) -> dict:
    try:
        from apps.main.stock_policy import _row

        row = _row(company) if company is not None else None
        return dict((row.settings or {}) if row else {})
    except Exception:  # noqa: BLE001
        return {}


def batches_on(company) -> bool:
    s = _settings(company)
    return s.get("costing_method") == "fifo" or s.get("batches") in (True, "true")


def fifo_on(company) -> bool:
    return _settings(company).get("costing_method") == "fifo"


def expired_policy(company) -> str:
    v = _settings(company).get("expired_sale_policy") or "allow"
    return v if v in EXPIRED_POLICIES else "allow"


# ------------------------------------------------------------------ подсказки для триггера

def _req():
    try:
        from apps.main.stock_ledger import _REQ

        return _REQ.get()
    except Exception:  # noqa: BLE001
        return None


def set_doc(doc_id):
    """Документ следующих движений остатка (продажа: id чека) — по нему возврат и себестоимость."""
    req = _req()
    if req is not None:
        req._ledger_doc = str(doc_id) if doc_id else None


def set_hint(**kw):
    """Подсказка партии для следующего движения: k — цена, e — срок, l — номер партии, sup — поставщик, rd — документ."""
    req = _req()
    if req is None:
        return
    req._ledger_extra = {k: (str(v) if v is not None else None) for k, v in kw.items()} if kw else None


def clear_hint():
    req = _req()
    if req is not None:
        req._ledger_extra = None


def receipt_line_hint(raw, it, receipt, supplier_id):
    """Подсказка для строки прихода: цена строки, срок годности, номер партии."""
    raw = raw if isinstance(raw, dict) else {}
    exp = None
    if raw.get("expires_at"):
        exp = parse_date(str(raw.get("expires_at"))[:10])
    elif raw.get("produced_at") and raw.get("shelf_life_days") not in (None, ""):
        d = parse_date(str(raw.get("produced_at"))[:10])
        try:
            exp = d + timedelta(days=int(raw.get("shelf_life_days"))) if d else None
        except (TypeError, ValueError):
            exp = None
    lot = str(raw.get("lot_no") or raw.get("batch_label") or "").strip()[:64]
    op = None  # закупка товара до этого прихода — цена старого остатка при первой раскладке
    try:
        prod = it.get("product")
        pid = getattr(prod, "id", prod)
        if pid:
            from apps.main.models import Product
            op = Product.objects.filter(id=pid).values_list("purchase_price", flat=True).first()
    except Exception:
        op = None
    set_hint(k=it.get("purchase_price"), e=exp.isoformat() if exp else None, l=lot or None,
             sup=supplier_id, rd=receipt.id, op=op)


# ------------------------------------------------------------------ себестоимость продажи

def _moves_for_doc(doc_id, product_ids=None):
    sql = ("SELECT m.product_id, m.batch_id, m.qty, m.unit_cost, b.expires_at, b.lot_no FROM main_stockbatchmove m "
           "LEFT JOIN main_stockbatch b ON b.id = m.batch_id WHERE m.doc_id = %s AND m.qty < 0")
    params = [str(doc_id)]
    if product_ids:
        sql += " AND m.product_id = ANY(%s::uuid[])"
        params.append([str(p) for p in product_ids])
    with connection.cursor() as cur:
        cur.execute(sql + " ORDER BY m.id", params)
        rows = cur.fetchall()
    out = defaultdict(list)
    for pid, bid, qty, cost, exp, lot in rows:
        out[str(pid)].append({"batch_id": str(bid) if bid else None, "qty": -Decimal(qty), "unit_cost": Decimal(cost),
                              "expires_at": exp.isoformat() if exp else None, "lot_no": lot or ""})
    return out


def _base_units(item):
    """Сколько базовых единиц списала строка (как в checkout_cart)."""
    if getattr(item, "base_quantity", None) is not None:
        return Decimal(str(item.base_quantity))
    q = Decimal(str(item.quantity or 0))
    pkg = getattr(item, "sale_package", None) if getattr(item, "sale_package_id", None) else None
    if pkg is not None:
        try:
            from apps.main.pos_utils import line_qty_consume_units

            return Decimal(str(line_qty_consume_units(q, pkg)))
        except Exception:  # noqa: BLE001
            return q
    return q


def allocations_for_sale(sale, items=None):
    """{sale_item_id: [allocation, ...]} — партии делятся между строками одного товара по порядку."""
    items = list(items if items is not None else sale.items.all())
    moves = _moves_for_doc(sale.id, {i.product_id for i in items if i.product_id})
    res = {}
    for it in items:
        pool = moves.get(str(it.product_id)) if it.product_id else None
        need = _base_units(it)
        alloc = []
        while pool and need > 0:
            m = pool[0]
            take = min(need, m["qty"])
            alloc.append({**m, "qty": take})
            m["qty"] -= take
            need -= take
            if m["qty"] <= 0:
                pool.pop(0)
        res[it.id] = alloc
    return res


def apply_sale_cost(sale):
    """FIFO: себестоимость строк чека по ценам партий (purchase_price_snapshot = себестоимость проданной единицы)."""
    try:
        if not fifo_on(sale.company):
            return
        from apps.main.models import SaleItem

        items = list(SaleItem.objects.filter(sale=sale).select_related("sale_package"))
        allocs = allocations_for_sale(sale, items)
        upd = []
        for it in items:
            al = allocs.get(it.id) or []
            q = Decimal(str(it.quantity or 0))
            if not al or q <= 0:
                continue
            total = sum((a["qty"] * a["unit_cost"] for a in al), Decimal("0"))
            covered = sum((a["qty"] for a in al), Decimal("0"))
            base = _base_units(it)
            if covered < base:  # нехватка партий — остаток по старой себестоимости строки
                per_base_old = (Decimal(str(it.purchase_price_snapshot or 0)) * q / base) if base else Decimal("0")
                total += (base - covered) * per_base_old
            it.purchase_price_snapshot = (total / q).quantize(_Q4)
            upd.append(it)
        if upd:
            SaleItem.objects.bulk_update(upd, ["purchase_price_snapshot"])
    except Exception:  # noqa: BLE001 — себестоимость никогда не ломает продажу
        log.exception("apply_sale_cost failed sale=%s", getattr(sale, "id", None))


# ------------------------------------------------------------------ срок годности при продаже

def expired_lines(company, demand):
    """demand: {product_id: base_qty}. Какие товары при FEFO уйдут из просроченной партии."""
    if not demand or not batches_on(company):
        return []
    today = timezone.localdate()
    with connection.cursor() as cur:
        cur.execute("SELECT product_id, sum(qty_left), min(expires_at) FROM main_stockbatch WHERE status = 'open' "
                    "AND qty_left > 0 AND expires_at < %s AND product_id = ANY(%s::uuid[]) GROUP BY product_id",
                    [today, [str(p) for p in demand]])
        return [{"product_id": str(pid), "expired_qty": format(q, "f"), "expires_at": exp.isoformat()}
                for pid, q, exp in cur.fetchall()]


# ------------------------------------------------------------------ API

def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _batch_row(r, today, show_cost):
    (bid, typ, doc, sup, qin, qleft, cost, rec, exp, lot, st) = r
    return {"id": str(bid), "source_doc_type": typ, "source_doc_id": doc, "supplier_id": str(sup) if sup else None,
            "qty_in": format(qin, "f"), "qty_left": format(qleft, "f"),
            "unit_cost": format(cost, "f") if show_cost else None,
            "received_at": rec.isoformat(), "expires_at": exp.isoformat() if exp else None,
            "days_left": (exp - today).days if exp else None, "lot_no": lot,
            "status": "expired" if (st == "open" and exp and exp < today) else st}


def _show_cost(request):
    try:
        from apps.main.test_marks import hides_cost

        return not hides_cost(request)
    except Exception:  # noqa: BLE001
        return True


class ProductBatchesAPIView(APIView):
    """GET products/<id>/batches/?open=1 — партии товара (К-4). Партии выключены или ?kind=label — партии-метки Б-61."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        from apps.main.models import Product

        company = _company(request.user)
        p = Product.objects.filter(pk=pk, company=company).only("id", "quantity", "name").first()
        if p is None:
            return Response({"code": "not_found", "detail": "Товар не найден."}, status=404)
        if not batches_on(company) or request.query_params.get("kind") == "label":
            # партии-метки (оттенок/калибр, ТЗ 26.10 Б-61) — прежний ответ этого адреса
            from apps.main.tz26.hw_batches.views import ProductBatchesAPIView as _Labels

            return _Labels.as_view()(request._request, pk=pk)
        with connection.cursor() as cur:
            cur.execute("SELECT nur_batches_init(%s, %s)", [str(p.id), p.quantity or 0])
            sql = ("SELECT id, source_doc_type, source_doc_id, supplier_id, qty_in, qty_left, unit_cost, received_at, "
                   "expires_at, lot_no, status FROM main_stockbatch WHERE product_id = %s AND qty_in > 0")
            if str(request.query_params.get("open", "")).lower() in ("1", "true", "yes"):
                sql += " AND status = 'open' AND qty_left > 0"
            cur.execute(sql + " ORDER BY expires_at ASC NULLS LAST, received_at, created_at", [str(p.id)])
            rows = cur.fetchall()
        today = timezone.localdate()
        show = _show_cost(request)
        res = [_batch_row(r, today, show) for r in rows]
        left = sum((Decimal(r["qty_left"]) for r in res if r["status"] in ("open", "expired")), Decimal("0"))
        return Response({"product_id": str(p.id), "name": p.name, "quantity": format(p.quantity or 0, "f"),
                         "batches_qty": format(left, "f"), "costing_method": _settings(company).get("costing_method", "last"),
                         "count": len(res), "results": res})


def expiring_from_batches(request, company, days, page, size, product=None):
    today = timezone.localdate()
    where = ("WHERE b.company_id = %s AND b.status = 'open' AND b.qty_left > 0 AND b.expires_at IS NOT NULL "
             "AND b.expires_at <= %s")
    params = [str(company.id), today + timedelta(days=days)]
    if product:
        where += " AND b.product_id = %s"
        params.append(str(product))
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM main_stockbatch b " + where, params)
        total = cur.fetchone()[0]
        cur.execute("SELECT b.id, b.product_id, p.name, b.qty_left, b.expires_at, b.received_at, b.source_doc_type, b.lot_no "
                    "FROM main_stockbatch b JOIN main_product p ON p.id = b.product_id " + where +
                    " ORDER BY b.expires_at, b.received_at LIMIT %s OFFSET %s", params + [size, (page - 1) * size])
        rows = cur.fetchall()
    return {"days": days, "today": today.isoformat(), "count": total, "page": page, "page_size": size, "source": "batches",
            "results": [{"batch_id": str(i), "product_id": str(pid), "product": str(pid), "name": nm,
                         "remaining": format(q, "f"), "qty_left": format(q, "f"), "expires_at": e.isoformat(),
                         "days_left": (e - today).days, "expired": e < today, "received_at": r.isoformat(),
                         "source_kind": t, "lot_no": lot} for i, pid, nm, q, e, r, t, lot in rows]}


def pick_raw(request, pid):
    """Сырая строка прихода из тела запроса для товара pid (по порядку, каждая — один раз)."""
    try:
        raws = request.data.get("items") if hasattr(request.data, "get") else None
        if not isinstance(raws, (list, tuple)):
            return None
        used = getattr(request, "_k458_used", None)
        if used is None:
            used = request._k458_used = set()
        for i, r in enumerate(raws):
            if i in used or not isinstance(r, dict):
                continue
            if str(r.get("product") or r.get("product_id") or "") == str(pid):
                used.add(i)
                return r
    except Exception:  # noqa: BLE001
        return None
    return None


def check_expired(view, company, items):
    """Быстрый чек кассы: товар уйдёт из просроченной партии -> warn: warnings[expired_batch], block: 409.
    Чек из очереди (offline / allow_negative_stock) — только предупреждение: продажа уже состоялась."""
    try:
        pol = expired_policy(company)
        if pol == "allow" or not batches_on(company):
            return
        demand = defaultdict(Decimal)
        idx = {}
        for i, it in enumerate(items or []):
            pid = it.get("product") if isinstance(it, dict) else None
            if pid:
                demand[str(pid)] += Decimal(str(it.get("qty") or 0))
                idx.setdefault(str(pid), i)
        rows = expired_lines(company, demand)
    except Exception:  # noqa: BLE001
        log.exception("check_expired failed")
        return
    if not rows:
        return
    if pol == "block" and not getattr(view, "_force_minus_queue", False):
        from core.exceptions import ApiError

        raise ApiError("expired_batch", "В продаже товар с истёкшим сроком — продажа запрещена настройкой.",
                       status_code=409, data={"lines": [{**r, "index": idx.get(r["product_id"])} for r in rows]})
    for r in rows:
        view._warnings.append({"code": "expired_batch", "index": idx.get(r["product_id"]), **r})


class _AllocCache(dict):
    pass


def wrap_sale_item_serializer(cls):
    """Строка чека: allocations по партиям (только у компаний с партиями)."""
    orig = cls.to_representation

    def to_representation(self, obj):
        data = orig(self, obj)
        try:
            if not isinstance(data, dict) or not getattr(obj, "sale_id", None) or not getattr(obj, "product_id", None):
                return data
            ctx = self.context if isinstance(getattr(self, "context", None), dict) else {}
            cache = ctx.setdefault("_k458", _AllocCache()) if isinstance(ctx, dict) else _AllocCache()
            cid = getattr(obj, "company_id", None)
            on_key = ("on", cid)
            if on_key not in cache:
                from apps.users.models import Company

                cache[on_key] = batches_on(Company.objects.filter(pk=cid).first()) if cid else False
            if not cache[on_key]:
                return data
            if obj.sale_id not in cache:
                cache[obj.sale_id] = allocations_for_sale(obj.sale)
            al = cache[obj.sale_id].get(obj.id) or []
            show = _show_cost(ctx.get("request"))
            data["allocations"] = [{"batch_id": a["batch_id"], "qty": format(a["qty"], "f"),
                                    "unit_cost": format(a["unit_cost"], "f") if show else None,
                                    "expires_at": a["expires_at"], "lot_no": a["lot_no"]} for a in al]
        except Exception:  # noqa: BLE001
            log.exception("allocations failed")
        return data

    cls.to_representation = to_representation
