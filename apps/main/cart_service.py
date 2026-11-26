from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.construction.models import Cashbox, CashShift
from .models import Cart, CartItem, Sale, SaleItem, Product


def _money(x: Decimal | None) -> Decimal:
    return (x or Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class CartOwner:
    user_id: Optional[int] = None
    session_key: Optional[str] = None

    def validate(self):
        if not self.user_id and not self.session_key:
            raise ValidationError("Нужен user_id или session_key для владения корзиной.")


def _resolve_open_shift(*, cashbox: Cashbox, owner: CartOwner) -> Optional[CashShift]:
    """
    Если require_shift=True → смена обязательна (и должна быть OPEN).
    Если False → смена опциональна (вернём открытую, если есть).
    """
    if owner.user_id is None:
        # гость без user — смену не найдём
        if cashbox.require_shift:
            raise ValidationError("Эта касса требует открытую смену, но нет кассира (user).")
        return None

    qs = CashShift.objects.filter(
        cashbox_id=cashbox.id,
        cashier_id=owner.user_id,
        status=CashShift.Status.OPEN,
    ).order_by("-opened_at")

    shift = qs.first()
    if cashbox.require_shift and not shift:
        raise ValidationError("Эта касса требует открытую смену. Открой смену и повтори.")
    return shift


@transaction.atomic
def get_or_create_active_cart(
    *,
    cashbox: Cashbox,
    owner: CartOwner,
    order_discount_total: Decimal | None = None,
) -> Cart:
    """
    Возвращает активную корзину для (cashbox + user) или (cashbox + session_key).
    Гарантия: одна активная корзина на владельца внутри кассы.
    """
    owner.validate()

    # Лочим кассу — дешёвый и надёжный способ убрать гонки на создании корзины
    Cashbox.objects.select_for_update().filter(id=cashbox.id).values("id").first()

    # shift (опционально/обязательно)
    shift = _resolve_open_shift(cashbox=cashbox, owner=owner)

    filters = {"cashbox_id": cashbox.id, "status": Cart.Status.ACTIVE}
    if owner.user_id:
        filters["user_id"] = owner.user_id
        filters["session_key__isnull"] = True
    else:
        filters["user__isnull"] = True
        filters["session_key"] = owner.session_key

    cart = (
        Cart.objects
        .select_for_update()
        .filter(**filters)
        .order_by("-updated_at")
        .first()
    )

    if cart:
        # если включили require_shift и корзина legacy без shift — допривяжем (если можно)
        if cashbox.require_shift and not cart.shift_id:
            cart.shift = shift
            cart.save(update_fields=["shift", "updated_at"])

        if order_discount_total is not None:
            cart.order_discount_total = _money(order_discount_total)
            cart.save(update_fields=["order_discount_total", "updated_at"])
            cart.recalc()

        return cart

    cart = Cart.objects.create(
        cashbox=cashbox,
        company=cashbox.company,
        branch=cashbox.branch,
        user_id=owner.user_id,
        session_key=None if owner.user_id else owner.session_key,
        shift=shift,
        status=Cart.Status.ACTIVE,
        order_discount_total=_money(order_discount_total or Decimal("0.00")),
    )
    cart.recalc()
    return cart


@transaction.atomic
def add_item_to_cart(
    *,
    cart: Cart,
    product: Product,
    quantity: int = 1,
    unit_price: Decimal | None = None,
    discount_total: Decimal | None = None,
) -> CartItem:
    """
    Добавляет/обновляет позицию в корзине.
    unit_price — базовая цена; discount_total — скидка на строку (вычитается из базы).
    Можно передать оба: final = (unit_price или product.price) - discount_total/qty.
    """
    if cart.status != Cart.Status.ACTIVE:
        raise ValidationError("Корзина не активна.")

    if not cart.cashbox_id:
        raise ValidationError("Активная корзина должна быть привязана к кассе.")

    if cart.cashbox.require_shift and not cart.shift_id:
        raise ValidationError("Эта касса требует смену. Нельзя добавлять без shift.")

    if quantity < 1:
        raise ValidationError("quantity минимум 1.")

    if unit_price is not None and unit_price < 0:
        raise ValidationError("unit_price должна быть ≥ 0.")
    if discount_total is not None and discount_total < 0:
        raise ValidationError("discount_total должна быть ≥ 0.")

    # лочим корзину и строку, чтобы 2 клика не раздвоили позиции
    Cart.objects.select_for_update().filter(id=cart.id).values("id").first()

    if cart.company_id != product.company_id:
        raise ValidationError("Товар другой компании.")
    if getattr(product, "branch_id", None) is not None and product.branch_id != cart.branch_id:
        raise ValidationError("Товар другого филиала и не глобальный.")

    # unit_price — база, line_discount — скидка на строку (хранятся отдельно)
    if unit_price is not None:
        base_price = _money(unit_price)
    else:
        if getattr(cart, "is_wholesale", False):
            raw_wholesale = getattr(product, "wholesale_price", None)
            raw_retail = getattr(product, "price", None)
            chosen = raw_wholesale if raw_wholesale not in (None, 0, "0") else raw_retail
            base_price = _money(chosen or Decimal("0.00"))
        else:
            base_price = _money(product.price or Decimal("0.00"))
    line_disc = _money(discount_total) if discount_total is not None else Decimal("0.00")

    # Со скидкой можно продавать ниже закупочной
    if line_disc <= 0 and quantity:
        min_price = _money(getattr(product, "purchase_price", None) or Decimal("0.00"))
        effective = base_price - (line_disc / int(quantity))
        if effective < min_price:
            raise ValidationError(
                f"Цена продажи не может быть ниже закупочной ({min_price})."
            )

    item = (
        CartItem.objects
        .select_for_update()
        .filter(cart_id=cart.id, product_id=product.id)
        .first()
    )

    if item:
        item.quantity = int(item.quantity or 0) + int(quantity)
        update_f = ["quantity"]
        if unit_price is not None:
            item.unit_price = base_price
            update_f.append("unit_price")
        if discount_total is not None:
            item.manual_discount = item.manual_discount_base() + line_disc
            item.line_discount = item.manual_discount
            update_f += ["line_discount", "manual_discount"]
        item.save(update_fields=update_f)
    else:
        item = CartItem.objects.create(
            cart=cart,
            company=cart.company,
            branch=cart.branch,
            product=product,
            quantity=quantity,
            unit_price=base_price,
            line_discount=line_disc,
            manual_discount=line_disc,
        )

    cart.recalc()
    return item


@transaction.atomic
def abandon_cart(*, cart: Cart) -> Cart:
    if cart.status != Cart.Status.ACTIVE:
        return cart
    Cart.objects.select_for_update().filter(id=cart.id).values("id").first()
    cart.status = Cart.Status.ABANDONED
    cart.save(update_fields=["status", "updated_at"])
    return cart
