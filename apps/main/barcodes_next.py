"""
bcn2111 (21.11.2026, просьба Бекназара): выдача свободных внутренних EAN-13 сервером.

POST /api/main/barcodes/next/  {"count": 1..100}  → {"barcodes": ["3...", ...]}
  * первая цифра 1 или 3–9 (2 — весовые этикетки на кассе, не выдаём), без префикса 470, верная контрольная цифра;
  * свободен в компании: не занят основным штрихкодом товара, доп. штрихкодом, штрихкодом варианта или упаковки;
  * коды не резервируются: выдаются случайно, повтор маловероятен; при сохранении товара уникальность
    всё равно проверяется (409 barcode_duplicate).
"""
import secrets

from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError

from .views import CompanyBranchRestrictedMixin

FIRST = "13456789"
MAX_COUNT = 100


def ean13_check(first12: str) -> str:
    total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(first12))
    return str((10 - total % 10) % 10)


def _candidate():
    while True:
        body = secrets.choice(FIRST) + "".join(secrets.choice("0123456789") for _ in range(11))
        if body.startswith("470"):
            continue
        return body + ean13_check(body)


def taken_codes(company_id, codes):
    from apps.main.models import Product, ProductAlternateBarcode, ProductVariant
    from apps.main.tz26.hw_units.models import ProductSaleUnit

    codes = list(codes)
    out = set(Product.objects.filter(company_id=company_id, barcode__in=codes).values_list("barcode", flat=True))
    out |= set(ProductAlternateBarcode.objects.filter(company_id=company_id, barcode__in=codes).values_list("barcode", flat=True))
    out |= set(ProductVariant.objects.filter(company_id=company_id, barcode__in=codes).values_list("barcode", flat=True))
    out |= set(ProductSaleUnit.objects.filter(product__company_id=company_id, barcode__in=codes).values_list("barcode", flat=True))
    return out


class BarcodesNextAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = self._company()
        if company is None:
            raise ApiError("no_company", "Пользователь не привязан к компании.", status_code=403)
        raw = (request.data or {}).get("count", 1) if hasattr(request.data, "get") else 1
        try:
            count = int(raw)
        except (TypeError, ValueError):
            count = 0
        if not 1 <= count <= MAX_COUNT:
            raise ApiError("invalid", f"count — от 1 до {MAX_COUNT}.", fields={"count": [f"1…{MAX_COUNT}."]})
        result = []
        for _ in range(10):
            need = count - len(result)
            cand = {_candidate() for _ in range(need * 2)} - set(result)
            busy = taken_codes(company.id, cand)
            free = [c for c in cand if c not in busy]
            result += free[:need]
            if len(result) >= count:
                break
        return Response({"barcodes": result[:count]})
