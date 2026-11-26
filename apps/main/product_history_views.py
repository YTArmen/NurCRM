"""21.11.2026 (hist2111): история товара и журнал товаров компании.

GET /api/main/products/<pk>/change-history/?page=     — всё, что было с товаром (даже удалённым): создание, каждое
                                                 изменение поля «было → стало», штрихкоды, фото, удаление,
                                                 возврат из корзины; кто, откуда (веб/касса/приложение/загрузка), IP.
GET /api/main/product-history/?op=&q=&user=&source=&date_from=&date_to=&page=
                                               — журнал по всем товарам компании (op: create|update|delete|import|restore).
Видят владелец/админ и те, кому открыт раздел «Товары».
"""
import uuid
from datetime import datetime, time, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models_product_history import ProductHistory

PAGE = 50
# legacy-журнал (до 21.11 история полей не писалась) — показываем записи до этого момента
HIST_START = datetime(2026, 11, 21, 0, 0, tzinfo=timezone.get_fixed_timezone(0))


def _owner_like(u):
    return bool(getattr(u, "is_superuser", False) or getattr(u, "role", None) in ("owner", "admin")
                or getattr(u, "owned_company_id", None))


def can_delete_products(u):
    """Удалять товары: владелец/админ — всегда; остальные — только явное право can_delete_products=True."""
    return _owner_like(u) or getattr(u, "can_delete_products", None) is True


def _company(u):
    return getattr(u, "company", None) or getattr(u, "owned_company", None)


def _check(request):
    u = request.user
    if not (_owner_like(u) or getattr(u, "can_view_products", False)):
        raise PermissionDenied({"detail": "Нет доступа к истории товаров.", "code": "permission_denied"})
    c = _company(u)
    if c is None:
        raise PermissionDenied("У пользователя не настроена компания.")
    return c


def source_of(source, client, ua):
    """Откуда пришло изменение — понятным словом."""
    s, ua = (source or ""), (ua or "")
    low = ua.lower()
    if s == "system":
        return "system", "Система (автоматически)"
    if "mass-incoming" in s or "mass_incoming" in s or "import" in s:
        kind = "import"
    elif client:
        return "kassa", f"Касса NurMarket {client}".strip()
    elif any(k in low for k in ("okhttp", "expo", "dalvik", "cfnetwork", "reactnative")):
        kind = "app"
    elif "nurmarket" in low or "avalonia" in low or ".net" in low or "httpclient" in low:
        kind = "kassa"
    elif "mozilla" in low:
        kind = "web"
    elif s.startswith("trash") or "restore" in s:
        kind = "web"
    else:
        kind = "api"
    label = {"import": "Загрузка из файла / приход", "app": "Мобильное приложение", "kassa": "Касса NurMarket",
             "web": "Веб (браузер)", "api": "API / интеграция"}[kind]
    return kind, label


def _users(ids):
    from django.contrib.auth import get_user_model

    ids = {i for i in ids if i}
    if not ids:
        return {}
    out = {}
    for u in get_user_model().objects.filter(pk__in=ids).only("id", "first_name", "last_name", "email"):
        out[str(u.pk)] = (f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email)
    return out


def _hist_row(h):
    kind, label = source_of(h.source, h.client, h.ua)
    ch = h.changes or {}
    if h.op in ("create", "delete"):
        ch = {k: v for k, v in ch.items() if v not in (None, "", [], {}) and k not in ("company_id", "id")}
    return {
        "id": f"h{h.id}", "at": h.created_at, "op": h.op, "product_id": str(h.product_id), "product_name": h.product_name,
        "changes": ch, "user": str(h.user_id) if h.user_id else None,
        "source": kind, "source_label": label, "endpoint": h.source, "ip": h.ip, "client": h.client,
    }


def _audit_row(a, op):
    after = a.after or {}
    before = a.before or {}
    changes = {k: [before.get(k), after.get(k)] for k in set(before) | set(after)} if op == "update" else after
    return {
        "id": f"a{a.id}", "at": a.created_at, "op": op, "product_id": a.object_id, "product_name": a.object_name,
        "changes": changes, "user": str(a.user_id) if a.user_id else None,
        "source": "web" if a.action != "import" else "import",
        "source_label": "Загрузка из файла / приход" if a.action == "import" else "Журнал действий",
        "endpoint": a.action, "ip": a.ip, "client": None,
    }


_LEGACY_OP = {"price_change": "update", "name_change": "update", "delete": "delete", "restore": "restore"}


def _day(d, end=False):
    if not d:
        return None
    t = timezone.make_aware(datetime.combine(d, time.min))
    return t + timedelta(days=1) if end else t


def _page(request):
    try:
        return max(int(request.query_params.get("page") or 1), 1)
    except ValueError:
        return 1


_REF_MODELS = {"category_id": ("main", "ProductCategory"), "brand_id": ("main", "ProductBrand"), "client_id": ("main", "Client")}


