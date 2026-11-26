"""
bc2111 (21.11.2026, просьба Бекназара): доп. штрихкод товара по одному, без замены всего списка.

POST   /api/main/products/<id>/barcodes/            {"barcode", "name"?, "quantity"?}  → 201 {product_id, version, alternate_barcodes}
DELETE /api/main/products/<id>/barcodes/<barcode>/                                     → 200 {product_id, version, alternate_barcodes}

  * If-Match: <version карточки> (необязательно) — не совпал → 409 version_conflict {version}.
  * Код занят другим товаром (основной или доп.) → 409 barcode_duplicate {product_id, name, code, source, barcode}.
  * Код уже есть у этого товара → 200 без изменений (повтор безопасен; name/quantity обновятся, если переданы).
  * Код совпадает с основным штрихкодом этого товара → 400 barcode_is_main.
  * quantity — штук в упаковке (целое > 0) или пусто; name — до 255 знаков; barcode — до 64.
Права — как у правки карточки (can_view_products + can_edit_products, PIN).
"""
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.permissions import read_only_guard
from apps.users.permissions_flags import flag_guard
from apps.users.pin import pin_guard
from core.exceptions import ApiError

from .etag_versions import _bare, _conflict, _product_version
from .models import Product, ProductAlternateBarcode
from .views import CompanyBranchRestrictedMixin, _filter_products_company_only


def duplicate_error(company_id, barcode, exclude_product_id, field="alternate_barcodes"):
    """409 barcode_duplicate, если код занят другим товаром компании (основной или доп.)."""
    other = (Product.objects.filter(company_id=company_id, barcode=barcode)
             .exclude(pk=exclude_product_id).only("id", "name", "code").first())
    src = "product"
    if other is None:
        alt = (ProductAlternateBarcode.objects.filter(company_id=company_id, barcode=barcode)
               .exclude(product_id=exclude_product_id).select_related("product").first())
        other, src = (alt.product, "alternate") if alt is not None else (None, None)
    if other is None:
        return None
    code = f" (код {other.code})" if other.code else ""
    how = "как основной" if src == "product" else "как дополнительный"
    text = f"Штрихкод «{barcode}» уже есть у товара «{other.name}»{code} {how}."
    return ApiError("barcode_duplicate", text, status_code=409,
                    data={"product_id": str(other.id), "name": other.name, "code": other.code or None,
                          "source": src, "barcode": barcode},
                    fields={field: [text]})


def _payload(product):
    product.refresh_from_db(fields=["updated_at"])
    rows = ProductAlternateBarcode.objects.filter(product=product).order_by("barcode")
    return {"product_id": str(product.pk), "version": _product_version(product),
            "alternate_barcodes": [{"barcode": a.barcode, "name": a.name, "quantity": a.quantity} for a in rows]}


def _touch(product, *codes):
    Product.objects.filter(pk=product.pk).update(updated_at=timezone.now())
    for c in codes:
        cache.delete(f"product_barcode:{product.company_id}:{c}")


@flag_guard("can_view_products")
@read_only_guard("can_edit_products")
@pin_guard()
class ProductBarcodesAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _product(self, request, pk):
        p = get_object_or_404(_filter_products_company_only(self, Product.objects.all()).select_for_update(), pk=pk)
        want = _bare(request.headers.get("If-Match"))
        if want and want != _product_version(p):
            return p, _conflict(_product_version(p))
        return p, None

    @transaction.atomic
    def post(self, request, pk, barcode=None):
        product, conflict = self._product(request, pk)
        if conflict is not None:
            return conflict
        d = request.data if hasattr(request.data, "get") else {}
        b = str(d.get("barcode") or "").strip()
        if not b:
            raise ApiError("invalid", "Укажите штрихкод.", fields={"barcode": ["Обязательное поле."]})
        if len(b) > 64:
            raise ApiError("invalid", "Штрихкод длиннее 64 знаков.", fields={"barcode": ["До 64 знаков."]})
        name = str(d.get("name") or "").strip()
        if len(name) > 255:
            raise ApiError("invalid", "Название длиннее 255 знаков.", fields={"name": ["До 255 знаков."]})
        qty = None
        raw = d.get("quantity")
        if raw not in (None, ""):
            try:
                val = float(raw)
                if val <= 0 or not val.is_integer():
                    raise ValueError
                qty = int(val)
            except (TypeError, ValueError):
                raise ApiError("invalid", "Количество в упаковке — целое число больше 0.",
                               fields={"quantity": ["Целое число > 0."]})
        if b == (product.barcode or "").strip():
            raise ApiError("barcode_is_main", f"«{b}» — основной штрихкод этого товара.", fields={"barcode": ["Это основной штрихкод."]})
        err = duplicate_error(product.company_id, b, product.pk, field="barcode")
        if err is not None:
            raise err
        row = ProductAlternateBarcode.objects.filter(product=product, barcode=b).first()
        created = row is None
        if created:
            ProductAlternateBarcode.objects.create(product=product, company_id=product.company_id,
                                                   barcode=b, name=name, quantity=qty)
        else:
            fields = []
            if "name" in d and row.name != name:
                row.name, fields = name, fields + ["name"]
            if "quantity" in d and row.quantity != qty:
                row.quantity, fields = qty, fields + ["quantity"]
            if fields:
                row.save(update_fields=fields)
            elif not created:
                return Response(_payload(product), status=200)
        _touch(product, b)
        return Response(_payload(product), status=201 if created else 200)

    @transaction.atomic
    def delete(self, request, pk, barcode=None):
        product, conflict = self._product(request, pk)
        if conflict is not None:
            return conflict
        b = str(barcode or "").strip()
        n, _ = ProductAlternateBarcode.objects.filter(product=product, barcode=b).delete()
        if not n:
            raise ApiError("not_found", f"У товара нет доп. штрихкода «{b}».", status_code=404)
        _touch(product, b)
        return Response(_payload(product))
