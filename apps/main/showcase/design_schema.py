"""
Документ вида онлайн-витрины (ТЗ-BE-2026-05, п. 5): значения по умолчанию, допустимые значения,
пресеты, проверка (PATCH со слиянием / PUT), контраст WCAG и апгрейд документов старого формата
(ТЗ-BE-2026-03: theme-плоские цвета, layout, cards, carousel, brand, footer).

Правила:
- неизвестный ключ или значение → 400 с именем поля (``field`` = путь через точку);
- PATCH сливает вложенные объекты (и словари текстов по языкам), массивы заменяются целиком;
- тексты хранятся по языкам {"ru": "...", "ky": "..."}; строку принимаем как {"ru": строка};
- ссылки только http(s)://, tel:, mailto: (https://wa.me/ — частный случай https);
- картинки только id загруженных медиа (п. 6.10).
"""
from __future__ import annotations

import copy
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional

from rest_framework import status
from rest_framework.exceptions import APIException

# ----------------------------------------------------------------------
# Справочники (п. 6.1)
# ----------------------------------------------------------------------

LANGUAGES = ("ru", "ky", "en", "uz", "kk")

FONTS = [
    {"family": "Inter", "cyrillic": True, "kyrgyz": True},
    {"family": "Roboto", "cyrillic": True, "kyrgyz": True},
    {"family": "Roboto Condensed", "cyrillic": True, "kyrgyz": True},
    {"family": "Montserrat", "cyrillic": True, "kyrgyz": True},
    {"family": "Nunito", "cyrillic": True, "kyrgyz": True},
    {"family": "PT Sans", "cyrillic": True, "kyrgyz": False},
    {"family": "Noto Sans", "cyrillic": True, "kyrgyz": True},
]
FONT_FAMILIES = tuple(f["family"] for f in FONTS)

SECTION_TYPES = (
    "announcement", "hero", "banners", "categories", "promo_blocks", "products",
    "featured", "new_arrivals", "on_sale", "text", "contacts",
)

# ТЗ Н-33: способы выбора варианта на странице товара
VARIANT_PICKERS = ("buttons", "dropdown", "swatches")

SORT_OPTIONS = (
    "default", "name_asc", "name_desc", "price_asc", "price_desc", "discount_desc", "discount_asc",
)
DEFAULT_SORTS = SORT_OPTIONS + ("manual", "new")

# Лимиты по умолчанию (п. 6.1, вопрос 6). Переопределяются settings.SHOWCASE_LIMITS (dict).
DEFAULT_LIMITS = {
    "banners": 10,
    "promo_blocks": 6,
    "pages": 10,
    "media_mb": 5,
    "pinned_products": 24,
    "sections": 30,
    "versions": 20,
}


def get_limits() -> dict:
    from django.conf import settings

    limits = dict(DEFAULT_LIMITS)
    limits.update(getattr(settings, "SHOWCASE_LIMITS", None) or {})
    return limits


# ----------------------------------------------------------------------
# Документ по умолчанию = текущий вид витрины (п. 5.2)
# ----------------------------------------------------------------------

DEFAULT_COLORS = {
    "background": "#F5F6F8", "surface": "#FFFFFF", "text": "#111827", "text_muted": "#6B7280",
    "accent": "#F7D74F", "accent_text": "#181F2B", "border": "#E5E7EB",
    "header_bg": "#FFFFFF", "header_text": "#111827",
    "footer_bg": "#111827", "footer_text": "#F9FAFB",
    "price": "#111827", "old_price": "#9CA3AF",
    "badge_new_bg": "#22C55E", "badge_new_text": "#FFFFFF",
    "badge_sale_bg": "#EF4444", "badge_sale_text": "#FFFFFF",
}


def default_theme() -> dict:
    return {
        "preset": None,
        "mode": "light",
        "colors": dict(DEFAULT_COLORS),
        "font": {"family": "Roboto Condensed", "base_size": 15, "heading_weight": 700},
        "radius": 14,
        "shadow": "soft",
        "button_style": "filled",
        "background_image": None,
        "background_pattern": None,
    }


def default_banners_carousel() -> dict:
    return {"autoplay": True, "interval_s": 5, "arrows": True, "dots": True}