def _ref_names(rows):
    """category_id/brand_id/client_id в журнале — названиями вместо id."""
    from django.apps import apps as _apps

    want = {k: set() for k in _REF_MODELS}
    for r in rows:
        for k in _REF_MODELS:
            v = r["changes"].get(k)
            vals = v if isinstance(v, list) else [v]
            want[k].update(str(x) for x in vals if x)
    for k, ids in want.items():
        if not ids:
            continue
        M = _apps.get_model(*_REF_MODELS[k])
        try:
            names = {str(o.pk): (getattr(o, "name", None) or getattr(o, "full_name", None) or str(o))
                     for o in M._base_manager.filter(pk__in=list(ids))}
        except Exception:
            continue
        for r in rows:
            v = r["changes"].get(k)
            if isinstance(v, list):
                r["changes"][k] = [names.get(str(x), x) if x else x for x in v]
            elif v:
                r["changes"][k] = names.get(str(v), v)


def _finish(rows, page, total=None):
    _ref_names(rows)
    names = _users(r["user"] for r in rows)
    for r in rows:
        r["user_name"] = names.get(r["user"]) if r["user"] else None
    return Response({"count": total if total is not None else len(rows), "page": page, "page_size": PAGE, "results": rows})


class ProductHistoryAPIView(APIView):
    """GET /api/main/products/<pk>/change-history/"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        from apps.construction.models import AuditLog
        from apps.main.models import Product

        company = _check(request)
        pid = str(pk)
        hist = list(ProductHistory.objects.filter(product_id=pk, company_id=company.pk).order_by("-id")[:1000])
        rows = [_hist_row(h) for h in hist]
        # старые записи журнала (до 21.11) + возвраты из корзины (их триггер видит как «создан»)
        for a in AuditLog.objects.filter(company=company, object_type="product", object_id=pid).order_by("-created_at")[:500]:
            op = _LEGACY_OP.get(a.action)
            if op is None:
                continue
            if op == "restore" or a.created_at < HIST_START:
                rows.append(_audit_row(a, op))
        has_create = any(r["op"] == "create" for r in rows)
        p = Product.objects.filter(pk=pk, company_id=company.pk).only("id", "name", "created_at", "created_by_id").first()
        if p is None and not rows:
            from rest_framework.exceptions import NotFound
            raise NotFound("Товар не найден.")
        if p is not None and not has_create and p.created_at:
            rows.append({
                "id": "c0", "at": p.created_at, "op": "create", "product_id": pid, "product_name": p.name, "changes": {},
                "user": str(p.created_by_id) if p.created_by_id else None, "source": "unknown",
                "source_label": "Создан (до 21.11.2026 подробности не записывались)", "endpoint": "", "ip": None, "client": None,
            })
        rows.sort(key=lambda r: r["at"], reverse=True)
        page = _page(request)
        total = len(rows)
        return _finish(rows[(page - 1) * PAGE: page * PAGE], page, total)


class CompanyProductHistoryAPIView(APIView):
    """GET /api/main/product-history/"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.construction.models import AuditLog

        company = _check(request)
        q = request.query_params
        op = (q.get("op") or "").strip()
        df, dt = parse_date(q.get("date_from") or ""), parse_date(q.get("date_to") or "")
        user = (q.get("user") or "").strip()
        if user:
            try:
                uuid.UUID(user)
            except ValueError:
                raise ValidationError({"user": "Неверный идентификатор."})
        text = (q.get("q") or "").strip()
        page = _page(request)

        if op in ("import", "restore"):
            qs = AuditLog.objects.filter(company=company, action=op,
                                         object_type="product_import" if op == "import" else "product")
            if df:
                qs = qs.filter(created_at__gte=_day(df))
            if dt:
                qs = qs.filter(created_at__lt=_day(dt, end=True))
            if user:
                qs = qs.filter(user_id=user)
            if text:
                qs = qs.filter(object_name__icontains=text)
            total = qs.count()
            rows = [_audit_row(a, op) for a in qs.order_by("-created_at")[(page - 1) * PAGE: page * PAGE]]
            return _finish(rows, page, total)

        qs = ProductHistory.objects.filter(company_id=company.pk)
        if op == "update":
            qs = qs.exclude(op__in=("create", "delete"))
        elif op:
            qs = qs.filter(op=op)
        if df:
            qs = qs.filter(created_at__gte=_day(df))
        if dt:
            qs = qs.filter(created_at__lt=_day(dt, end=True))
        if user:
            qs = qs.filter(user_id=user)
        if text:
            qs = qs.filter(product_name__icontains=text)
        total = qs.count()
        rows = [_hist_row(h) for h in qs.order_by("-id")[(page - 1) * PAGE: page * PAGE]]
        return _finish(rows, page, total)


def log_import(request, *, rows, created, errors, ok, detail=""):
    """Журнал загрузок: одна запись на каждую пачку mass-incoming (файл, сколько строк, создано, ошибки)."""
    try:
        from apps.construction.audit import log_request

        data = request.data if isinstance(request.data, dict) else {}
        fname = str(data.get("import_file") or "")[:200]
        err_list = errors if isinstance(errors, list) else []
        log_request(
            request, "import", "product_import", str(data.get("import_run") or "")[:64],
            fname or "Массовый приход (без файла)",
            after={
                "ok": ok, "rows": rows, "created": created, "errors": len(err_list),
                "error_samples": [
                    {"index": e.get("index"), "detail": str(e.get("detail") or e.get("error") or "")[:200]}
                    for e in err_list[:20] if isinstance(e, dict)
                ],
                "detail": str(detail)[:300], "chunk": data.get("import_chunk"), "chunks": data.get("import_chunks"),
                "file": fname or None,
                "key": (request.headers.get("Idempotency-Key") or "")[:128],
            },
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("import log failed", exc_info=True)
