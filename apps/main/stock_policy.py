"""П0-4 К-16 (14.11.2026, k16_1411): политика отрицательных остатков компании.

GET/PUT settings/stock/ {"negative_stock_policy": "forbid"|"warn"|"allow"} — менять может владелец/админ.
Хранится в users.CompanyAppSettings (app="stock", без филиала), без миграций. По умолчанию forbid — как было.
Где действует (pos/checkout/, быстрый чек кассы):
- forbid: онлайн-продажа сверх остатка -> 409 insufficient_stock (как раньше);
- warn:   продажа проходит, остаток уходит в минус, в ответе warnings [{code: negative_stock, product_id, balance}];
- allow:  проходит, предупреждения те же (касса может их не показывать).
Офлайн-досылка (offline=true) и allow_negative_stock (чек из очереди) проходят при любой политике — с warnings.

К-4 (15.11.2026, k4_1511): costing_method — как приход меняет закупочную цену товара (= себестоимость продажи):
- "last" (по умолчанию, как было): закупочная = цена последнего прихода;
- "average": средняя взвешенная: (остаток × старая + приход × новая) / (остаток + приход); при остатке ≤ 0 — новая.
Себестоимость строки продажи — закупочная в момент продажи (SaleItem.purchase_price_snapshot):
в чеке cogs = qty × она. "fifo" — пока не поддерживается (400 costing_method_not_supported).
"""
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

APP = "stock"
POLICIES = ("forbid", "warn", "allow")
DEFAULT = "forbid"
COSTING = ("last", "average", "fifo")  # k458: fifo — по партиям


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _row(company, create=False):
    from apps.users.models import CompanyAppSettings

    row = CompanyAppSettings.objects.filter(company=company, branch__isnull=True, app=APP).first()
    if row is None and create:
        row = CompanyAppSettings(company=company, app=APP, settings={})
    return row


def policy_of(company) -> str:
    if company is None:
        return DEFAULT
    try:
        row = _row(company)
        val = ((row.settings or {}).get("negative_stock_policy") if row else None) or DEFAULT
    except Exception:  # noqa: BLE001 — настройка никогда не ломает продажу
        return DEFAULT
    return val if val in POLICIES else DEFAULT


def costing_of(company) -> str:
    if company is None:
        return "last"
    try:
        row = _row(company)
        val = ((row.settings or {}).get("costing_method") if row else None) or "last"
    except Exception:  # noqa: BLE001
        return "last"
    return val if val in COSTING else "last"


def receipt_cost(company, product, qty, new_price):
    """К-4: закупочная цена товара после прихода qty по цене new_price."""
    from decimal import Decimal

    if costing_of(company) != "average":
        return new_price
    # несколько строк одного товара в приходе: берём остаток и цену после предыдущих строк (_k4_q/_k4_p)
    old_q = Decimal(str(getattr(product, "_k4_q", None) if getattr(product, "_k4_q", None) is not None
                        else (getattr(product, "quantity", None) or 0)))
    old_p = getattr(product, "_k4_p", None) if getattr(product, "_k4_p", None) is not None else getattr(product, "purchase_price", None)
    qty = Decimal(str(qty or 0))
    if old_q <= 0 or old_p is None or qty <= 0:
        res = new_price
    else:
        res = ((old_q * Decimal(str(old_p)) + qty * Decimal(str(new_price))) / (old_q + qty)).quantize(Decimal("0.0001"))
    product._k4_p = res
    return res


def receipt_line_done(product, qty):
    """После строки прихода: остаток товара в памяти для следующей строки того же товара (К-4)."""
    from decimal import Decimal

    base = getattr(product, "_k4_q", None)
    if base is None:
        base = getattr(product, "quantity", None) or 0
    product._k4_q = Decimal(str(base)) + Decimal(str(qty or 0))


def negative_warnings(items):
    """_stock_negative(sale) -> warnings кассы."""
    return [{"code": "negative_stock", "index": None, "product_id": r.get("product_id"),
             "variant_id": r.get("variant_id"), "balance": r.get("balance")} for r in (items or [])]


class StockSettingsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        c = _company(request.user)
        return Response({"negative_stock_policy": policy_of(c), "policies": list(POLICIES),
                         "costing_method": costing_of(c), "costing_methods": list(COSTING), **_k458_extra(c)})

    def put(self, request):
        from apps.main.pos_serializers import _is_owner_like

        if not _is_owner_like(request.user):
            return Response({"code": "permission_denied", "detail": "Настройки склада меняет владелец.",
                             "permission": "owner"}, status=403)
        data = request.data or {}
        upd = {}
        if "negative_stock_policy" in data:
            val = str(data.get("negative_stock_policy") or "").strip()
            if val not in POLICIES:
                return Response({"code": "validation_error", "detail": "negative_stock_policy: forbid, warn или allow.",
                                 "fields": {"negative_stock_policy": ["forbid, warn или allow."]}}, status=400)
            upd["negative_stock_policy"] = val
        if "costing_method" in data:
            cm = str(data.get("costing_method") or "").strip()
            if cm not in COSTING:
                return Response({"code": "validation_error", "detail": "costing_method: last, average или fifo.",
                                 "fields": {"costing_method": ["last, average или fifo."]}}, status=400)
            upd["costing_method"] = cm
        if "batches" in data:  # k458: партии без FIFO-себестоимости (сроки, остатки по партиям)
            upd["batches"] = str(data.get("batches")).strip().lower() in ("1", "true", "yes", "on")
        if "expired_sale_policy" in data:
            ep = str(data.get("expired_sale_policy") or "").strip()
            if ep not in ("allow", "warn", "block"):
                return Response({"code": "validation_error", "detail": "expired_sale_policy: allow, warn или block.",
                                 "fields": {"expired_sale_policy": ["allow, warn или block."]}}, status=400)
            upd["expired_sale_policy"] = ep
        if "reserved_sale_policy" in data:  # К-8
            rp = str(data.get("reserved_sale_policy") or "").strip()
            if rp not in ("warn", "block"):
                return Response({"code": "validation_error", "detail": "reserved_sale_policy: warn или block.",
                                 "fields": {"reserved_sale_policy": ["warn или block."]}}, status=400)
            upd["reserved_sale_policy"] = rp
        if not upd:
            return Response({"code": "validation_error", "detail": "Нечего менять."}, status=400)
        company = _company(request.user)
        row = _row(company, create=True)
        row.settings = {**(row.settings or {}), **upd}
        fields = ["settings"]
        if hasattr(row, "version"):
            row.version = (row.version or 0) + 1
            fields.append("version")
        if hasattr(row, "updated_by_id"):
            row.updated_by = request.user
            fields.append("updated_by")
        if row.pk and not row._state.adding:
            row.save(update_fields=fields)
        else:
            row.save()
        return Response({"negative_stock_policy": policy_of(company), "policies": list(POLICIES),
                         "costing_method": costing_of(company), "costing_methods": list(COSTING), **_k458_extra(company)})

    patch = put


def _k458_extra(company):
    """k458: партии и продажа просрочки."""
    from apps.main.stock_batches import batches_on, expired_policy

    return {"batches": batches_on(company), "expired_sale_policy": expired_policy(company),
            "expired_sale_policies": ["allow", "warn", "block"],
            "reserved_sale_policy": __import__("apps.main.stock_reservations", fromlist=["policy_of"]).policy_of(company),
            "reserved_sale_policies": ["warn", "block"]}
