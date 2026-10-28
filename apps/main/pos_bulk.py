"""
ТЗ Н-15 (п. 7): быстрые операции над корзиной кассы.

    POST /api/main/pos/sales/{id}/items/bulk/   — добавить много позиций одним запросом (до 1000)
    POST /api/main/pos/carts/{id}/clear/        — очистить корзину: позиции И скидки чека

{id} — id корзины (как у add-item). Всё или ничего: при отказе ни одна позиция не добавляется,
в ответе `code` и `errors[]` с `index` (с 0), как у pos/checkout/.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.kassa_views import MAX_CHECKOUT_ITEMS, _line_error
from core.api_compat import is_strict
from apps.main.models import Cart, CartItem, Product, ProductPackage, ProductVariant
from apps.main.pos_utils import (
    cart_item_stock_consume_units,
    line_qty_consume_units,
    log_cart_item_deletion,
    qty3,
)
from apps.main.pos_views import (
    MarketCashierOnlyMixin,
    _cart_response,
    _discount_limit_error,
    _is_cart_deletion_authorized,
    _is_owner_like,
    _line_discount_from_request,
    _line_discount_over_limit,
    _q2,
    default_unit_price_for_package,
)
from core.exceptions import ApiError


def _first(d, *names):
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return None


def _dec(value, idx, field):
    try:
        return Decimal(str(value).replace(",", "."))
    except Exception:  # noqa: BLE001
        raise _line_error("validation_error", idx, f"Поле {field}: ожидается число.")


class SaleItemsBulkAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        cart = get_object_or_404(
            Cart.objects.select_related("company", "branch", "user", "shift").select_for_update(),
            id=pk, company=request.user.company, status=Cart.Status.ACTIVE,
        )
        raw = request.data.get("items") if hasattr(request.data, "get") else None
        if not isinstance(raw, list) or not raw:
            raise ApiError("validation_error", "Передайте непустой список items.", fields={"items": ["Обязательное поле."]},
                           top={"items": ["Обязательное поле."]})
        if len(raw) > MAX_CHECKOUT_ITEMS:
            raise ApiError("too_many_items", f"Не больше {MAX_CHECKOUT_ITEMS} позиций за запрос.",
                           top={"limit": MAX_CHECKOUT_ITEMS, "count": len(raw),
                                "errors": [{"index": None, "code": "too_many_items"}]})

        cid = cart.company_id
        pids = {str(_first(i, "product_id", "product")) for i in raw if isinstance(i, dict) and not i.get("custom")}
        pids.discard("None")
        products = {str(p.id): p for p in Product.objects.filter(company_id=cid, id__in=pids)}
        pkg_ids = {str(i["sale_package_id"]) for i in raw if isinstance(i, dict) and i.get("sale_package_id")}
        packages = {str(p.id): p for p in ProductPackage.objects.filter(company_id=cid, id__in=pkg_ids)}
        vids = {str(_first(i, "variant_id", "variant")) for i in raw if isinstance(i, dict) and _first(i, "variant_id", "variant")}
        variants = {
            str(v.id): v for v in ProductVariant.objects.filter(company_id=cid, id__in=vids).select_related("product")
        }

        is_admin = getattr(request.user, "role", None) in ("owner", "admin")
        can_minus = bool(request.data.get("allow_minus")) and _is_owner_like(request.user)
        max_dp = cart.company.max_discount_percent

        # текущее содержимое корзины: слияние одинаковых строк и учёт остатка
        cart_items = list(CartItem.objects.filter(cart=cart, product__isnull=False).select_related("sale_package"))
        merged = {(ci.product_id, ci.sale_package_id, ci.variant_id): ci for ci in cart_items}
        touched = {}
        used = defaultdict(lambda: Decimal("0"))
        for ci in cart_items:
            used[ci.product_id] += cart_item_stock_consume_units(ci)

        new_rows, added = [], []
        neg_discount = Decimal("0")
        for idx, it in enumerate(raw):
            if not isinstance(it, dict):
                raise _line_error("validation_error", idx, "Позиция должна быть объектом.")
            qty = qty3(_dec(_first(it, "quantity", "qty") or "1", idx, "quantity"))
            if qty <= 0:
                raise _line_error("validation_error", idx, "Количество должно быть больше нуля.")
            if it.get("custom"):
                name = str(it.get("name") or "").strip()
                price_raw = _first(it, "unit_price", "price")
                if not name or price_raw is None:
                    raise _line_error("validation_error", idx, "Для своей позиции нужны name и price.")
                price = _q2(_dec(price_raw, idx, "price"))
                if price < 0:
                    # то же правило, что в pos/checkout/ (Б-02): новый клиент — 400, старая касса —
                    # «Расход» проводится скидкой на чек
                    if is_strict(request, cart.company):
                        raise _line_error("negative_price", idx, "Цена своей позиции не может быть отрицательной.")
                    neg_discount += _q2(-price * qty)
                    added.append(("skip", None, None))
                    continue
                new_rows.append(CartItem(company_id=cid, branch_id=cart.branch_id, cart=cart, product=None,
                                         custom_name=name, unit_price=price, quantity=qty))
                added.append(("new", len(new_rows) - 1, None))
                continue

            product = products.get(str(_first(it, "product_id", "product")))
            if product is None:
                raise _line_error("product_not_found", idx, "Товар не найден.")
            pkg = None
            if it.get("sale_package_id"):
                pkg = packages.get(str(it["sale_package_id"]))
                if pkg is None or pkg.product_id != product.id:
                    raise _line_error("package_not_found", idx, "Упаковка не найдена.")
            variant = None
            vkey = _first(it, "variant_id", "variant")
            if vkey:
                variant = variants.get(str(vkey))
                if variant is None or variant.product_id != product.id:
                    raise _line_error("variant_not_found", idx, "Вариант не найден.")
            up_raw = _first(it, "unit_price", "price")
            unit_price = _q2(_dec(up_raw, idx, "unit_price")) if up_raw is not None else None
            if unit_price is None and variant is not None and variant.price is not None:
                unit_price = variant.price
            if unit_price is not None and unit_price < 0:
                raise _line_error("negative_price", idx, "Цена не может быть отрицательной.")
            d_total = _first(it, "discount_total", "discount")
            d_pct = it.get("discount_percent")
            base_price = _q2(unit_price) if unit_price is not None else _q2(default_unit_price_for_package(product, pkg))
            disc = _line_discount_from_request(
                base_price, qty,
                _dec(d_total, idx, "discount_total") if d_total is not None else None,
                _dec(d_pct, idx, "discount_percent") if d_pct is not None else None,
            )
            if not is_admin and _line_discount_over_limit(base_price, qty, disc, max_dp):
                body = _discount_limit_error(max_dp)
                body["errors"] = [{"index": idx, "code": "discount_limit_exceeded", "detail": body["detail"],
                                   "max_discount_percent": str(max_dp)}]
                return Response(body, status=status.HTTP_400_BAD_REQUEST)
            if disc <= 0:
                min_price = _q2(Decimal(str(product.purchase_price or 0)))
                if pkg:
                    ipp = Decimal(str(pkg.quantity_in_package or 0))
                    min_price = _q2(min_price / ipp) if ipp > 0 else min_price
                if base_price < min_price:
                    raise _line_error("price_below_cost", idx,
                                      f"Цена продажи не может быть ниже закупочной ({min_price}).",
                                      min_price=str(min_price), price=str(base_price))
            if pkg is None:  # ТЗ 26.10 Б-52: шаг и минимум (под флагом hardware_2026_11)
                from apps.main.tz26.hw_units.services import check_line_quantity

                check_line_quantity(request, cart.company, product, qty, index=idx)
            units = line_qty_consume_units(qty, pkg)
            used[product.id] += units
            if product.kind != Product.Kind.SERVICE and not can_minus and used[product.id] > Decimal(str(product.quantity or 0)):
                raise _line_error(
                    "insufficient_stock", idx, "Недостаточно остатка.",
                    available=str(qty3(max(Decimal("0"), Decimal(str(product.quantity or 0)) - (used[product.id] - units)))),
                    requested=str(units),
                )
            key = (product.id, pkg.id if pkg else None, variant.id if variant else None)
            target = merged.get(key)
            if target is not None:
                target.quantity = qty3(target.quantity + qty)
                if unit_price is not None:
                    target.unit_price = base_price
                if d_total is not None or d_pct is not None:
                    target.manual_discount = target.manual_discount_base() + disc
                    target.line_discount = target.manual_discount
                if target.pk is not None and not target._state.adding and target in cart_items:
                    touched[target.pk] = target
                added.append(("merge", key, target))
            else:
                row = CartItem(company_id=cid, branch_id=cart.branch_id, cart=cart, product=product,
                               sale_package=pkg, variant=variant, quantity=qty, unit_price=base_price,
                               line_discount=disc, manual_discount=disc)
                new_rows.append(row)
                merged[key] = row
                added.append(("new", len(new_rows) - 1, None))

        if touched:
            CartItem.objects.bulk_update(
                list(touched.values()), ["quantity", "unit_price", "line_discount", "manual_discount"]
            )
        CartItem.objects.bulk_create(new_rows)
        if neg_discount > 0:
            cart.order_discount_percent = None
            cart.order_discount_total = _q2((cart.order_discount_total or Decimal("0")) + neg_discount)
            cart.save(update_fields=["order_discount_total", "order_discount_percent", "updated_at"])
        cart.recalc()
        resp = _cart_response(request, cart.id, status_code=status.HTTP_201_CREATED)
        if isinstance(resp.data, dict):
            resp.data["added_items"] = [
                {"index": i, "id": str((new_rows[ref] if kind == "new" else target).id) if kind != "skip" else None}
                for i, (kind, ref, target) in enumerate(added)
            ]
        return resp


class CartClearAPIView(MarketCashierOnlyMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, *args, **kwargs):
        cart = get_object_or_404(
            Cart.objects.select_for_update(), id=pk, company=request.user.company, status=Cart.Status.ACTIVE,
        )
        if not _is_cart_deletion_authorized(request):
            return Response({"detail": "Требуется код удаления или время проверки истекло"},
                            status=status.HTTP_403_FORBIDDEN)
        for item in CartItem.objects.filter(cart=cart).select_related("product"):
            log_cart_item_deletion(item=item, deleted_by=request.user)
        CartItem.objects.filter(cart=cart).delete()
        # скидки чека сбрасываем вместе с позициями
        cart.order_discount_total = Decimal("0.00")
        cart.order_discount_percent = None
        cart.save(update_fields=["order_discount_total", "order_discount_percent", "updated_at"])
        cart.recalc()
        return _cart_response(request, cart.id, status_code=status.HTTP_200_OK)
