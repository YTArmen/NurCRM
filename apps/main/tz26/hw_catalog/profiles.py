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


def electronics_profile():
    """13.11.2026: «Электроника» — серийные номера и гарантия (флаг компании electronics_2026_11)."""
    hints = [
        ("Смартфоны", "Смартфондор", "Smartphones", "Akıllı telefonlar", "Smartfonlar"),
        ("Ноутбуки и компьютеры", "Ноутбуктар жана компьютерлер", "Laptops and computers",
         "Dizüstü ve masaüstü bilgisayarlar", "Noutbuklar va kompyuterlar"),
        ("Телевизоры", "Телевизорлор", "TVs", "Televizyonlar", "Televizorlar"),
        ("Бытовая техника", "Тиричилик техникасы", "Home appliances", "Ev aletleri", "Maishiy texnika"),
        ("Аксессуары", "Аксессуарлар", "Accessories", "Aksesuarlar", "Aksessuarlar"),
        ("Кабели и зарядки", "Кабелдер жана кубаттагычтар", "Cables and chargers", "Kablolar ve şarj cihazları",
         "Kabellar va zaryadlovchilar"),
    ]
    return {
        "id": "electronics",
        "base": "grocery",
        "icon": "cpu",
        "accent": "#3B82F6",
        "title": {
            "ru": "Электроника и техника",
            "ky": "Электроника жана техника",
            "en": "Electronics and appliances",
            "tr": "Elektronik ve ev aletleri",
            "uz": "Elektronika va texnika",
        },
        "units": ["pcs", "set", "pair", "box", "m"],
        "fractional_units": {"m": {"step": "0.1", "min": "1"}},
        "variants": False,
        "expiry": "none",
        "batches": False,
        "serials": True,
        "flag": "electronics_2026_11",
        "features": {
            "serials": True,
            "serial_lookup": True,
            "warranty": True,
            "repair": False,
            "trade_in": False,
        },
        "presets": {"small": {}, "market": {}},
        "category_hints": [dict(zip(_LANGS, row)) for row in hints],
        "price_tag_template": "basic",
        "label_template": "basic",
        "default_debt_days": 14,
    }


def all_profiles():
    """Список профилей для GET api/public/sphere-profiles/. grocery/clothing в коде как профили не описаны."""
    return [hardware_profile(), electronics_profile()]
