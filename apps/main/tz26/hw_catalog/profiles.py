"""Б-54: каталог профилей магазина (пока — профиль hardware «Хозтовары и стройматериалы»)."""

_HINTS = [
    # ru, ky, en, tr, uz
    ("Крепёж", "Бекиткичтер", "Fasteners", "Bağlantı elemanları", "Mahkamlash buyumlari"),
    ("Инструмент", "Шаймандар", "Tools", "El aletleri", "Asboblar"),
    ("Электрика", "Электр буюмдары", "Electrical", "Elektrik malzemeleri", "Elektr jihozlari"),
    ("Сантехника", "Сантехника", "Plumbing", "Tesisat", "Santexnika"),
    ("Краски и лаки", "Боёктор жана лактар", "Paints and varnishes", "Boya ve vernikler", "Bo'yoqlar va laklar"),
    ("Сухие смеси", "Кургак аралашмалар", "Dry mixes", "Kuru karışımlar", "Quruq qorishmalar"),
    ("Плитка и напольные покрытия", "Плитка жана төшөмө каптоолор", "Tiles and flooring",
     "Fayans ve zemin kaplamaları", "Plitka va pol qoplamalari"),
    ("Обои и клей", "Обой жана желим", "Wallpaper and adhesives", "Duvar kâğıdı ve yapıştırıcılar", "Oboy va yelim"),
    ("Листовые материалы", "Барак материалдар", "Sheet materials", "Levha malzemeler", "List materiallar"),
    ("Пиломатериалы", "Жыгач материалдар", "Lumber", "Kereste", "Yog'och materiallar"),
    ("Утеплитель и изоляция", "Жылуулоочу жана изоляция материалдары", "Insulation",
     "Yalıtım malzemeleri", "Issiqlik va ovoz izolyatsiyasi"),
    ("Сад и огород", "Бакча жана огород", "Garden", "Bahçe", "Bog' va polizchilik"),
    ("Бытовая химия и хозтовары", "Тиричилик химиясы жана чарба буюмдары", "Household chemicals and goods",
     "Temizlik ve ev gereçleri", "Maishiy kimyo va xo'jalik mollari"),
    ("Герметики и пена", "Герметиктер жана көбүк", "Sealants and foam", "Sızdırmazlık ve köpükler",
     "Germetik va ko'pik"),
    ("Спецодежда и СИЗ", "Атайын кийим жана жеке коргонуу каражаттары", "Workwear and PPE",
     "İş kıyafetleri ve KKD", "Maxsus kiyim va himoya vositalari"),
]
_LANGS = ("ru", "ky", "en", "tr", "uz")


def hardware_profile():
    return {
        "id": "hardware",
        "base": "grocery",
        "icon": "hammer",
        "accent": "#F59E0B",
        "title": {
            "ru": "Хозтовары и стройматериалы",
            "ky": "Чарба буюмдары жана курулуш материалдары",
            "en": "Hardware and building materials",
            "tr": "Hırdavat ve yapı malzemeleri",
            "uz": "Xo'jalik mollari va qurilish materiallari",
        },
        "units": ["pcs", "m", "m2", "m3", "kg", "l", "t", "pack", "box", "bag", "roll", "sheet", "set", "pair"],
        "fractional_units": {
            "m": {"step": "0.1", "min": "1"},
            "m2": {"step": "0.01", "min": "0.5"},
            "m3": {"step": "0.01", "min": "0.1"},
            "l": {"step": "0.1", "min": "0.5"},
            "kg": {"step": "0.1", "min": "0.1"},
            "t": {"step": "0.01", "min": "0.01"},
        },
        "variants": False,
        "expiry": "optional",
        "batches": False,
        "serials": False,
        "features": {
            "pack_scan_multiplies": True,
            "consumption_calc": True,
            "price_levels": True,
            "client_objects": True,
            "storage_locations": True,
        },
        "presets": {"small": {"price_levels": False, "client_objects": False}, "market": {}},
        "category_hints": [dict(zip(_LANGS, row)) for row in _HINTS],
        "price_tag_template": "hardware_58x40",
        "label_template": "basic",
        "default_debt_days": 14,
    }


def all_profiles():
    """Список профилей для GET api/public/sphere-profiles/. grocery/clothing в коде как профили не описаны."""
    return [hardware_profile()]
