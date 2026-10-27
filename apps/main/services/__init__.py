from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

import logging

from apps.main.models import Cart, CartItem, Sale, SaleItem, Product, ProductVariant
from apps.main.pos_utils import cart_item_stock_consume_units, money as pos_money
from apps.main.services.product_list_filters import apply_product_list_filters  # noqa: F401


class NotEnoughStock(Exception):
    # ТЗ Н-15: shortages — все нехватки чека [{product_id, variant_id, available, requested}] для errors[]
    def __init__(self, *args, shortages=None):
        super().__init__(*args)
        self.shortages = shortages or []


# ТЗ Н-15: был @transaction.atomic — savepoint на каждую строку чека, а функция ничего не пишет
def _performer_commission(item) -> Decimal:
    """Процент мастеру с суммы строки (процент задаётся у услуги)."""
    pct = getattr(getattr(item, "product", None), "performer_commission_percent", None)
    if not item.performer_id or not pct:
        return Decimal("0.00")
    line = (item.unit_price or Decimal("0")) * (item.quantity or Decimal("0")) - (item.line_discount or Decimal("0"))
    return pos_money(max(line, Decimal("0")) * Decimal(str(pct)) / Decimal("100"))


def checkout_cart(
    cart: Cart,
    department=None,
    allow_negative_stock: bool = False,
    *,
    payment_method=None,
    cash_received=None,
    payments=None,
    cash_amount=None,
    card_amount=None,
    card_method=None,
    prepayment_method=None,
    client=None,
    consultant=None,
    consultant_commission_enabled: bool = False,
    consultant_commission_percent=None,
    lenient_variants: bool = False,
) -> Sale:
    """
    Перенос корзины в Sale (статус NEW) и списание остатков.

    Если переданы ``payments`` или ``payment_method`` / ``cash_received``, в конце вызывается
    ``sale.mark_paid(...)`` (фискализация eKassa — в фоне после commit).
    """
    cart.recalc()

    items = list(
        cart.items.select_related("product", "sale_package", "variant", "sale_unit")
    )
    if not items:
        raise ValueError("Корзина пуста.")

    shift = getattr(cart, "shift", None)
    if not shift:
        raise ValueError("Нет смены. Сначала открой смену или передай кассу и создай смену.")

    cashbox = shift.cashbox
    branch = shift.branch

    prod_ids = [it.product_id for it in items if it.product_id]
    products = {p.id: p for p in Product.objects.select_for_update().filter(id__in=prod_ids)}

    consume_by_pid: dict = defaultdict(lambda: Decimal("0"))
    for it in items:
        if not it.product_id:
            continue
        if it.product_id not in products:
            raise ValueError("Товар позиции не найден.")
        try:
            item_consume = cart_item_stock_consume_units(it)
        except ValueError as e:
            raise ValueError(str(e)) from e
        consume_by_pid[it.product_id] += item_consume

    # Проверка обязательности варианта (ТЗ-10 п. 3.5 Вариант А)
    active_variant_pids = set(
        ProductVariant.objects.filter(product_id__in=prod_ids, is_active=True).values_list("product_id", flat=True)
    )
    for it in items:
        if lenient_variants:
            break  # ТЗ Н-15: офлайн-досылка — вариант не выбран, списываем с товара
        if it.product_id and it.product_id in active_variant_pids and not it.variant_id:
            p_name = products[it.product_id].name if it.product_id in products else "Товар"
            raise ValueError(f"Товар «{p_name}» имеет размеры/цвета. Выберите размер/цвет.")

    # Остаток по вариантам (размер/цвет)
    variant_ids = [it.variant_id for it in items if it.variant_id]
    variants = {v.id: v for v in ProductVariant.objects.select_for_update().filter(id__in=variant_ids)}
    consume_by_vid: dict = defaultdict(lambda: Decimal("0"))
    for it in items:
        if it.variant_id:
            if it.variant_id not in variants or variants[it.variant_id].product_id != it.product_id:
                raise ValueError("Вариант позиции не найден.")
            consume_by_vid[it.variant_id] += cart_item_stock_consume_units(it)
    if not allow_negative_stock:
        shortages, first_msg = [], None
        for vid, need in consume_by_vid.items():
            v = variants[vid]
            if need > Decimal(str(v.quantity or 0)):
                if first_msg is None:
                    first_msg = (
                        f"Недостаточно остатка «{products[v.product_id].name}» {v.size} {v.color}".strip()
                        + f". Требуется {need}, доступно {v.quantity}."
                    )
                shortages.append({"product_id": v.product_id, "variant_id": vid,
                                  "available": Decimal(str(v.quantity or 0)), "requested": need})
        for pid, need in consume_by_pid.items():
            p = products[pid]
            if getattr(p, "kind", None) == Product.Kind.SERVICE:
                continue
            have = Decimal(str(p.quantity or 0))
            if need > have:
                if first_msg is None:
                    first_msg = (
                        f"Недостаточно остатка для «{getattr(p, 'name', '') or p.id}». "
                        f"Требуется {need} (в учётных единицах склада), доступно {have}."
                    )
                shortages.append({"product_id": pid, "variant_id": None, "available": have, "requested": need})
        if shortages:
            raise NotEnoughStock(first_msg, shortages=shortages)

    comm_enabled = bool(consultant and consultant_commission_enabled)
    comm_pct = (
        Decimal(str(consultant_commission_percent))
        if (consultant and consultant_commission_percent not in (None, "", "null"))
        else None
    )
    comm_amount = Decimal("0.00")
    if consultant and comm_enabled and comm_pct is not None and comm_pct > 0:
        comm_amount = (cart.total * comm_pct / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    sale = Sale.objects.create(
        company=cart.company,
        branch=branch,
        user=shift.cashier,
        shift=shift,
        cashbox=cashbox,
        consultant=consultant if consultant else None,
        consultant_commission_enabled=comm_enabled,
        consultant_commission_percent=comm_pct,
        consultant_commission_amount=comm_amount,
        status=Sale.Status.NEW,
        subtotal=cart.subtotal,
        discount_total=cart.discount_total,
        tax_total=cart.tax_total,
        total=cart.total,
        created_at=timezone.now(),
    )

    sale_items = []
    for it in items:
        p = it.product
        name_snap = (getattr(p, "name", None) or getattr(it, "custom_name", None) or "Позиция")
        barcode_snap = getattr(p, "barcode", None) or ""
        pp = (p.purchase_price or Decimal("0.00")) if p else Decimal("0.00")
        if it.sale_package_id:
            ipp = Decimal(str(it.sale_package.quantity_in_package or 0))
            snap = pos_money(pp / ipp) if ipp > 0 else pos_money(pp)
        else:
            snap = pos_money(pp)
        if it.sale_unit_id:  # ТЗ 26.10 Б-53: себестоимость единицы продажи = базовая x factor
            snap = pos_money(pp * it.sale_unit.factor)
        sale_items.append(
            SaleItem(
                company=cart.company,
                branch=branch,
                sale=sale,
                product=p,
                is_custom=bool(p is None),
                name_snapshot=name_snap,
                barcode_snapshot=barcode_snap,
                unit_price=it.unit_price,
                quantity=it.quantity,
                line_discount=getattr(it, "line_discount", None) or Decimal("0.00"),
                manual_discount=getattr(it, "manual_discount", None) or Decimal("0.00"),
                discount_source=getattr(it, "discount_source", None) or "none",
                promotion_id=getattr(it, "promotion_id", None),
                sale_package_id=it.sale_package_id,
                purchase_price_snapshot=snap,
                price_manually_edited=bool(getattr(it, "price_manually_edited", False)),
                variant_id=it.variant_id,
                performer_id=it.performer_id,
                performer_commission_amount=_performer_commission(it),
                is_wholesale=bool(getattr(it, "is_wholesale", False) or getattr(cart, "is_wholesale", False)),
                sale_unit_id=it.sale_unit_id,  # ТЗ 26.10 Б-53
                base_quantity=(cart_item_stock_consume_units(it) if it.sale_unit_id else None),
            )
        )
    SaleItem.objects.bulk_create(sale_items)
    for vid, need in consume_by_vid.items():
        v = variants[vid]
        v.quantity = Decimal(str(v.quantity or 0)) - need
    if consume_by_vid:
        ProductVariant.objects.bulk_update([variants[vid] for vid in consume_by_vid], ["quantity"])

    changed = []
    low_stock = []
    for pid, qty_need in consume_by_pid.items():
        p = products[pid]
        if getattr(p, "kind", None) == Product.Kind.SERVICE:
            continue
        before = Decimal(str(p.quantity or 0))
        p.quantity = before - qty_need
        changed.append(p)
        threshold = getattr(p, "minimum_quantity", None)
        if threshold is not None and threshold > 0 and before > threshold >= p.quantity:
            low_stock.append(p)
    if low_stock:
        from apps.integrations.events import emit_event

        for p in low_stock:
            emit_event(
                p.company_id,
                "stock.low",
                {
                    "product": p.id,
                    "name": p.name,
                    "barcode": getattr(p, "barcode", None),
                    "quantity": p.quantity,
                    "minimum_quantity": p.minimum_quantity,
                    "branch": getattr(p, "branch_id", None),
                },
            )
    if changed:
        Product.objects.bulk_update(changed, ["quantity"])
        changed_ids = [p.id for p in changed if getattr(p, "id", None)]

        # ТЗ Н-31: одно событие stock_changed на всё списание (одна вставка в ленту)
        try:
            from apps.integrations.events import emit_event as _emit_stock
            _emit_stock(cart.company_id, "stock_changed", {
                "products": [str(i) for i in changed_ids][:200],
                "reason": "sale",
                "branch": getattr(cart, "branch_id", None),
            })
        except Exception:
            pass

        def _send_webhooks():
            from apps.main.services.webhooks import send_product_webhook

            # ТЗ Н-15: один запрос на все товары чека, а не по запросу на товар
            fresh = {p.pk: p for p in Product.objects.filter(pk__in=changed_ids)}
            for pid in changed_ids:
                try:
                    prod = fresh[pid]
                    send_product_webhook(prod, "product.updated")
                except Exception:
                    logging.getLogger("crm.webhooks").error(
                        "Failed to send product.updated webhook after checkout. product_id=%s",
                        pid,
                        exc_info=True,
                    )

        try:
            transaction.on_commit(_send_webhooks)
        except Exception:
            _send_webhooks()

    CartItem.objects.filter(cart=cart).delete()
    cart.status = Cart.Status.CHECKED_OUT
    cart.save(update_fields=["status", "updated_at"])

    if cart.company_id:
        try:
            from django.core.cache import cache
            cache.delete(f"tg_catalog_data:{cart.company_id}")
        except Exception:
            pass

    if getattr(cart, "shift_id", None) and sale.shift_id != cart.shift_id:
        sale.shift_id = cart.shift_id
        sale.save(update_fields=["shift"])

    if client is not None:
        sale.client = client
        sale.save(update_fields=["client"])

    if payments:
        sale.mark_paid(
            payments=payments,
            cash_received=cash_received,
            cash_amount=cash_amount,
            card_amount=card_amount,
            card_method=card_method,
            prepayment_method=prepayment_method,
        )
    elif payment_method is not None:
        sale.mark_paid(
            payment_method=payment_method,
            cash_received=cash_received,
            cash_amount=cash_amount,
            card_amount=card_amount,
            card_method=card_method,
            prepayment_method=prepayment_method,
        )

    # Автоматическое создание долговой сделки (ClientDeal) при продаже в долг
    debt_amt = Decimal("0.00")
    if sale.payment_method == Sale.PaymentMethod.DEBT:
        debt_amt = sale.debt_initial or (sale.total - sale.paid_now)
    elif payments:
        debt_amt = sum(
            (Decimal(str(p.get("amount") or 0)) for p in payments if p.get("method") == Sale.PaymentMethod.DEBT),
            Decimal("0.00"),
        )
    if sale.client_id and debt_amt > Decimal("0.00"):
        from apps.main.models import ClientDeal, DealInstallment
        from datetime import timedelta
        prepay_val = sale.paid_now or getattr(sale, "cash_received", Decimal("0.00")) or Decimal("0.00")
        if not ClientDeal.objects.filter(sale=sale).exists():
            unlinked_deal = (
                ClientDeal.objects.filter(
                    company=sale.company,
                    client=sale.client,
                    kind=ClientDeal.Kind.DEBT,
                    sale__isnull=True,
                    created_at__gte=timezone.now() - timedelta(seconds=60),
                )
                .order_by("-created_at")
                .first()
            )
            if unlinked_deal and (abs((unlinked_deal.amount or Decimal("0.00")) - debt_amt) <= Decimal("0.01") or unlinked_deal.amount == Decimal("0.00")):
                unlinked_deal.sale = sale
                if not unlinked_deal.amount or unlinked_deal.amount == Decimal("0.00"):
                    unlinked_deal.amount = sale.total
                if prepay_val > Decimal("0.00") and unlinked_deal.prepayment == Decimal("0.00"):
                    unlinked_deal.prepayment = prepay_val
                unlinked_deal.save(update_fields=["sale", "amount", "prepayment", "updated_at"])
                deal_obj = unlinked_deal
            else:
                deal_obj = ClientDeal.objects.create(
                    company=sale.company,
                    branch=sale.branch,
                    client=sale.client,
                    sale=sale,
                    title=f"Продажа в долг №{sale.doc_number or sale.id}",
                    kind=ClientDeal.Kind.DEBT,
                    amount=sale.total,
                    prepayment=prepay_val,
                    # Один взнос на всю сумму со сроком через 30 дней (ТЗ ч.12, 2.2):
                    # в графике v2 debt_days — число платежей, а не срок.
                    debt_days=1,
                    first_due_date=timezone.localdate() + timedelta(days=30),
                )
            if not DealInstallment.objects.filter(deal=deal_obj).exists():
                rem_inst = max(Decimal("0.00"), sale.total - prepay_val)
                if rem_inst > 0:
                    DealInstallment.objects.create(
                        deal=deal_obj,
                        amount=rem_inst,
                        due_date=timezone.localdate() + timedelta(days=30),
                        paid_amount=Decimal("0.00"),
                    )

    try:
        from apps.main.cache_utils import invalidate_cache_pattern
        invalidate_cache_pattern(f"analytics:market:{sale.company_id}:")
        invalidate_cache_pattern(f"products:list:{sale.company_id}:")
    except Exception:
        pass

    return sale


def _parse_kind(raw, Product):
    kind_raw = str(raw or Product.Kind.PRODUCT).strip().lower()
    kind_map = {
        "product": Product.Kind.PRODUCT,
        "товар": Product.Kind.PRODUCT,
        "service": Product.Kind.SERVICE,
        "услуга": Product.Kind.SERVICE,
        "bundle": Product.Kind.BUNDLE,
        "комплект": Product.Kind.BUNDLE,
    }
    return kind_map.get(kind_raw, Product.Kind.PRODUCT)


def _parse_bool_like(raw):
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "yes", "да", "kg", "кг", "weight", "вес")