def default_document(company_name: str = "", whatsapp_phone: Optional[str] = None) -> dict:
    name = company_name or ""
    return {
        "theme": default_theme(),
        "header": {
            "logo": None, "logo_height": 44, "show_name": True, "name": {"ru": name},
            "slogan": {"ru": ""}, "layout": "logo_left", "sticky": True,
            "show_search": True, "search_placeholder": {"ru": "Поиск товаров..."},
            "cart_button": {"style": "button", "text": {"ru": "Корзина"}},
            "announcement": {"enabled": False, "text": {"ru": ""}, "bg": "#111827", "text_color": "#FFFFFF", "link": None},
        },
        "sections": [
            {"id": "s1", "type": "hero", "enabled": True},
            {"id": "s2", "type": "banners", "enabled": False, "place": "hero", "carousel": default_banners_carousel()},
            {"id": "s3", "type": "categories", "enabled": True},
            {"id": "s4", "type": "promo_blocks", "enabled": False},
            {"id": "s5", "type": "products", "enabled": True, "source": "all"},
        ],
        "hero": {
            "style": "card", "title": {"ru": name}, "subtitle": {"ru": ""},
            "image": None, "overlay": 0.3, "align": "left",
            "button": {"enabled": False, "text": {"ru": "Смотреть акции"}, "link": None},
        },
        "categories": {
            "style": "chips", "show_all": True, "show_count": True, "title": {"ru": "Категории"},
            "order": [], "hidden": [],
        },
        "products": {
            "title": {"ru": "Все товары"},
            "columns": {"desktop": 3, "tablet": 3, "mobile": 2},
            "page_size": 60, "pagination": "pages",
            "default_sort": "default",
            "sort_options": list(SORT_OPTIONS),
            "show_count": True, "hide_out_of_stock": False, "hide_zero_price": False,
        },
        "card": {
            "template": "standard", "photo_ratio": "4:3", "photo_fit": "contain",
            "placeholder_image": None, "placeholder_text": {"ru": "Фото нет"},
            "price_position": "photo_corner",
            "show": {
                "old_price": True, "discount_badge": True, "new_badge": True, "category": True,
                "stock": "hidden", "unit": True, "description": False, "add_button": True, "quantity_stepper": False,
                "swatches": False,  # ТЗ Н-33: кружки цветов на карточке (одежда)
            },
            "new_badge_days": 14, "new_badge_text": {"ru": "НОВИНКА"},
            "add_button_text": {"ru": "В корзину"},
            "shadow": True, "border": False,
        },
        "product_page": {
            "enabled": False, "gallery": True, "show_description": True,
            "show_characteristics": True, "show_related": True,
            "variant_picker": "buttons",  # ТЗ Н-33: как выбирать размер/цвет
        },
        "cart": {
            "style": "drawer",
            "fields": {"phone": "required", "name": "hidden", "address": "hidden", "comment": "hidden"},
            "phone_country": "+996", "min_order_total": None,
            "delivery": {"pickup": True, "delivery": False, "delivery_fee": None, "free_from": None, "zones_text": {"ru": ""}},
            "payment_text": {"ru": ""},
            "order_channel": "whatsapp",
            "whatsapp_phone": whatsapp_phone or None,
            "checkout_button_text": {"ru": "Оформить заказ"},
            "success_text": {"ru": "Спасибо! Мы скоро свяжемся с вами."},
        },
        "footer": {
            "enabled": False, "address": {"ru": ""}, "phones": [], "hours": {"ru": ""},
            "socials": {"instagram": None, "telegram": None, "whatsapp": None, "tiktok": None},
            "map_url": None, "show_pages": True, "copyright": {"ru": ""},
        },
        "seo": {"title": {"ru": name}, "description": {"ru": ""}, "og_image": None, "favicon": None, "indexing": True},
        "languages": {"enabled": ["ru"], "default": "ru", "switcher": False},
    }


DOCUMENT_SECTIONS = tuple(default_document().keys())

# ----------------------------------------------------------------------
# Пресеты тем (п. 6.1). Пресет меняет только theme.
# ----------------------------------------------------------------------


def _preset_theme(colors: dict, *, mode="light", font="Roboto Condensed", radius=14, shadow="soft",
                  button_style="filled", pattern=None) -> dict:
    t = default_theme()
    t["colors"].update(colors)
    t["mode"] = mode
    t["font"]["family"] = font
    t["radius"] = radius
    t["shadow"] = shadow
    t["button_style"] = button_style
    t["background_pattern"] = pattern
    return t


PRESETS = [
    {"code": "classic", "name": {"ru": "Классика", "ky": "Классика"}, "theme": _preset_theme({})},
    {"code": "dark", "name": {"ru": "Тёмная", "ky": "Караңгы"}, "theme": _preset_theme({
        "background": "#0F172A", "surface": "#1E293B", "text": "#F1F5F9", "text_muted": "#94A3B8",
        "accent": "#F7D74F", "accent_text": "#111827", "border": "#334155",
        "header_bg": "#111827", "header_text": "#F9FAFB", "footer_bg": "#020617", "footer_text": "#E2E8F0",
        "price": "#F8FAFC", "old_price": "#64748B",
    }, mode="dark", font="Inter", radius=12)},
    {"code": "minimal", "name": {"ru": "Минимализм", "ky": "Минимализм"}, "theme": _preset_theme({
        "background": "#FFFFFF", "surface": "#FFFFFF", "text": "#111111", "text_muted": "#555555",
        "accent": "#111111", "accent_text": "#FFFFFF", "border": "#E5E5E5",
        "header_bg": "#FFFFFF", "header_text": "#111111", "footer_bg": "#FAFAFA", "footer_text": "#111111",
        "price": "#111111", "old_price": "#8A8A8A",
    }, font="Inter", radius=4, shadow="none", button_style="outline")},
    {"code": "fresh", "name": {"ru": "Свежая", "ky": "Жаңы"}, "theme": _preset_theme({
        "background": "#F0FDF4", "surface": "#FFFFFF", "text": "#14532D", "text_muted": "#3F6212",
        "accent": "#16A34A", "accent_text": "#FFFFFF", "border": "#BBF7D0",
        "header_bg": "#FFFFFF", "header_text": "#14532D", "footer_bg": "#14532D", "footer_text": "#F0FDF4",
        "price": "#166534", "old_price": "#86A08F",
    }, font="Nunito", radius=18)},
    {"code": "kyrgyz", "name": {"ru": "Кыргыз", "ky": "Кыргыз"}, "theme": _preset_theme({
        # Красный флага + солнце-тундук; Noto Sans — с ң ө ү.
        "background": "#FFF8E7", "surface": "#FFFFFF", "text": "#1F1A17", "text_muted": "#6B5E55",
        "accent": "#C8102E", "accent_text": "#FFFFFF", "border": "#F1D9A7",
        "header_bg": "#C8102E", "header_text": "#FFFFFF", "footer_bg": "#7A0A1C", "footer_text": "#FFF3D6",
        "price": "#A10D25", "old_price": "#9C8F86",
        "badge_new_bg": "#F2B705", "badge_new_text": "#1F1A17",
    }, font="Noto Sans", radius=12, pattern="ornament")},
    {"code": "premium", "name": {"ru": "Премиум", "ky": "Премиум"}, "theme": _preset_theme({
        "background": "#0B0B0C", "surface": "#161618", "text": "#F5F1E8", "text_muted": "#B8B0A0",
        "accent": "#C9A44C", "accent_text": "#0B0B0C", "border": "#2A2A2D",
        "header_bg": "#0B0B0C", "header_text": "#F5F1E8", "footer_bg": "#000000", "footer_text": "#D9D2C3",
        "price": "#E8C872", "old_price": "#7D776C",
        "badge_new_bg": "#C9A44C", "badge_new_text": "#0B0B0C",
    }, mode="dark", font="Montserrat", radius=6, shadow="strong")},
]
PRESET_CODES = tuple(p["code"] for p in PRESETS)


