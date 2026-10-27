"""Журнал денежных операций: запись одной строкой INSERT, ошибки журнала не ломают операцию."""
import logging

CASHFLOW_FIELDS = ("amount", "type", "cashbox_id", "category_id", "name", "status", "created_at")


def cashflow_snapshot(cf):
    out = {}
    for f in CASHFLOW_FIELDS:
        v = getattr(cf, f, None)
        out[f] = str(v) if v is not None else None
    return out


def log_change(*, company, user, action, object_type, object_id="", object_name="", before=None, after=None, request=None, branch=None):
    try:
        from apps.construction.models import AuditLog

        if before is not None and after is not None:
            keys = [k for k in after if before.get(k) != after.get(k)]
            if not keys and action in ("update", "rights_change"):
                return
            before = {k: before.get(k) for k in keys}
            after = {k: after[k] for k in keys}
        ip = None
        if request is not None:
            xff = request.META.get("HTTP_X_FORWARDED_FOR")
            ip = (xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")) or None
        AuditLog.objects.create(
            company=company, user=user if getattr(user, "pk", None) else None, action=action,
            object_type=object_type, object_id=str(object_id or ""), object_name=(object_name or "")[:255],
            before=before, after=after, ip=ip, branch=branch,
        )
    except Exception:
        logging.getLogger(__name__).warning("audit log failed", exc_info=True)


def log_cashflow(request, cf, action, before=None, after=None):
    log_change(
        company=cf.company, user=request.user, action=action, object_type="cashflow", object_id=cf.pk,
        object_name=cf.name, before=before, after=after, request=request,
    )


# ─────────────────────────────────────────────────────────────
# ТЗ «Админка» п.8: общий журнал действий (товары, клиенты, продажи, права, вход)
# ─────────────────────────────────────────────────────────────
PRODUCT_AUDIT_FIELDS = ("price", "purchase_price", "markup_percent", "quantity")


def request_company(request):
    user = getattr(request, "user", None)
    return getattr(user, "company", None) or getattr(user, "owned_company", None)


def _user_branch(user):
    try:
        return user.primary_branch()
    except Exception:
        return None


def log_request(request, action, object_type, object_id="", object_name="", before=None, after=None, company=None):
    """Запись от имени request.user; company по умолчанию — компания пользователя."""
    company = company or request_company(request)
    if company is None:
        return
    log_change(
        company=company, user=request.user, action=action, object_type=object_type, object_id=object_id,
        object_name=object_name, before=before, after=after, request=request, branch=_user_branch(request.user),
    )


def _n(v):
    """Число в одном виде (100.00 и 100.0000 — одно и то же), чтобы журнал не писал «изменений» без изменений."""
    from decimal import Decimal, InvalidOperation

    if v is None:
        return None
    try:
        return format(Decimal(str(v)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(v)


def product_snapshot(p):
    return {f: _n(getattr(p, f, None)) for f in PRODUCT_AUDIT_FIELDS}


def product_values(pk):
    """Снимок полей товара прямо из БД (до изменения) или None."""
    from apps.main.models import Product

    row = Product.objects.filter(pk=pk).values(*PRODUCT_AUDIT_FIELDS, "name").first()
    if not row:
        return None
    return {"name": row.pop("name"), **{k: _n(v) for k, v in row.items()}}


def log_product_change(request, product, before):
    """Цена/наценка (price_change) и остаток (stock_change) — раздельные записи, только изменённые поля."""
    after = product_snapshot(product)
    price_keys = ("price", "purchase_price", "markup_percent")
    for action, keys in (("price_change", price_keys), ("stock_change", ("quantity",))):
        b = {k: before.get(k) for k in keys}
        a = {k: after.get(k) for k in keys}
        if _same_numbers(b, a):
            continue
        log_request(request, action, "product", product.pk, product.name, before=b, after=a, company=product.company)


def _same_numbers(b, a):
    from decimal import Decimal, InvalidOperation

    for k in a:
        x, y = b.get(k), a.get(k)
        if x == y:
            continue
        try:
            if Decimal(str(x)) == Decimal(str(y)):
                continue
        except (InvalidOperation, TypeError):
            pass
        return False
    return True


def user_rights(u):
    out = {f.name: getattr(u, f.name) for f in u._meta.concrete_fields if f.name.startswith("can_")}
    out["role"] = getattr(u, "role", None)
    out["custom_role_id"] = str(u.custom_role_id) if getattr(u, "custom_role_id", None) else None
    return {k: (v if isinstance(v, (bool, str, type(None), int)) else str(v)) for k, v in out.items()}
