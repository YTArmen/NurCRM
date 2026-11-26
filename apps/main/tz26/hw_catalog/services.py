"""ТЗ 26.10 Б-57…Б-59: атрибуты, ячейки хранения, сопутствующие товары.

Сюда ведут маленькие «крючки» из ProductSerializer, ProductListView, инвентаризации и витрины.
"""
import re
import uuid
from decimal import Decimal, InvalidOperation

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from core.exceptions import ApiError

LANGS = ("ru", "ky", "en", "tr", "uz")
# ТЗ: key — «[a-z_]{1,32}», но типовые ключи содержат цифры (pack_area_m2, section_mm2) — цифры разрешаем, начинать надо с буквы.
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_ATTRIBUTES = 30
MAX_RELATED = 100
MAX_LOCATION_LEN = 32


def _models():
    from apps.main import models as m

    return m


# ======================================================================
# Общие помощники
# ======================================================================
def company_of(request):
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def lang_of(request) -> str:
    if request is None:
        return "ru"
    raw = ""
    try:
        raw = request.query_params.get("lang") or ""
    except Exception:
        raw = request.GET.get("lang", "") if hasattr(request, "GET") else ""
    if not raw:
        raw = (getattr(request, "META", {}) or {}).get("HTTP_ACCEPT_LANGUAGE", "")
    code = raw.split(",")[0].split(";")[0].strip().lower()[:2]
    return code if code in LANGS else "ru"


def _qp(request):
    if request is None:
        return {}
    qp = getattr(request, "query_params", None)
    if qp is None:
        qp = getattr(request, "GET", None)
    return qp if qp is not None else {}


def resolve_branch(request, company, view=None, raw=None):
    """Филиал для ячейки: raw/«?branch=» ('main' — главный), иначе активный филиал вьюхи. None = главный склад."""
    if raw is None:
        raw = _qp(request).get("branch")
    raw = ("" if raw is None else str(raw)).strip()
    if raw.lower() == "main":
        return None
    if raw:
        try:
            bid = uuid.UUID(raw)
        except ValueError:
            raise ApiError("branch_not_found", "Филиал не найден.", status_code=400, fields={"branch": ["Неверный филиал."]})
        from apps.users.models import Branch

        branch = Branch.objects.filter(pk=bid, company=company).first() if company else None
        if branch is None:
            raise ApiError("branch_not_found", "Филиал не найден в вашей компании.", status_code=400,
                           fields={"branch": ["Филиал не найден."]})
        return branch
    if view is not None and hasattr(view, "_auto_branch"):
        try:
            return view._auto_branch()
        except Exception:
            return None
    return None


def touch_product(product):
    """Сдвинуть updated_at товара — чтобы кассы подтянули изменение через updated_since."""
    m = _models()
    now = timezone.now()
    m.Product.objects.filter(pk=product.pk).update(updated_at=now)
    product.updated_at = now


# ======================================================================
# Б-57. Атрибуты
# ======================================================================
def _num(value):
    """Число из значения или None."""
    if isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or abs(d) >= Decimal("10") ** 14:
        return None
    return d


