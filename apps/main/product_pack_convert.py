"""
Перевод остатка товара в упаковки — одной атомарной операцией (вопрос кассы 11.11).

POST main/products/<id>/convert-to-packages/
  {"quantity_in_package": "10", "unit": "упак", "package_name": "Штука", "expected_quantity": "100",
   "allow_rounding": false, "idempotency_key": "<uuid>"}

Было: учётная единица — штука, остаток 100 шт, цена 15.
Стало: учётная единица — упаковка (по 10 шт), остаток 10 упак., цена 150; у товара появляется упаковка
«штука» (10 в упаковке, цена штуки 15), поэтому поштучная продажа на кассе продолжает работать.

Под блокировкой строки товара, поэтому продажи, прошедшие до блокировки, учтены, а после — ждут.
`expected_quantity` (необязательно) — защита от устаревшего экрана: если остаток уже другой, 409 stock_changed.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response

from core.exceptions import ApiError

_MAX_PRICE = Decimal("9999999.999")


def _dec(value, field):
    try:
        d = Decimal(str(value))
    except Exception:
        raise ApiError("validation_error", "Некорректное число.", status_code=400, fields={field: ["Некорректное число."]})
    return d


def convert_stock_to_packages(view, request, pk):
    from apps.main.cache_utils import invalidate_cache_pattern
    from apps.main.models import (Product, ProductPackage, ProductVariant, Sale, SaleItem, StockMovement,
                                  record_stock_movement)
    from apps.construction import audit as admin_audit

    data = request.data or {}
    n = _dec(data.get("quantity_in_package"), "quantity_in_package")
    if n <= 1:
        raise ApiError("validation_error", "В упаковке должно быть больше одной штуки.", status_code=400,
                       fields={"quantity_in_package": ["Должно быть > 1."]})
    unit = (data.get("unit") or "упак").strip()[:32] or "упак"
    pkg_name = (data.get("package_name") or "Штука").strip()[:64] or "Штука"
    allow_rounding = str(data.get("allow_rounding", "")).lower() in ("1", "true", "yes", "да")

    with transaction.atomic():
        qs = view._filter_qs_company_branch(Product.objects.select_for_update(of=("self",)))
        p = get_object_or_404(qs, pk=pk)
        before_audit = admin_audit.product_values(p.pk)
        if p.is_weight:
            raise ApiError("weight_product_not_supported", "Весовой товар нельзя перевести в упаковки.", status_code=409)
        if ProductVariant.objects.filter(product=p).exists():
            raise ApiError("variants_not_supported", "У товара есть варианты — переведите остаток вручную.", status_code=409)
        if p.packages.exists():
            raise ApiError("already_has_packages", "У товара уже есть упаковки — повторный перевод удвоил бы цену.", status_code=409)
        if p.promotion_tiers.exists():
            raise ApiError("has_promotion_tiers", "У товара есть ступени акции — сначала уберите их.", status_code=409)
        if SaleItem.objects.filter(product=p, sale__status=Sale.Status.NEW).exists():
            raise ApiError("product_in_open_cart", "Товар лежит в открытой корзине кассы — закройте корзины и повторите.",
                           status_code=409)

        q_old = Decimal(str(p.quantity or 0))
        if data.get("expected_quantity") not in (None, ""):
            exp = _dec(data.get("expected_quantity"), "expected_quantity")
            if exp != q_old:
                raise ApiError("stock_changed", "Остаток уже изменился (идут продажи). Обновите товар и повторите.",
                               status_code=409, top={"current_quantity": str(q_old)})
        q_new = (q_old / n).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        if q_new * n != q_old and not allow_rounding:
            raise ApiError("not_divisible", f"Остаток {q_old} не делится на {n} без остатка.", status_code=400,
                           top={"quantity": str(q_old), "remainder": str(q_old - (q_old / n).to_integral_value(rounding="ROUND_FLOOR") * n)})

        old_price = Decimal(str(p.price or 0))
        new_price = (old_price * n).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        new_wholesale = (Decimal(str(p.wholesale_price or 0)) * n).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        new_purchase = (Decimal(str(p.purchase_price or 0)) * n)
        if new_price > _MAX_PRICE or new_wholesale > _MAX_PRICE:
            raise ApiError("price_overflow", "Цена за упаковку выходит за допустимый предел (9 999 999,999).", status_code=400)
        new_min = (Decimal(str(p.minimum_quantity or 0)) / n).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        old_unit = (p.unit or "шт")

        Product.objects.filter(pk=p.pk).update(
            quantity=q_new, minimum_quantity=new_min, price=new_price, wholesale_price=new_wholesale,
            purchase_price=new_purchase.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if p.purchase_price is not None else None,
            unit=unit, updated_at=timezone.now(),
        )
        ProductPackage.objects.create(
            product=p, company=p.company, branch=p.branch, name=pkg_name, quantity_in_package=n, unit=old_unit,
            piece_unit_price=old_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        record_stock_movement(
            company=p.company, branch=p.branch, type=StockMovement.Type.ADJUSTMENT, object_id=p.pk,
            product_name=p.name, qty_before=q_old, change=q_new - q_old, qty_after=q_new,
            created_by=request.user,
            comment=f"Перевод остатка в упаковки: {n} {old_unit} в 1 {unit}",
        )
        p.refresh_from_db()
        try:
            admin_audit.log_product_change(request, p, before_audit)
        except Exception:
            pass
    invalidate_cache_pattern(f"products:list:{p.company_id}:")
    from apps.main.serializers import ProductSerializer

    out = ProductSerializer(p, context={"request": request}).data
    out["converted"] = {"factor": str(n), "quantity_before": str(q_old), "quantity_after": str(q_new),
                        "unit_before": old_unit, "unit_after": unit, "price_before": str(old_price),
                        "price_after": str(new_price)}
    return Response(out)