def get_preset(code: str) -> Optional[dict]:
    for p in PRESETS:
        if p["code"] == code:
            return copy.deepcopy(p)
    return None


# ----------------------------------------------------------------------
# Ошибки
# ----------------------------------------------------------------------


class ShowcaseFieldError(APIException):
    """400 {detail, code, field}."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "invalid"

    def __init__(self, field: Optional[str], detail: str, code: str = "invalid", status_code: Optional[int] = None,
                 extra: Optional[dict] = None):
        payload = {"detail": detail, "code": code}
        if field:
            payload["field"] = field
        if extra:
            payload.update(extra)  # ТЗ Н-27: index, available
        if status_code:
            self.status_code = status_code
        super().__init__(detail=payload, code=code)
        self.detail = payload  # без превращения в ErrorDetail-словарь


def _err(path: str, detail: str, code: str = "invalid"):
    raise ShowcaseFieldError(path, detail, code)


# ----------------------------------------------------------------------
# Проверка ссылок / текстов
# ----------------------------------------------------------------------

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SAFE_URL_RE = re.compile(r"^(https?://[^\s<>\"']+|tel:\+?[0-9()\-\s]{3,32}|mailto:[^\s<>\"'@]+@[^\s<>\"']+)$", re.I)
PHONE_RE = re.compile(r"^\+?[0-9()\-\s]{5,24}$")
SECTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def is_safe_url(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 2048 and bool(SAFE_URL_RE.match(value.strip()))


def clean_text(value: Any, path: str, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        _err(path, "Ожидается текст.")
    value = _CTRL_RE.sub("", value)
    if len(value) > max_len:
        _err(path, f"Текст длиннее {max_len} символов.", "too_long")
    return value


def _as_uuid_str(value: Any, path: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _err(path, "Ожидается идентификатор (UUID).")


# ----------------------------------------------------------------------
# Описание полей
# ----------------------------------------------------------------------


class Ctx:
    """Контекст проверки: компания (для проверки id медиа)."""

    def __init__(self, company=None, media_ids: Optional[set] = None):
        self.company = company
        self._media_ids = media_ids

    def media_exists(self, media_id: str) -> bool:
        if self.company is None:
            return True
        if self._media_ids is None:
            from apps.main.models import ShowcaseMedia

            self._media_ids = {str(x) for x in ShowcaseMedia.objects.filter(company=self.company).values_list("id", flat=True)}
        return media_id in self._media_ids


class Field:
    nullable = False

    def check(self, value, path: str, ctx: Ctx):  # pragma: no cover - interface
        raise NotImplementedError

    def patch(self, current, value, path: str, ctx: Ctx):
        """Применить значение из PATCH к текущему (по умолчанию — замена)."""
        if value is None:
            if self.nullable:
                return None
            _err(path, "Поле не может быть пустым (null).")
        return self.check(value, path, ctx)

    def lenient(self, default, stored, ctx: Ctx):
        """Мягкое чтение сохранённого значения (апгрейд): ошибка → значение по умолчанию."""
        try:
            return self.patch(default, stored, "", ctx)
        except ShowcaseFieldError:
            return copy.deepcopy(default)


class Color(Field):
    def __init__(self, nullable=False):
        self.nullable = nullable

    def check(self, value, path, ctx):
        if not isinstance(value, str) or not HEX_COLOR_RE.match(value):
            _err(path, "Цвет должен быть в формате #RRGGBB.")
        return value.upper()


class Enum(Field):
    def __init__(self, values, nullable=False):
        self.values = tuple(values)
        self.nullable = nullable

    def check(self, value, path, ctx):
        if value not in self.values:
            _err(path, f"Недопустимое значение. Допустимые: {', '.join(str(v) for v in self.values)}.")
        return value


class Int(Field):
    def __init__(self, lo, hi, nullable=False):
        self.lo, self.hi, self.nullable = lo, hi, nullable

    def check(self, value, path, ctx):
        if isinstance(value, bool):
            _err(path, "Ожидается целое число.")
        try:
            iv = int(value)
            if isinstance(value, float) and iv != value:
                raise ValueError
            if isinstance(value, str) and str(iv) != value.strip():
                raise ValueError
        except (ValueError, TypeError):
            _err(path, "Ожидается целое число.")
        if not (self.lo <= iv <= self.hi):
            _err(path, f"Допустимо от {self.lo} до {self.hi}.")
        return iv


class Num(Field):
    def __init__(self, lo, hi, nullable=False, places=2):
        self.lo, self.hi, self.nullable, self.places = lo, hi, nullable, places

    def check(self, value, path, ctx):
        if isinstance(value, bool):
            _err(path, "Ожидается число.")
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            _err(path, "Ожидается число.")
        if not d.is_finite() or d < Decimal(str(self.lo)) or d > Decimal(str(self.hi)):
            _err(path, f"Допустимо от {self.lo} до {self.hi}.")
        d = d.quantize(Decimal(1).scaleb(-self.places))
        return int(d) if d == d.to_integral_value() else float(d)


class Bool(Field):
    def check(self, value, path, ctx):
        if not isinstance(value, bool):
            _err(path, "Ожидается true или false.")
        return value


class Text(Field):
    def __init__(self, max_len=200, nullable=False, regex=None, regex_msg="Неверный формат."):
        self.max_len, self.nullable, self.regex, self.regex_msg = max_len, nullable, regex, regex_msg

    def check(self, value, path, ctx):
        value = clean_text(value, path, self.max_len)
        if self.regex is not None and value and not self.regex.match(value):
            _err(path, self.regex_msg)
        return value


class I18n(Field):
    """Текст по языкам {"ru": "...", "ky": "..."}; при PATCH языки сливаются."""

    def __init__(self, max_len=200, nullable=False):
        self.max_len, self.nullable = max_len, nullable

    def check(self, value, path, ctx):
        if isinstance(value, str):
            value = {"ru": value}
        if not isinstance(value, dict):
            _err(path, 'Ожидается текст по языкам: {"ru": "...", "ky": "..."}.')
        out = {}
        for lang, txt in value.items():
            if lang not in LANGUAGES:
                _err(f"{path}.{lang}" if path else lang, f"Неизвестный язык. Допустимые: {', '.join(LANGUAGES)}.")
            out[lang] = clean_text(txt, f"{path}.{lang}", self.max_len)
        return out

    def patch(self, current, value, path, ctx):
        if value is None:
            if self.nullable:
                return None
            _err(path, "Поле не может быть пустым (null).")
        new = self.check(value, path, ctx)
        if isinstance(current, dict) and not isinstance(value, str):
            merged = dict(current)
            merged.update(new)
            return merged
        return new


class Media(Field):
    nullable = True

    def check(self, value, path, ctx):
        mid = _as_uuid_str(value, path)
        if not ctx.media_exists(mid):
            _err(path, "Картинка не найдена. Загрузите её через /api/main/showcase/media/.", "media_not_found")
        return mid


class Url(Field):
    def __init__(self, nullable=True):
        self.nullable = nullable

    def check(self, value, path, ctx):
        if not is_safe_url(value):
            _err(path, "Ссылка должна начинаться с http://, https://, tel: или mailto:.", "invalid_link")
        return value.strip()


LINK_TYPES = ("product", "category", "promotion", "page", "url")


class Link(Field):
    """{"type": product|category|promotion|page|url, "id"|"url": ...}."""

    nullable = True

    def check(self, value, path, ctx):
        if not isinstance(value, dict):
            _err(path, 'Ожидается ссылка {"type": ..., "id" | "url": ...}.')
        ltype = value.get("type")
        if ltype not in LINK_TYPES:
            _err(f"{path}.type", f"Недопустимый тип ссылки. Допустимые: {', '.join(LINK_TYPES)}.")
        allowed = {"type", "url"} if ltype == "url" else {"type", "id"}
        for k in value:
            if k not in allowed:
                _err(f"{path}.{k}", "Неизвестное поле ссылки.")
        if ltype == "url":
            return {"type": "url", "url": Url(nullable=False).check(value.get("url"), f"{path}.url", ctx)}
        lid = value.get("id")
        if ltype == "page":
            if not isinstance(lid, str) or not lid:
                _err(f"{path}.id", "Укажите slug или id страницы.")
            return {"type": "page", "id": clean_text(lid, f"{path}.id", 64)}
        return {"type": ltype, "id": _as_uuid_str(lid, f"{path}.id")}


class ListOf(Field):
    def __init__(self, item: Field, max_items=50, unique=False):
        self.item, self.max_items, self.unique = item, max_items, unique

    def check(self, value, path, ctx):
        if not isinstance(value, list):
            _err(path, "Ожидается список.")
        if len(value) > self.max_items:
            _err(path, f"Не больше {self.max_items} элементов.", "limit_exceeded")
        out = [self.item.patch(None, v, f"{path}[{i}]", ctx) for i, v in enumerate(value)]
        if self.unique:
            seen = []
            for v in out:
                if v not in seen:
                    seen.append(v)
            out = seen
        return out


class UUIDItem(Field):
    def check(self, value, path, ctx):
        return _as_uuid_str(value, path)


class Obj(Field):
    def __init__(self, fields: Dict[str, Field], nullable=False, default: Optional[Callable] = None):
        self.fields, self.nullable, self.default = fields, nullable, default

    def _base(self, current):
        if isinstance(current, dict):
            return copy.deepcopy(current)
        return copy.deepcopy(self.default()) if self.default else {}

    def patch(self, current, value, path, ctx):
        if value is None:
            if self.nullable:
                return None
            _err(path, "Поле не может быть пустым (null).")
        if not isinstance(value, dict):
            _err(path, "Ожидается объект.")
        out = self._base(current)
        for key, v in value.items():
            spec = self.fields.get(key)
            sub = f"{path}.{key}" if path else key
            if spec is None:
                _err(sub, "Неизвестное поле.", "unknown_field")
            out[key] = spec.patch(out.get(key), v, sub, ctx)
        return out

    def check(self, value, path, ctx):
        return self.patch(None, value, path, ctx)

    def lenient(self, default, stored, ctx):
        out = copy.deepcopy(default) if isinstance(default, dict) else {}
        if not isinstance(stored, dict):
            return out
        for key, spec in self.fields.items():
            if key in stored:
                out[key] = spec.lenient(out.get(key), stored[key], ctx)
        return out


# --- Разделы страницы (sections) ---

_CAROUSEL = Obj({
    "autoplay": Bool(), "interval_s": Int(2, 30), "arrows": Bool(), "dots": Bool(),
}, default=default_banners_carousel)

_SECTION_EXTRA: Dict[str, Dict[str, Field]] = {
    "banners": {"place": Enum(("hero", "inline", "sidebar", "popup")), "carousel": _CAROUSEL},
    "products": {
        "source": Enum(("all", "category", "new", "on_sale", "featured")),
        "category": type("NullableUUID", (UUIDItem,), {"nullable": True})(),
        "max_items": Int(1, 120, nullable=True),
    },
    "featured": {"max_items": Int(1, 48), "style": Enum(("grid", "carousel"))},
    "new_arrivals": {"max_items": Int(1, 48), "style": Enum(("grid", "carousel"))},
    "on_sale": {"max_items": Int(1, 48), "style": Enum(("grid", "carousel"))},
    "promo_blocks": {"ids": ListOf(UUIDItem(), max_items=20, unique=True)},
    "text": {"text": I18n(2000)},
}


class Sections(Field):
    def check(self, value, path, ctx):
        limit = get_limits()["sections"]
        if not isinstance(value, list):
            _err(path, "Ожидается список блоков.")
        if len(value) > limit:
            _err(path, f"Не больше {limit} блоков.", "limit_exceeded")
        out, ids = [], set()
        for i, sec in enumerate(value):
            sp = f"{path}[{i}]"
            if not isinstance(sec, dict):
                _err(sp, "Ожидается объект блока.")
            stype = sec.get("type")
            if stype not in SECTION_TYPES:
                _err(f"{sp}.type", f"Неизвестный тип блока. Допустимые: {', '.join(SECTION_TYPES)}.")
            sid = sec.get("id") or f"s{i + 1}"
            if not isinstance(sid, str) or not SECTION_ID_RE.match(sid):
                _err(f"{sp}.id", "id блока: латиница, цифры, - и _, до 32 символов.")
            if sid in ids:
                _err(f"{sp}.id", "Повторяющийся id блока.")
            ids.add(sid)
            enabled = sec.get("enabled", True)
            if not isinstance(enabled, bool):
                _err(f"{sp}.enabled", "Ожидается true или false.")
            item = {"id": sid, "type": stype, "enabled": enabled}
            extra = dict(_SECTION_EXTRA.get(stype, {}))
            extra["title"] = I18n(200)
            for k, v in sec.items():
                if k in ("id", "type", "enabled"):
                    continue
                spec = extra.get(k)
                if spec is None:
                    _err(f"{sp}.{k}", f"Неизвестное поле для блока «{stype}».", "unknown_field")
                item[k] = spec.patch(None, v, f"{sp}.{k}", ctx)
            if stype == "banners":
                item.setdefault("place", "hero")
                item["carousel"] = _CAROUSEL.patch(default_banners_carousel(), item.get("carousel") or {}, f"{sp}.carousel", ctx)
            out.append(item)
        return out

    def lenient(self, default, stored, ctx):
        try:
            return self.check(stored, "", ctx)
        except ShowcaseFieldError:
            return copy.deepcopy(default)


_MONEY = Num(0, 100000000, nullable=True)
_FIELD_MODE = Enum(("required", "optional", "hidden"))

DOCUMENT_SPEC = Obj({
    "theme": Obj({
        "preset": Enum(PRESET_CODES, nullable=True),
        "mode": Enum(("light", "dark", "auto")),
        "colors": Obj({k: Color() for k in DEFAULT_COLORS}),
        "font": Obj({
            "family": Enum(FONT_FAMILIES),
            "base_size": Int(13, 18),
            "heading_weight": Enum((400, 500, 600, 700, 800, 900)),
        }),
        "radius": Int(0, 28),
        "shadow": Enum(("none", "soft", "strong")),
        "button_style": Enum(("filled", "outline", "soft")),
        "background_image": Media(),
        "background_pattern": Enum(("dots", "grid", "waves", "ornament"), nullable=True),
    }),
    "header": Obj({
        "logo": Media(),
        "logo_height": Int(24, 96),
        "show_name": Bool(),
        "name": I18n(120),
        "slogan": I18n(200),
        "layout": Enum(("logo_left", "logo_center")),
        "sticky": Bool(),
        "show_search": Bool(),
        "search_placeholder": I18n(80),
        "cart_button": Obj({"style": Enum(("button", "icon")), "text": I18n(40)}),
        "announcement": Obj({
            "enabled": Bool(), "text": I18n(300), "bg": Color(), "text_color": Color(), "link": Link(),
        }),
    }),
    "sections": Sections(),
    "hero": Obj({
        "style": Enum(("card", "cover", "split", "hidden")),
        "title": I18n(120),
        "subtitle": I18n(300),
        "image": Media(),
        "overlay": Num(0, 0.9),
        "align": Enum(("left", "center", "right")),
        "button": Obj({"enabled": Bool(), "text": I18n(40), "link": Link()}),
    }),
    "categories": Obj({
        "style": Enum(("chips", "tiles", "list", "hidden")),
        "show_all": Bool(),
        "show_count": Bool(),
        "title": I18n(80),
        "order": ListOf(UUIDItem(), max_items=1000, unique=True),
        "hidden": ListOf(UUIDItem(), max_items=1000, unique=True),
    }),
    "products": Obj({
        "title": I18n(80),
        "columns": Obj({"desktop": Int(2, 6), "tablet": Int(2, 4), "mobile": Int(1, 3)}),
        "page_size": Int(12, 120),
        "pagination": Enum(("pages", "load_more", "infinite")),
        "default_sort": Enum(DEFAULT_SORTS),
        "sort_options": ListOf(Enum(SORT_OPTIONS), max_items=len(SORT_OPTIONS), unique=True),
        "show_count": Bool(),
        "hide_out_of_stock": Bool(),
        "hide_zero_price": Bool(),
    }),
    "card": Obj({
        "template": Enum(("compact", "standard", "large", "list")),
        "photo_ratio": Enum(("1:1", "4:3", "3:4", "16:9")),
        "photo_fit": Enum(("contain", "cover")),
        "placeholder_image": Media(),
        "placeholder_text": I18n(40),
        "price_position": Enum(("photo_corner", "under_name")),
        "show": Obj({
            "old_price": Bool(), "discount_badge": Bool(), "new_badge": Bool(), "category": Bool(),
            "stock": Enum(("hidden", "low_only", "always")), "unit": Bool(), "description": Bool(),
            "add_button": Bool(), "quantity_stepper": Bool(), "swatches": Bool(),
        }),
        "new_badge_days": Int(0, 365),
        "new_badge_text": I18n(24),
        "add_button_text": I18n(40),
        "shadow": Bool(),
        "border": Bool(),
    }),
    "product_page": Obj({
        "enabled": Bool(), "gallery": Bool(), "show_description": Bool(),
        "show_characteristics": Bool(), "show_related": Bool(),
        "variant_picker": Enum(VARIANT_PICKERS),
    }),
    "cart": Obj({
        "style": Enum(("drawer", "page")),
        "fields": Obj({"phone": _FIELD_MODE, "name": _FIELD_MODE, "address": _FIELD_MODE, "comment": _FIELD_MODE}),
        "phone_country": Text(5, regex=re.compile(r"^\+\d{1,4}$"), regex_msg="Код страны вида +996."),
        "min_order_total": _MONEY,
        "delivery": Obj({
            "pickup": Bool(), "delivery": Bool(), "delivery_fee": _MONEY, "free_from": _MONEY, "zones_text": I18n(1000),
        }),
        "payment_text": I18n(1000),
        "order_channel": Enum(("whatsapp", "server", "server_and_whatsapp")),
        "whatsapp_phone": Text(20, nullable=True, regex=re.compile(r"^\+?\d{9,15}$"),
                               regex_msg="Номер WhatsApp: 9–15 цифр, например 996771830438."),
        "checkout_button_text": I18n(40),
        "success_text": I18n(300),
    }),
    "footer": Obj({
        "enabled": Bool(),
        "address": I18n(300),
        "phones": ListOf(Text(24, regex=PHONE_RE, regex_msg="Неверный номер телефона."), max_items=5),
        "hours": I18n(200),
        "socials": Obj({"instagram": Url(), "telegram": Url(), "whatsapp": Url(), "tiktok": Url()}),
        "map_url": Url(),
        "show_pages": Bool(),
        "copyright": I18n(200),
    }),
    "seo": Obj({
        "title": I18n(120),
        "description": I18n(300),
        "og_image": Media(),
        "favicon": Media(),
        "indexing": Bool(),
    }),
    "languages": Obj({
        "enabled": ListOf(Enum(LANGUAGES), max_items=len(LANGUAGES), unique=True),
        "default": Enum(LANGUAGES),
        "switcher": Bool(),
    }),
})

# Ключи, которые фронт может прислать обратно вместе с документом — игнорируем.
_IGNORED_INPUT_KEYS = ("version", "banners", "promo_blocks", "pages", "media", "preview", "catalog")


def _strip_ignored(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in _IGNORED_INPUT_KEYS}


def _cross_checks(doc: dict, touched: set, company=None):
    cart = doc.get("cart") or {}
    if "cart" in touched:
        if cart.get("order_channel") in ("whatsapp", "server_and_whatsapp"):
            phone = cart.get("whatsapp_phone") or (getattr(company, "phones_howcase", None) if company else None)
            if not phone:
                _err("cart.whatsapp_phone", "Для заказов в WhatsApp укажите номер WhatsApp.", "required")
        delivery = cart.get("delivery") or {}
        if not delivery.get("pickup") and not delivery.get("delivery"):
            _err("cart.delivery", "Включите самовывоз или доставку.", "required")
    langs = doc.get("languages") or {}
    if "languages" in touched and langs.get("default") not in (langs.get("enabled") or []):
        _err("languages.default", "Язык по умолчанию должен быть среди включённых.")
    if "products" in touched:
        prod = doc.get("products") or {}
        if not prod.get("sort_options"):
            _err("products.sort_options", "Нужен хотя бы один вариант сортировки.")


def apply_patch(current: dict, data: Any, company=None, ctx: Optional[Ctx] = None) -> dict:
    """PATCH: слияние по разделам с проверкой. Массивы заменяются целиком."""
    if not isinstance(data, dict):
        _err(None, "Ожидается объект с настройками.")
    ctx = ctx or Ctx(company)
    data = _strip_ignored(data)
    new = DOCUMENT_SPEC.patch(current, data, "", ctx)
    _cross_checks(new, set(data.keys()), company)
    return new


def replace_document(data: Any, company=None, ctx: Optional[Ctx] = None) -> dict:
    """PUT: весь документ целиком; не переданные разделы/поля — значения по умолчанию."""
    if not isinstance(data, dict):
        _err(None, "Ожидается объект с настройками.")
    ctx = ctx or Ctx(company)
    base = default_document(getattr(company, "name", "") or "", getattr(company, "phones_howcase", None))
    data = _strip_ignored(data)
    new = DOCUMENT_SPEC.patch(base, data, "", ctx)
    _cross_checks(new, set(DOCUMENT_SECTIONS), company)
    return new


# ----------------------------------------------------------------------
# Контраст (WCAG AA 4.5:1) — предупреждения, сохранять не мешают
# ----------------------------------------------------------------------

CONTRAST_PAIRS = [
    ("text", "background"),
    ("text", "surface"),
    ("text_muted", "surface"),
    ("accent_text", "accent"),
    ("header_text", "header_bg"),
    ("footer_text", "footer_bg"),
    ("price", "surface"),
    ("badge_new_text", "badge_new_bg"),
    ("badge_sale_text", "badge_sale_bg"),
]
WCAG_AA = 4.5


def _lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_str: str) -> float:
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast_ratio(c1: str, c2: str) -> float:
    l1, l2 = relative_luminance(c1), relative_luminance(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def contrast_warnings(doc: dict) -> List[dict]:
    """
    Пары «текст/фон» из theme.colors (+ полоса объявления). Пара, совпадающая с текущим видом
    по умолчанию (п. 5.2: белый текст на зелёном бейдже «НОВИНКА»), не предупреждается.
    """
    warnings: List[dict] = []
    colors = ((doc.get("theme") or {}).get("colors") or {})
    for fg, bg in CONTRAST_PAIRS:
        a, b = colors.get(fg), colors.get(bg)
        if not (isinstance(a, str) and isinstance(b, str) and HEX_COLOR_RE.match(a) and HEX_COLOR_RE.match(b)):
            continue
        if a.upper() == DEFAULT_COLORS[fg] and b.upper() == DEFAULT_COLORS[bg]:
            continue
        ratio = contrast_ratio(a, b)
        if ratio < WCAG_AA:
            warnings.append({
                "field": f"theme.colors.{fg}", "against": f"theme.colors.{bg}",
                "code": "low_contrast", "ratio": round(ratio, 1), "min_ratio": WCAG_AA,
            })
    ann = ((doc.get("header") or {}).get("announcement") or {})
    a, b = ann.get("text_color"), ann.get("bg")
    if isinstance(a, str) and isinstance(b, str) and HEX_COLOR_RE.match(a) and HEX_COLOR_RE.match(b):
        ratio = contrast_ratio(a, b)
        if ratio < WCAG_AA:
            warnings.append({
                "field": "header.announcement.text_color", "against": "header.announcement.bg",
                "code": "low_contrast", "ratio": round(ratio, 1), "min_ratio": WCAG_AA,
            })
    return warnings


# ----------------------------------------------------------------------
# Апгрейд документов старого формата (ТЗ-BE-2026-03) и мягкое чтение
# ----------------------------------------------------------------------

LEGACY_KEYS = ("layout", "cards", "carousel", "brand")
_LEGACY_THEME_COLOR_MAP = {
    "background": "background", "text": "text", "accent": "accent", "header_bg": "header_bg",
    "header_text": "header_text", "footer_bg": "footer_bg", "card_bg": "surface", "price": "price",
}
_LEGACY_SORT_MAP = {"manual": "manual", "new": "default", "popular": "default", "price_asc": "price_asc", "price_desc": "price_desc"}
_LEGACY_SECTION_MAP = {"banners": "banners", "promos": "promo_blocks", "categories": "categories",
                       "featured": "featured", "all_products": "products"}


def is_legacy_document(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    if any(k in doc for k in LEGACY_KEYS):
        return True
    theme = doc.get("theme")
    if isinstance(theme, dict) and "colors" not in theme and any(k in theme for k in _LEGACY_THEME_COLOR_MAP):
        return True
    footer = doc.get("footer")
    return isinstance(footer, dict) and (
        "phone" in footer or isinstance(footer.get("address"), str) or isinstance(footer.get("hours"), str)
    )


def legacy_patch_to_new(data: dict, current: Optional[dict] = None, *, only_changed: bool = False) -> dict:
    """
    Переводит ключи старого формата в новый документ (частичный PATCH).
    only_changed=True — переносить только значения, отличные от старых значений по умолчанию
    (при апгрейде сохранённых документов: старые дефолты не совпадали с текущим видом).
    Каталожные списки (hidden_products и т.п.) не переводятся — см. legacy_catalog().
    """
    from apps.main.models import get_legacy_default_showcase_design

    ld = get_legacy_default_showcase_design() if only_changed else {}

    def changed(section, key, value):
        if not only_changed:
            return True
        return (ld.get(section) or {}).get(key) != value

    out: Dict[str, Any] = {}
    theme = data.get("theme")
    if isinstance(theme, dict) and "colors" not in theme:
        t: Dict[str, Any] = {}
        colors = {}
        for old, new in _LEGACY_THEME_COLOR_MAP.items():
            if old in theme and theme[old] is not None and changed("theme", old, theme[old]):
                colors[new] = theme[old]
        if colors:
            t["colors"] = colors
        if theme.get("font") and changed("theme", "font", theme["font"]):
            t["font"] = {"family": theme["font"]}
        if theme.get("radius") is not None and changed("theme", "radius", theme["radius"]):
            t["radius"] = theme["radius"]
        if theme.get("mode") and changed("theme", "mode", theme["mode"]):
            t["mode"] = theme["mode"]
        if "preset" in theme and changed("theme", "preset", theme["preset"]):
            t["preset"] = theme["preset"]
        if t:
            out["theme"] = t
    elif isinstance(theme, dict):
        out["theme"] = theme

    layout = data.get("layout")
    if isinstance(layout, dict):
        p: Dict[str, Any] = {}
        cols = layout.get("columns")
        if isinstance(cols, dict) and changed("layout", "columns", cols):
            p["columns"] = {k: v for k, v in cols.items() if k in ("desktop", "tablet", "mobile")}
        if layout.get("default_sort") and changed("layout", "default_sort", layout["default_sort"]):
            ds = layout["default_sort"]
            if ds not in _LEGACY_SORT_MAP:
                _err("layout.default_sort", "Допустимые значения: manual, popular, new, price_asc, price_desc.")
            p["default_sort"] = _LEGACY_SORT_MAP[ds]
        if p:
            out["products"] = p
        secs = layout.get("sections")
        if isinstance(secs, list) and changed("layout", "sections", secs):
            new_secs = [{"id": "s1", "type": "hero", "enabled": True}]
            for i, name in enumerate(secs):
                if name in _LEGACY_SECTION_MAP:
                    new_secs.append({"id": f"s{i + 2}", "type": _LEGACY_SECTION_MAP[name], "enabled": True})
            out["sections"] = new_secs

    cards = data.get("cards")
    if isinstance(cards, dict):
        c: Dict[str, Any] = {}
        for key in ("template", "photo_ratio", "shadow", "border"):
            if key in cards and changed("cards", key, cards[key]):
                c[key] = cards[key]
        show = cards.get("show")
        if isinstance(show, dict):
            lds = (ld.get("cards") or {}).get("show") or {} if only_changed else {}
            s = {}
            for key in ("old_price", "stock", "unit", "discount_badge", "add_button"):
                if key in show and (not only_changed or lds.get(key) != show[key]):
                    s[key] = show[key]
            if s:
                c["show"] = s
        if c:
            out["card"] = c

    brand = data.get("brand")
    if isinstance(brand, dict):
        h: Dict[str, Any] = {}
        if brand.get("logo"):
            h["logo"] = brand["logo"]
        if brand.get("title"):
            h["name"] = {"ru": brand["title"]}
        if brand.get("slogan"):
            h["slogan"] = {"ru": brand["slogan"]}
        if h:
            out["header"] = h
        if brand.get("favicon"):
            out.setdefault("seo", {})["favicon"] = brand["favicon"]

    footer = data.get("footer")
    if isinstance(footer, dict):
        if "phone" in footer or isinstance(footer.get("address"), str) or isinstance(footer.get("hours"), str):
            f: Dict[str, Any] = {}
            if footer.get("phone"):
                f["phones"] = [footer["phone"]]
            if footer.get("address"):
                f["address"] = {"ru": footer["address"]}
            if footer.get("hours"):
                f["hours"] = {"ru": footer["hours"]}
            socials = footer.get("socials") or {}
            soc = {k: (socials.get(k) or None) for k in ("instagram", "whatsapp") if socials.get(k)}
            if soc:
                f["socials"] = soc
            if f:
                f["enabled"] = True
                out["footer"] = f
        else:
            out["footer"] = footer

    carousel = data.get("carousel")
    if isinstance(carousel, dict) and (not only_changed or carousel != ld.get("carousel")):
        base_secs = out.get("sections") or copy.deepcopy((current or {}).get("sections") or default_document()["sections"])
        car = {k: carousel[k] for k in ("autoplay", "interval_s", "arrows", "dots") if k in carousel}
        found = False
        for sec in base_secs:
            if sec.get("type") == "banners":
                sec["carousel"] = {**(sec.get("carousel") or default_banners_carousel()), **car}
                found = True
        if found:
            out["sections"] = base_secs

    for key in DOCUMENT_SECTIONS:
        if key in data and key not in out and key not in ("theme", "footer"):
            out[key] = data[key]
    return out


def legacy_catalog(doc: Any) -> dict:
    """Каталожные списки из старого layout (hidden/pinned/order товаров и категорий)."""
    layout = (doc or {}).get("layout") if isinstance(doc, dict) else None
    if not isinstance(layout, dict):
        return {}
    out = {}
    for key in ("hidden_products", "pinned_products", "product_order", "hidden_categories", "category_order"):
        vals = layout.get(key)
        if isinstance(vals, list):
            out[key] = [str(v) for v in vals if v]
    return out


def normalize_document(stored: Any, company=None) -> dict:
    """
    Сохранённый документ → полный документ нового формата. Неизвестное/битое — значения по умолчанию.
    Без проверки существования медиа (их могли удалить: тогда фронт просто не найдёт картинку в media).
    """
    base = default_document(getattr(company, "name", "") or "", getattr(company, "phones_howcase", None))
    if not isinstance(stored, dict) or not stored:
        return base
    ctx = Ctx(None)
    src = stored
    if is_legacy_document(stored):
        try:
            src = legacy_patch_to_new(stored, only_changed=True)
        except ShowcaseFieldError:
            src = {}
    return DOCUMENT_SPEC.lenient(base, src, ctx)


def collect_media_ids(value: Any, acc: Optional[set] = None) -> set:
    """Все строки-UUID внутри JSON (для проверки «картинка используется»)."""
    acc = acc if acc is not None else set()
    if isinstance(value, dict):
        for v in value.values():
            collect_media_ids(v, acc)
    elif isinstance(value, list):
        for v in value:
            collect_media_ids(v, acc)
    elif isinstance(value, str) and len(value) == 36:
        try:
            acc.add(str(uuid.UUID(value)))
        except ValueError:
            pass
    return acc
