"""
Общие утилиты для POS системы.
Содержит функции для работы с денежными суммами, количествами и Decimal значениями.
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional, Union
from django.db import models


# Константы для точности округления
Q2 = Decimal("0.01")  # Для денег (2 знака после запятой)
Q3 = Decimal("0.001")  # Для количества (3 знака после запятой)


def money(x: Optional[Decimal]) -> Decimal:
    """
    Округляет Decimal до 2 знаков после запятой (для денежных сумм).
    Принимает Decimal, str, int, float (в т.ч. строки из DRF serializer.data).
    """
    val = to_decimal(x, default=Decimal("0")) or Decimal("0")
    return val.quantize(Q2, rounding=ROUND_HALF_UP)


def cart_item_stock_consume_units(item) -> Decimal:
    """
    Сколько единиц остатка Product.quantity списать для строки корзины.
    Без sale_package: quantity уже в учётных единицах (например пачки).
    С sale_package: quantity — в штуках внутри упаковки, списание = quantity / quantity_in_package.
    """
    q = Decimal(str(getattr(item, "quantity", None) or 0))
    if getattr(item, "sale_unit_id", None):  # ТЗ 26.10 Б-53: строка в единице продажи, списание = quantity x factor
        from apps.main.tz26.hw_units.units import base_quantity_of

        return base_quantity_of(item)
    sp_id = getattr(item, "sale_package_id", None)
    if not sp_id:
        return qty3(q)
    sp = getattr(item, "sale_package", None)
    ipp = Decimal(str(getattr(sp, "quantity_in_package", None) or 0)) if sp is not None else Decimal("0")
    if ipp <= 0:
        raise ValueError("У упаковки должно быть quantity_in_package > 0 для поштучной продажи.")
    return qty3(q / ipp)


def line_qty_consume_units(qty: Decimal, sale_package) -> Decimal:
    """Списание в пачках для количества qty и опциональной упаковки (как cart_item_stock_consume_units без item)."""
    q = qty3(Decimal(str(qty or 0)))
    if sale_package is None:
        return q
    ipp = Decimal(str(getattr(sale_package, "quantity_in_package", None) or 0))
    if ipp <= 0:
        raise ValueError("У упаковки должно быть quantity_in_package > 0.")
    return qty3(q / ipp)


def default_unit_price_for_package(product, sale_package) -> Decimal:
    """Цена за штуку при продаже из пачки: приоритет piece_unit_price упаковки, иначе цена пачки / штук в пачке."""
    pack_price = Decimal(str(getattr(product, "price", None) or 0))
    if sale_package is None:
        return money(pack_price)
    piece = getattr(sale_package, "piece_unit_price", None)
    if piece is not None:
        return money(Decimal(str(piece)))
    ipp = Decimal(str(getattr(sale_package, "quantity_in_package", None) or 0))
    if ipp <= 0:
        return money(pack_price)
    return money(pack_price / ipp)


def qty3(x: Optional[Decimal]) -> Decimal:
    """
    Округляет Decimal до 3 знаков после запятой (для количества товаров).
    Используется для весовых товаров.
    
    Args:
        x: Decimal значение или None
        
    Returns:
        Decimal округленное до 3 знаков
    """
    return (x or Decimal("0")).quantize(Q3, rounding=ROUND_HALF_UP)


def total_cart_consume_packs_for_product(cart_id, product_id) -> Decimal:
    """Суммарное списание в учётных единицах товара (пачках) по всем строкам корзины."""
    from apps.main.models import CartItem

    total = Decimal("0")
    for ci in CartItem.objects.filter(cart_id=cart_id, product_id=product_id).select_related("sale_package"):
        total += cart_item_stock_consume_units(ci)
    return qty3(total)


def q2(x: Optional[Decimal]) -> Decimal:
    """
    Алиас для money() - округление до 2 знаков.
    Оставлен для обратной совместимости.
    """
    return money(x)


def _q2(x: Optional[Decimal]) -> Decimal:
    """
    Внутренняя функция округления до 2 знаков.
    Используется в некоторых местах для явного округления.
    """
    return money(x)


def to_decimal(v: Union[str, int, float, Decimal, None], default: Optional[Decimal] = None) -> Optional[Decimal]:
    """
    Безопасное преобразование значения в Decimal.
    
    Args:
        v: Значение для преобразования (может быть строкой, числом, Decimal или None)
        default: Значение по умолчанию, если преобразование невозможно
        
    Returns:
        Decimal или default
    """
    if v in (None, "", "null", "None"):
        return default
    try:
        # Заменяем запятую на точку для корректного парсинга
        return Decimal(str(v).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return default


def as_decimal(v: Union[str, int, float, Decimal, None], default: Decimal = Decimal("0")) -> Decimal:
    """
    Преобразует значение в Decimal с гарантированным результатом.
    
    Args:
        v: Значение для преобразования
        default: Значение по умолчанию (по умолчанию Decimal("0"))
        
    Returns:
        Decimal значение (никогда не None)
    """
    d = to_decimal(v, default=None)
    if d is None:
        return default
    try:
        return Decimal(d)
    except Exception:
        return default


def fmt_money(x: Optional[Decimal]) -> str:
    """
    Форматирует Decimal как денежную сумму с 2 знаками после запятой.
    
    Args:
        x: Decimal значение
        
    Returns:
        Строка вида "123.45"
    """
    return f"{_q2(x):.2f}"


def fmt(x: Optional[Decimal]) -> str:
    """
    Алиас для fmt_money().
    """
    return fmt_money(x)


def has_field(model: type[models.Model], name: str) -> bool:
    """
    Проверяет, есть ли у модели поле с указанным именем.
    
    Args:
        model: Класс модели Django
        name: Имя поля
        
    Returns:
        True если поле существует, False иначе
    """
    try:
        return any(f.name == name for f in model._meta.get_fields())
    except Exception:
        return False


def get_attr(obj: Optional[object], name: str, default=None):
    """
    Безопасное получение атрибута объекта.
    
    Args:
        obj: Объект или None
        name: Имя атрибута
        default: Значение по умолчанию
        
    Returns:
        Значение атрибута или default
    """
    return getattr(obj, name, default) if obj is not None else default


def log_cart_item_deletion(*, item, deleted_by) -> None:
    """Запись в журнал при удалении позиции из корзины POS (до вызова item.delete())."""
    if deleted_by is None or not getattr(deleted_by, "is_authenticated", False):
        return
    from apps.main.models import CartItemDeletionLog  # локальный импорт — избегаем циклов

    name = ""
    prod = getattr(item, "product", None)
    if prod is not None:
        name = (getattr(prod, "name", None) or "").strip()
    if not name:
        name = (getattr(item, "custom_name", None) or "").strip() or "Позиция"

    CartItemDeletionLog.objects.create(
        company_id=item.company_id,
        branch_id=getattr(item, "branch_id", None),
        cart_id=item.cart_id,
        cart_item_id=item.pk,
        product_id=item.product_id,
        product_name=name[:255],
        quantity=item.quantity,
        deleted_by=deleted_by,
    )
