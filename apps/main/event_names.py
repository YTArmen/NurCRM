"""
ТЗ 31.10 п.14: единый словарь событий.

Раньше одно и то же событие называлось по-разному: в ленте GET main/changes/ — «как в коде»
(product_changed + data.what=price, sale_paid), а в WebSocket ws/main/events/ — «для кассы»
(price_changed, sale_created). Единое имя = имя потока WebSocket (оно уже в проде у касс).

  * GET main/changes/   : type — старое имя ленты (не меняем), НОВОЕ поле event — единое имя;
  * ws/main/events/     : type — единое имя (как было), НОВОЕ поле feed_type — старое имя ленты;
  * ws/company/         : event.type — старое имя ленты, event.event — единое имя.
Фильтр ?types= в main/changes принимает и старые, и единые имена.
"""

# --- единые имена (то, что видит клиент в поле event / type потока) ---
PRICE_CHANGED = "price_changed"
PRODUCT_CHANGED = "product_changed"
STOCK_CHANGED = "stock_changed"
SETTINGS_CHANGED = "settings_changed"
SALE_CREATED = "sale_created"
SALE_CHANGED = "sale_changed"
SHIFT_CHANGED = "shift_changed"
STAFF_CHANGED = "staff_changed"
CLIENT_CHANGED = "client_changed"
BONUS_CHANGED = "bonus_changed"
DEBT_CHANGED = "debt_changed"
SUPPLIER_DEBT_CHANGED = "supplier_debt_changed"
LICENSE_CHANGED = "license_changed"
RECEIPT_CHANGED = "receipt_changed"  # ТЗ редактор накладных 30.10: правка/откат прихода поставщика
CUSTOMER_ORDER_CHANGED = "customer_order_changed"  # feed2011
PLAN_CHANGED = "plan_changed"  # feed2011

# имя ленты (ChangeEvent.type) -> единое имя; product_changed разбирается по data.what отдельно
FEED_TO_UNIFIED = {
    "sale_paid": SALE_CREATED,
    "sale_returned": SALE_CHANGED,
    "shift_opened": SHIFT_CHANGED,
    "shift_closed": SHIFT_CHANGED,
    "cash_moved": SHIFT_CHANGED,
    "app_settings_changed": SETTINGS_CHANGED,
    "debt_created": DEBT_CHANGED,
    "debt_paid": DEBT_CHANGED,
    "features_changed": LICENSE_CHANGED,
    "stock_changed": STOCK_CHANGED,
    "staff_changed": STAFF_CHANGED,
    "client_changed": CLIENT_CHANGED,
    "bonus_changed": BONUS_CHANGED,
    "supplier_debt_changed": SUPPLIER_DEBT_CHANGED,
    "receipt_changed": RECEIPT_CHANGED,
    "sale_changed": SALE_CHANGED,  # feed2011
    "customer_order_changed": CUSTOMER_ORDER_CHANGED,  # feed2011
}

# единое имя -> старые имена ленты (для фильтра ?types=)
UNIFIED_TO_FEED = {}
for _feed, _uni in FEED_TO_UNIFIED.items():
    UNIFIED_TO_FEED.setdefault(_uni, []).append(_feed)
UNIFIED_TO_FEED.setdefault(PRICE_CHANGED, []).append("product_changed")
UNIFIED_TO_FEED.setdefault(PRODUCT_CHANGED, []).append("product_changed")

# что уходит в поток WebSocket (остальные типы ленты в поток не попадают)
STREAM_UNIFIED = (
    PRICE_CHANGED, PRODUCT_CHANGED, STOCK_CHANGED, SETTINGS_CHANGED, SALE_CREATED, SALE_CHANGED,
    SHIFT_CHANGED, STAFF_CHANGED, CLIENT_CHANGED, BONUS_CHANGED, DEBT_CHANGED, SUPPLIER_DEBT_CHANGED,
    LICENSE_CHANGED, RECEIPT_CHANGED, CUSTOMER_ORDER_CHANGED, PLAN_CHANGED,
)


def unified_name(feed_type: str, data=None) -> str:
    """Единое имя события по имени из ленты (и data.what для product_changed)."""
    if feed_type == "product_changed":
        what = (data or {}).get("what")
        return PRICE_CHANGED if what == "price" else (STOCK_CHANGED if what == "quantity" else PRODUCT_CHANGED)
    return FEED_TO_UNIFIED.get(feed_type, feed_type)


def feed_types_for(name: str) -> list:
    """Имена ленты для фильтра ?types=: единое имя разворачивается в старые, остальные как есть."""
    return list(UNIFIED_TO_FEED.get(name, [])) or [name]