def _parse_decimal(value, field_name):
    try:
        if value in (None, ""):
            return Decimal("0")
        return Decimal(str(value))
    except Exception:
        raise ValueError(field_name)


def _parse_decimal_nonneg(value, field_name="value", *, default=None, decimal_places=3):
    """
    Неотрицательное decimal (остаток товара, вес и т.п.).
    Пустое значение: default или 0.
    """
    try:
        if value in (None, ""):
            d = Decimal(str(default)) if default is not None else Decimal("0")
        else:
            s = str(value).strip().replace(",", ".")
            d = Decimal(s)
        if not d.is_finite() or d < 0:
            raise ValueError(field_name)
        quant = Decimal(10) ** -int(decimal_places)
        return d.quantize(quant, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(field_name)


def _parse_int_nonneg(value, field_name="value", *, default=None, maximum=None):
    """
    Целое >= 0. Пустое значение: при default — default, иначе 0 (как раньше).
    maximum — верхняя граница (включительно), после парсинга.
    """
    try:
        if value in (None, ""):
            v = int(default) if default is not None else 0
        else:
            v = int(value)
        if v < 0:
            raise ValueError(field_name)
        if maximum is not None and v > int(maximum):
            v = int(maximum)
        return v
    except Exception:
        raise ValueError(field_name)


def _parse_date_to_aware_datetime(raw_value):
    """
    Принимает YYYY-MM-DD или ISO datetime.
    Возвращает timezone-aware datetime.
    """
    if raw_value in (None, ""):
        return None

    if isinstance(raw_value, timezone.datetime):
        dt = raw_value
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt

    s = str(raw_value).strip()
    dt = parse_datetime(s)
    if dt:
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt

    d = parse_date(s)
    if d:
        dt = timezone.datetime(d.year, d.month, d.day, 0, 0, 0)
        return timezone.make_aware(dt)

    raise ValueError("date")
