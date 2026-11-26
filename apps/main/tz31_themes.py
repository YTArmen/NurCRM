"""ТЗ кассы 05.11 п.33 / Б-48: GET /api/public/marketplace/themes/ — каталог тем и видов (без входа, кэш 5 минут)."""
from django.core.cache import cache
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import MarketplaceTheme

CACHE_KEY = "public:marketplace-themes:v1"
CACHE_TTL = 300
LANGS = ("ru", "ky", "en", "tr", "uz")


def build_catalog() -> list:
    out = []
    for t in MarketplaceTheme.objects.filter(is_active=True):
        title = t.title if isinstance(t.title, dict) else {}
        out.append({
            "id": t.slug,
            "kind": t.kind,
            "title": {lang: title.get(lang) or title.get("ru") or t.slug for lang in LANGS},
            "preview_url": t.preview_url,
            "price": f"{t.price:.2f}" if t.price is not None else None,
            "feature_code": t.feature_code,
            "version": t.version,
        })
    return out


class PublicMarketplaceThemesAPIView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        data = None
        try:
            data = cache.get(CACHE_KEY)
        except Exception:
            pass
        if data is None:
            data = build_catalog()
            try:
                cache.set(CACHE_KEY, data, CACHE_TTL)
            except Exception:
                pass
        resp = Response(data)
        resp["Cache-Control"] = f"public, max-age={CACHE_TTL}"
        return resp