def _value_text(value) -> str:
    """Текст значения для индекса: числа без хвоста нулей (20.0 -> «20»)."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, Decimal)):
        d = _num(value)
        if d is not None:
            return format(d.normalize(), "f")
    return str(value).strip()


def company_keys(company) -> dict:
    m = _models()
    return {k.key: k for k in m.ProductAttributeKey.objects.filter(company=company)}


def normalize_attributes(raw, company=None):
    """Проверить и привести attributes[] к [{key, value, unit?}]. Ошибка — ApiError 400 invalid_attributes."""

    def bad(msg, idx=None):
        field = "attributes" if idx is None else f"attributes[{idx}]"
        raise ApiError("invalid_attributes", msg, status_code=400, fields={field: [msg]})

    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        bad("Ожидается массив атрибутов.")
    if len(raw) > MAX_ATTRIBUTES:
        bad(f"Не больше {MAX_ATTRIBUTES} атрибутов у товара.")
    keys = company_keys(company) if company is not None else {}
    out, seen = [], set()
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            bad("Атрибут должен быть объектом {key, value, unit?}.", i)
        key = row.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            bad("key — латиница, цифры и «_», до 32 символов, начинается с буквы.", i)
        if key in seen:
            bad(f"Ключ «{key}» указан дважды.", i)
        seen.add(key)
        value = row.get("value")
        if isinstance(value, bool) or value is None:
            bad("value — строка до 64 символов или число.", i)
        if isinstance(value, str):
            value = value.strip()
            if not value or len(value) > 64:
                bad("value — строка от 1 до 64 символов.", i)
        elif isinstance(value, (int, float)):
            if _num(value) is None:
                bad("value — некорректное число.", i)
        else:
            bad("value — строка до 64 символов или число.", i)
        spec = keys.get(key)
        if spec is not None:
            if spec.type == "number" and _num(value) is None:
                bad(f"«{key}»: ожидается число.", i)
            if spec.type == "enum" and spec.options and _value_text(value) not in [str(o) for o in spec.options]:
                bad(f"«{key}»: значение не из списка.", i)
        item = {"key": key, "value": value}
        unit = row.get("unit")
        if unit not in (None, ""):
            if not isinstance(unit, str) or len(unit) > 16:
                bad("unit — строка до 16 символов.", i)
            item["unit"] = unit.strip()
        out.append(item)
    return out


def sync_attribute_index(product, attrs):
    m = _models()
    m.ProductAttributeValue.objects.filter(product=product).delete()
    rows = []
    for a in attrs:
        num = _num(a["value"])
        rows.append(
            m.ProductAttributeValue(
                company_id=product.company_id,
                product=product,
                key=a["key"],
                value=_value_text(a["value"])[:64],
                num=num.quantize(Decimal("0.0001")) if num is not None else None,
            )
        )
    if rows:
        m.ProductAttributeValue.objects.bulk_create(rows)


def set_attributes(product, raw, company=None):
    attrs = normalize_attributes(raw, company or product.company)
    m = _models()
    m.Product.objects.filter(pk=product.pk).update(attributes=attrs, updated_at=timezone.now())
    product.attributes = attrs
    sync_attribute_index(product, attrs)
    return attrs


def rebuild_attribute_index(company):
    """Перестроить индекс значений для всех товаров компании (после импорта/ручной правки JSON)."""
    m = _models()
    n = 0
    for p in m.Product.objects.filter(company=company).exclude(attributes=[]):
        sync_attribute_index(p, p.attributes or [])
        n += 1
    return n


def apply_attr_filters(qs, params):
    """?attr.diameter_mm=20&attr.color_ral=9016,9010 — И между ключами, ИЛИ между значениями через запятую."""
    m = _models()
    for name in list(params.keys()):
        if not name.startswith("attr."):
            continue
        key = name[5:]
        if not KEY_RE.match(key):
            raise ApiError("invalid_attribute_key", f"Неверный ключ атрибута «{key}».", status_code=400,
                           fields={name: ["Неверный ключ."]})
        values = [v.strip() for v in str(params.get(name) or "").split(",") if v.strip()]
        if not values:
            continue
        cond = Q()
        for v in values:
            cond |= Q(value__iexact=v)
            d = _num(v)
            if d is not None:
                cond |= Q(num=d.quantize(Decimal("0.0001")))
        qs = qs.filter(Exists(m.ProductAttributeValue.objects.filter(product=OuterRef("pk"), key=key).filter(cond)))
    return qs


def apply_location_filter(view, qs):
    """?location_prefix=Б-04 — товары, чья ячейка (в филиале запроса) начинается с префикса."""
    request = view.request
    prefix = (_qp(request).get("location_prefix") or "").strip()
    if not prefix:
        return qs
    m = _models()
    branch = resolve_branch(request, company_of(request), view)
    rows = m.ProductStorageLocation.objects.filter(product=OuterRef("pk"), location__istartswith=prefix)
    rows = rows.filter(branch=branch) if branch is not None else rows.filter(branch__isnull=True)
    return qs.filter(Exists(rows))


def apply_catalog_filters(view, qs):
    """Единая точка для ProductListView: атрибуты (Б-57) и ячейка (Б-58)."""
    qs = apply_attr_filters(qs, _qp(view.request))
    return apply_location_filter(view, qs)


def attribute_facets(company, product_ids_qs, lang="ru"):
    """Б-57: facets для filterable-ключей: [{key, name, unit, values:[{value, count}]}]."""
    from django.db.models import Count

    m = _models()
    keys = {k.key: k for k in m.ProductAttributeKey.objects.filter(company=company, filterable=True)}
    if not keys:
        return []
    rows = (
        m.ProductAttributeValue.objects.filter(product__in=product_ids_qs, key__in=list(keys))
        .values("key", "value", "num")
        .annotate(count=Count("product", distinct=True))
    )
    by_key = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)
    out = []
    for key, spec in sorted(keys.items()):
        vals = by_key.get(key)
        if not vals:
            continue
        if spec.type == "number":
            vals.sort(key=lambda r: (r["num"] is None, r["num"] or 0, r["value"]))
        else:
            vals.sort(key=lambda r: r["value"].lower())
        out.append({
            "key": key,
            "name": key_name(spec, lang),
            "unit": spec.unit or None,
            "values": [{"value": r["value"], "count": r["count"]} for r in vals],
        })
    return out


def key_names(spec) -> dict:
    raw = spec.name if isinstance(spec.name, dict) else {}
    base = raw.get("ru") or next((v for v in raw.values() if v), "") or spec.key
    return {lang: (raw.get(lang) or base) for lang in LANGS}


def key_name(spec, lang="ru") -> str:
    return key_names(spec)[lang if lang in LANGS else "ru"]


def public_attributes(product, company, request=None, ctx=None):
    """Атрибуты для публичной карточки: [{key, name, value, unit}] (имя и единица — из справочника)."""
    attrs = product.attributes or []
    if not attrs:
        return []
    cache = ctx.setdefault("_hw_keys", {}) if ctx is not None else {}
    if company.pk not in cache:
        cache[company.pk] = company_keys(company)
    keys = cache[company.pk]
    lang = lang_of(request)
    out = []
    for a in attrs:
        spec = keys.get(a.get("key"))
        out.append({
            "key": a.get("key"),
            "name": key_name(spec, lang) if spec else a.get("key"),
            "value": a.get("value"),
            "unit": a.get("unit") or (spec.unit if spec and spec.unit else None),
        })
    return out


HARDWARE_DEFAULT_KEYS = [
    # key, ru, en, ky, tr, uz, type, unit, filterable
    ("diameter_mm", "Диаметр", "Diameter", "Диаметри", "Çap", "Diametr", "number", "мм", True),
    ("length_m", "Длина", "Length", "Узундугу", "Uzunluk", "Uzunlik", "number", "м", True),
    ("width_mm", "Ширина", "Width", "Туурасы", "Genişlik", "Kenglik", "number", "мм", True),
    ("thickness_mm", "Толщина", "Thickness", "Калыңдыгы", "Kalınlık", "Qalinlik", "number", "мм", True),
    ("section_mm2", "Сечение", "Cross-section", "Кесилиши", "Kesit", "Kesim", "number", "мм²", True),
    ("cores", "Количество жил", "Number of cores", "Жылдардын саны", "Damar sayısı", "Tomirlar soni", "number", "", True),
    ("color_ral", "Цвет (RAL)", "Colour (RAL)", "Түсү (RAL)", "Renk (RAL)", "Rang (RAL)", "text", "", True),
    ("grade", "Марка", "Grade", "Маркасы", "Marka", "Markasi", "text", "", True),
    ("gost", "ГОСТ", "GOST", "ГОСТ", "GOST", "GOST", "text", "", False),
    ("pack_area_m2", "м² в упаковке", "m² per pack", "Таңгактагы м²", "Paketteki m²", "Qadoqdagi m²", "number", "м²", False),
    ("consumption_per_m2", "Расход на м²", "Consumption per m²", "1 м² үчүн чыгым", "m² başına sarfiyat",
     "1 m² uchun sarf", "number", "", False),
    ("coverage_m2_per_l", "Покрытие, м² на литр", "Coverage, m² per litre", "Каптоо, литрине м²",
     "Kaplama, litre başına m²", "Qoplash, litriga m²", "number", "", False),
    ("layers", "Количество слоёв", "Number of layers", "Катмарлардын саны", "Kat sayısı", "Qatlamlar soni", "number", "", False),
]


def ensure_default_keys(company):
    m = _models()
    created = []
    for key, ru, en, ky, tr, uz, typ, unit, filt in HARDWARE_DEFAULT_KEYS:
        _, was = m.ProductAttributeKey.objects.get_or_create(
            company=company, key=key,
            defaults={"name": {"ru": ru, "ky": ky, "en": en, "tr": tr, "uz": uz}, "type": typ, "unit": unit, "filterable": filt},
        )
        if was:
            created.append(key)
    return created


# ======================================================================
# Б-58. Ячейка хранения
# ======================================================================
def normalize_location(raw):
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if len(raw) > MAX_LOCATION_LEN:
        raise ApiError("invalid_storage_location", f"Ячейка — не больше {MAX_LOCATION_LEN} символов.", status_code=400,
                       fields={"storage_location": ["Слишком длинное значение."]})
    return raw


def set_location(product, location, branch):
    """Записать (или очистить пустой строкой/null) ячейку товара в филиале (None — главный)."""
    m = _models()
    location = normalize_location(location)
    qs = m.ProductStorageLocation.objects.filter(product=product)
    qs = qs.filter(branch=branch) if branch is not None else qs.filter(branch__isnull=True)
    if not location:
        qs.delete()
        return ""
    row = qs.first()
    if row is None:
        m.ProductStorageLocation.objects.create(company_id=product.company_id, product=product, branch=branch, location=location)
    elif row.location != location:
        row.location = location
        row.save(update_fields=["location", "updated_at"])
    return location


def _location_map(product_ids, branch):
    m = _models()
    qs = m.ProductStorageLocation.objects.filter(product_id__in=product_ids)
    qs = qs.filter(branch=branch) if branch is not None else qs.filter(branch__isnull=True)
    return {r.product_id: r.location for r in qs}


# ======================================================================
# Б-59. Сопутствующие и аналоги
# ======================================================================
def normalize_related(raw, product, company):
    """related_input -> список проверенных строк. Ошибка — ApiError 400 invalid_related."""
    m = _models()

    def bad(msg, idx=None):
        field = "related_input" if idx is None else f"related_input[{idx}]"
        raise ApiError("invalid_related", msg, status_code=400, fields={field: [msg]})

    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        bad("Ожидается массив.")
    if len(raw) > MAX_RELATED:
        bad(f"Не больше {MAX_RELATED} связей у товара.")
    rows, seen, ids = [], set(), []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            bad("Связь должна быть объектом {product, kind, ratio?, ratio_unit?, sort_order?}.", i)
        try:
            pid = uuid.UUID(str(row.get("product")))
        except ValueError:
            bad("product — id товара.", i)
        if pid == product.pk:
            bad("Товар нельзя связать с самим собой.", i)
        kind = row.get("kind") or m.ProductRelation.Kind.ACCESSORY
        if kind not in m.ProductRelation.Kind.values:
            bad("kind: accessory или analog.", i)
        if (pid, kind) in seen:
            bad("Такая связь указана дважды.", i)
        seen.add((pid, kind))
        ratio = row.get("ratio")
        if ratio in (None, ""):
            ratio = None
        else:
            d = _num(ratio)
            if d is None or d <= 0 or d >= Decimal("10") ** 9:
                bad("ratio — положительное число.", i)
            ratio = d.quantize(Decimal("0.0001"))
        unit = row.get("ratio_unit") or ""
        if not isinstance(unit, str) or len(unit) > 8:
            bad("ratio_unit — строка до 8 символов.", i)
        try:
            order = int(row.get("sort_order", i))
            if order < 0:
                raise ValueError
        except (TypeError, ValueError):
            bad("sort_order — целое число ≥ 0.", i)
        rows.append({"related_product_id": pid, "kind": kind, "ratio": ratio, "ratio_unit": unit.strip(), "sort_order": order})
        ids.append(pid)
    if ids:
        found = set(m.Product.objects.filter(pk__in=ids, company=company).values_list("pk", flat=True))
        for i, r in enumerate(rows):
            if r["related_product_id"] not in found:
                bad("Товар не найден в вашей компании.", i)
    return rows


def set_related(product, raw, company=None):
    m = _models()
    rows = normalize_related(raw, product, company or product.company)
    m.ProductRelation.objects.filter(product=product).delete()
    m.ProductRelation.objects.bulk_create(
        [m.ProductRelation(company_id=product.company_id, product=product, **r) for r in rows]
    )


def _related_map(product_ids):
    m = _models()
    out = {}
    qs = m.ProductRelation.objects.filter(product_id__in=product_ids).select_related("related_product")
    for r in qs.order_by("sort_order", "id"):
        out.setdefault(r.product_id, []).append(r)
    return out


def _related_row(r):
    p = r.related_product
    return {
        "product": str(r.related_product_id),
        "product_name": p.name,
        "product_price": str(p.price) if p.price is not None else None,
        "product_unit": p.unit,
        "product_quantity": str(p.quantity),
        "kind": r.kind,
        "ratio": format(r.ratio.normalize(), "f") if r.ratio is not None else None,
        "ratio_unit": r.ratio_unit,
        "sort_order": r.sort_order,
    }


# ======================================================================
# Крючки для ProductSerializer
# ======================================================================
def _data_of(serializer):
    d = getattr(serializer, "initial_data", None)
    return d if hasattr(d, "get") else {}


def _apply(data, request, view, product):
    company = product.company
    if "attributes" in data:
        set_attributes(product, data.get("attributes"), company)
    if "storage_location" in data:
        branch = resolve_branch(request, company, view, raw=data.get("branch"))
        set_location(product, data.get("storage_location"), branch)
        touch_product(product)
    if "related_input" in data:
        set_related(product, data.get("related_input"), company)
        touch_product(product)


def apply_input(serializer, product):
    """После create/update товара: attributes, storage_location (+branch), related_input из тела запроса."""
    data = _data_of(serializer)
    if data:
        _apply(data, serializer.context.get("request"), serializer.context.get("view"), product)


def apply_request_data(view, request, data, product):
    """То же для вьюх create-manual / create-by-barcode, которые пишут товар без ProductSerializer.create."""
    if hasattr(data, "get"):
        _apply(data, request, view, product)


def _page_products(serializer, instance):
    parent = getattr(serializer, "parent", None)
    items = getattr(parent, "instance", None)
    if isinstance(items, (list, tuple)) and items:
        return list(items)
    return [instance]


def extend_representation(serializer, instance, data):
    """Добавить в ответ товара attributes, storage_location, related (одним запросом на страницу)."""
    ctx = serializer.context
    request = ctx.get("request")
    cache = ctx.setdefault("_hw_cache", {})
    if "ids" not in cache or instance.pk not in cache["ids"]:
        products = _page_products(serializer, instance)
        ids = {p.pk for p in products} | {instance.pk}
        raw = None  # при записи (PATCH/POST) ответ показывает филиал из тела запроса
        body = getattr(request, "data", None)
        if request is not None and request.method in ("POST", "PATCH", "PUT") and hasattr(body, "get"):
            raw = body.get("branch")
        try:
            branch = resolve_branch(request, company_of(request) or instance.company, ctx.get("view"), raw=raw)
        except ApiError:
            branch = None
        cache["ids"] = ids
        cache["loc"] = _location_map(ids, branch)
        cache["rel"] = _related_map(ids)
    data["attributes"] = instance.attributes or []
    data["storage_location"] = cache["loc"].get(instance.pk, "")
    data["related"] = [_related_row(r) for r in cache["rel"].get(instance.pk, [])]
    if ctx.get("include_purchase_batches"):  # детальная карточка: ячейки по всем филиалам
        m = _models()
        data["storage_locations"] = [
            {"branch": str(r.branch_id) if r.branch_id else "main", "location": r.location}
            for r in m.ProductStorageLocation.objects.filter(product=instance).order_by("location")
        ]
    return data


# ======================================================================
# Б-58. Ревизия по ячейкам
# ======================================================================
def check_location_prefix(view, request, rows):
    """inventory/sessions/ {"location_prefix": "Б-04", items:[…]}: в акте только товары ряда.

    Возвращает доп. поля ответа ({} без префикса). Ошибка — ApiError 400 product_not_in_location.
    """
    data = request.data
    prefix = (data.get("location_prefix") if hasattr(data, "get") else "") or ""
    prefix = str(prefix).strip()
    if not prefix:
        return {}
    m = _models()
    company = company_of(request)
    branch = resolve_branch(request, company, view, raw=data.get("branch") if hasattr(data, "get") else None)
    locs = m.ProductStorageLocation.objects.filter(company=company, location__istartswith=prefix)
    locs = locs.filter(branch=branch) if branch is not None else locs.filter(branch__isnull=True)
    expected = set(locs.values_list("product_id", flat=True))
    counted = {r["product_id"] for r in rows}
    outside = [str(pid) for pid in counted if pid not in expected]
    if outside:
        raise ApiError(
            "product_not_in_location",
            f"Товары вне ячеек «{prefix}…»: {len(outside)} шт.",
            status_code=400,
            data={"location_prefix": prefix, "product_ids": outside[:50]},
            fields={"items": ["Есть товары не из этого ряда."]},
        )
    return {
        "location_prefix": prefix,
        "expected_count": len(expected),
        "uncounted_count": len(expected - counted),
    }


# ======================================================================
# Витрина (публично)
# ======================================================================
def _in_stock_q():
    m = _models()
    return Q(quantity__gt=0) | ~Q(kind=m.Product.Kind.PRODUCT) | Exists(
        m.ProductVariant.objects.filter(product=OuterRef("pk"), is_active=True, quantity__gt=0)
    )


def public_related(product, company, request, ctx, limit=12):
    """Сопутствующие и аналоги для публичной карточки — только товары в наличии и видимые на витрине.

    Если вручную ничего не задано, добираем товары из связанных категорий (category-relations/), source="category".
    """
    from apps.main.showcase import services as showcase_svc

    m = _models()
    catalog = ctx.get("showcase_catalog")

    def visible(qs):
        qs = qs.filter(company=company).filter(_in_stock_q()).exclude(status=m.Product.Status.ARCHIVED)
        if catalog is not None:
            qs = showcase_svc.apply_catalog_visibility(qs, catalog)
            if catalog.hide_zero_price:
                qs = qs.filter(price__gt=0)
        return qs

    def card(p):
        imgs = [i for i in p.images.all() if getattr(i, "image", None)]
        imgs.sort(key=lambda x: (not bool(getattr(x, "is_primary", False)), getattr(x, "created_at", None) or ""))
        url = None
        if imgs:
            url = request.build_absolute_uri(imgs[0].image.url) if request else imgs[0].image.url
        price = p.price or Decimal("0")
        disc = p.discount_percent or Decimal("0")
        final = (price * (Decimal("1") - disc / Decimal("100"))).quantize(Decimal("0.01")) if disc > 0 else price
        return {
            "id": str(p.pk), "title": p.name, "unit": p.unit, "price": str(price),
            "final_price": str(final), "image_url": url,
        }

    rels = list(m.ProductRelation.objects.filter(product=product).order_by("sort_order", "id"))
    avail = {}
    if rels:
        qs = visible(m.Product.objects.filter(pk__in=[r.related_product_id for r in rels])).prefetch_related("images")
        avail = {p.pk: p for p in qs}
    out = []
    for r in rels:
        p = avail.get(r.related_product_id)
        if p is None:
            continue
        out.append({
            **card(p), "kind": r.kind, "source": "manual",
            "ratio": format(r.ratio.normalize(), "f") if r.ratio is not None else None,
            "ratio_unit": r.ratio_unit,
        })
    if not out and product.category_id:
        crs = list(m.CategoryRelation.objects.filter(category_id=product.category_id))
        if crs:
            kind_by_cat = {c.related_category_id: c.kind for c in crs}
            qs = visible(m.Product.objects.filter(category_id__in=list(kind_by_cat)).exclude(pk=product.pk))
            for p in qs.prefetch_related("images").order_by("-created_at", "id")[:limit]:
                out.append({**card(p), "kind": kind_by_cat.get(p.category_id), "source": "category", "ratio": None, "ratio_unit": ""})
    return out[:limit]


def extend_public(serializer, instance, data, with_related=True):
    """Публичная карточка: attributes (и related в детальной карточке)."""
    ctx = serializer.context
    request = ctx.get("request")
    company = ctx.get("showcase_company") or instance.company
    data["attributes"] = public_attributes(instance, company, request, ctx)
    if with_related:
        data["related"] = public_related(instance, company, request, ctx)
    return data
