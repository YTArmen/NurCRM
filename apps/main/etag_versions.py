"""
etag2011 (20.11.2026, просьба Бекназара): версия + ETag + If-Match для карточки товара и settings/*.

Карточка товара  GET/PATCH/PUT/DELETE main/products/<id>/
  GET → "version" (мс updated_at) и заголовок ETag: "<version>"; If-None-Match совпал → 304.
  PATCH/PUT/DELETE с If-Match: <version> (или "version" в теле) не совпал → 409 version_conflict {version: текущая}.
  Без If-Match — как раньше (старые клиенты не ломаются).
settings/stock/, settings/fiscal/ — то же: version (строка-хэш содержимого) + ETag, If-Match на PUT/PATCH.
main/app-settings/<app>/ — version/ETag/If-Match были и раньше (числовая версия).
"""
import hashlib
import json

from rest_framework.response import Response

from core.http_cache import etag_matches


def _bare(v):
    v = (v or "").strip()
    if v.startswith("W/"):
        v = v[2:]
    return v.strip().strip('"')


def _conflict(cur):
    return Response({"code": "version_conflict", "detail": "Данные изменили в другом месте. Обновите и повторите.",
                     "version": cur}, status=409)


def _wanted(request):
    h = _bare(request.headers.get("If-Match"))
    if h:
        return h
    d = request.data if isinstance(getattr(request, "data", None), dict) else {}
    v = d.get("version")
    return str(v) if v not in (None, "") else ""


def _not_modified(request, etag):
    if etag_matches(request.headers.get("If-None-Match"), etag):
        r = Response(status=304)
        r["ETag"] = etag
        return r
    return None


# ------------------------------------------------------------------ карточка товара

def _product_version(obj):
    u = getattr(obj, "updated_at", None)
    return str(int(u.timestamp() * 1000)) if u else "0"


def wrap_product_view(cls):
    orig_retrieve, orig_update, orig_destroy = cls.retrieve, cls.update, cls.destroy

    def retrieve(self, request, *a, **kw):
        resp = orig_retrieve(self, request, *a, **kw)
        try:
            if resp.status_code == 200 and isinstance(resp.data, dict):
                ver = _product_version(self.get_object())
                resp.data["version"] = ver
                etag = f'"{ver}"'
                nm = _not_modified(request, etag)
                if nm is not None:
                    return nm
                resp["ETag"] = etag
        except Exception:  # noqa: BLE001
            pass
        return resp

    def _check(self, request):
        want = _wanted(request)
        if not want:
            return None
        cur = _product_version(self.get_object())
        return None if want == cur else _conflict(cur)

    def update(self, request, *a, **kw):
        bad = _check(self, request)
        if bad is not None:
            return bad
        resp = orig_update(self, request, *a, **kw)
        try:
            if resp.status_code < 300 and isinstance(resp.data, dict):
                from apps.main.models import Product

                obj = Product.objects.filter(pk=kw.get("pk") or resp.data.get("id")).only("updated_at").first()
                if obj is not None:
                    ver = _product_version(obj)
                    resp.data["version"] = ver
                    resp["ETag"] = f'"{ver}"'
        except Exception:  # noqa: BLE001
            pass
        return resp

    def destroy(self, request, *a, **kw):
        bad = _check(self, request)
        if bad is not None:
            return bad
        return orig_destroy(self, request, *a, **kw)

    cls.retrieve, cls.update, cls.destroy = retrieve, update, destroy


# ------------------------------------------------------------------ settings/*

def _hash_version(data):
    d = {k: v for k, v in (data or {}).items() if k != "version"} if isinstance(data, dict) else data
    raw = json.dumps(d, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def wrap_settings_view(cls):
    orig_get = cls.get

    def _current(self, request):
        r = orig_get(self, request)
        return _hash_version(r.data) if r.status_code == 200 else None

    def get(self, request, *a, **kw):
        resp = orig_get(self, request, *a, **kw)
        if resp.status_code == 200 and isinstance(resp.data, dict):
            ver = _hash_version(resp.data)
            resp.data["version"] = ver
            etag = f'"{ver}"'
            nm = _not_modified(request, etag)
            if nm is not None:
                return nm
            resp["ETag"] = etag
        return resp

    cls.get = get
    for name in ("put", "patch"):
        orig = getattr(cls, name, None)
        if orig is None:
            continue

        def make(orig):
            def handler(self, request, *a, **kw):
                want = _wanted(request)
                if want:
                    cur = _current(self, request)
                    if cur is not None and want != cur:
                        return _conflict(cur)
                resp = orig(self, request, *a, **kw)
                try:
                    if resp.status_code < 300:
                        cur = _current(self, request)
                        if cur and isinstance(resp.data, dict):
                            resp.data["version"] = cur
                        if cur:
                            resp["ETag"] = f'"{cur}"'
                except Exception:  # noqa: BLE001
                    pass
                return resp
            return handler
        setattr(cls, name, make(orig))


def install():
    from apps.main.fiscal_kassa import FiscalSettingsAPIView
    from apps.main.stock_policy import StockSettingsAPIView
    from apps.main.views import ProductRetrieveUpdateDestroyAPIView

    wrap_product_view(ProductRetrieveUpdateDestroyAPIView)
    wrap_settings_view(StockSettingsAPIView)
    wrap_settings_view(FiscalSettingsAPIView)
