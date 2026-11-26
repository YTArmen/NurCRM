"""ТЗ 26.10, Б-52 (дробное количество) и Б-53 (единицы продажи): общие функции для крючков.

Крючки в старом коде (pos_views, pos_bulk, kassa_views, serializers) только вызывают эти функции.

Правило слияния строк корзины (Б-52 п.4): строка = товар + упаковка (sale_package) + вариант
+ единица продажи (sale_unit). Количества строк с одним ключом складываются; строки с разной
единицей продажи или упаковкой не сливаются никогда. Дробное количество само по себе слиянию не
мешает. Разная цена / скидка у одной и той же пары товар+единица остаются одной строкой (как и раньше:
в БД на корзину и товар разрешена одна строка без упаковки и варианта).
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from core.api_compat import is_hardware_strict
from core.exceptions import ApiError

Q3 = Decimal("0.001")
Q4 = Decimal("0.0001")

UNIT_CODES = ("pcs", "m", "m2", "m3", "kg", "l", "t", "pack", "box", "bag", "roll", "sheet", "set", "pair")


def q3(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(Q3, rounding=ROUND_HALF_UP)


def _dec(value, field):
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ApiError("validation_error", f"Поле {field}: ожидается число.", fields={field: ["Ожидается число."]},
                       top={field: ["Ожидается число."]})


# ---------------------------------------------------------------- Б-52: поля товара

def parse_quantity_fields(data, *, partial=True) -> dict:
    """Достаёт и проверяет unit_code/quantity_step/min_sale_quantity/quantity_decimals из тела запроса.

    Возвращает только пришедшие поля (null = «как сейчас»). Ошибка -> ApiError 400.
    """
    out = {}
    if not hasattr(data, "get"):
        return out

    def bad(field, msg):
        return ApiError("validation_error", msg, fields={field: [msg]}, top={field: [msg]})

    if "unit_code" in data:
        v = data.get("unit_code")
        v = "" if v is None else str(v).strip()
        if v and v not in UNIT_CODES:
            raise bad("unit_code", "Допустимые коды единицы: " + ", ".join(UNIT_CODES) + ".")
        out["unit_code"] = v
    if "quantity_step" in data:
        v = data.get("quantity_step")
        if v in (None, ""):
            out["quantity_step"] = None
        else:
            d = _dec(v, "quantity_step")
            if d <= 0:
                raise bad("quantity_step", "Шаг должен быть больше нуля.")
            out["quantity_step"] = d.quantize(Q3, rounding=ROUND_HALF_UP)
            if out["quantity_step"] <= 0:
                raise bad("quantity_step", "Шаг не меньше 0.001.")
    if "min_sale_quantity" in data:
        v = data.get("min_sale_quantity")
        if v in (None, ""):
            out["min_sale_quantity"] = None
        else:
            d = _dec(v, "min_sale_quantity")
            if d < 0:
                raise bad("min_sale_quantity", "Минимум не может быть отрицательным.")
            out["min_sale_quantity"] = d.quantize(Q3, rounding=ROUND_HALF_UP)
    if "quantity_decimals" in data:
        v = data.get("quantity_decimals")
        if v in (None, ""):
            out["quantity_decimals"] = None
        else:
            try:
                n = int(str(v))
            except ValueError:
                raise bad("quantity_decimals", "Ожидается целое от 0 до 3.")
            if not 0 <= n <= 3:
                raise bad("quantity_decimals", "Знаков в количестве: от 0 до 3.")
            out["quantity_decimals"] = n
    return out


def apply_quantity_fields(product, data, *, save=True) -> dict:
    """Крючок для создания/правки товара: проставляет поля Б-52 из тела запроса."""
    fields = parse_quantity_fields(data)
    for k, v in fields.items():
        setattr(product, k, v)
    if fields and save:
        product.save(update_fields=list(fields))
    return fields


# ---------------------------------------------------------------- Б-52: проверка шага и минимума

def _step_of(product):
    step = getattr(product, "quantity_step", None)
    dec = getattr(product, "quantity_decimals", None)
    if step is not None and Decimal(str(step)) > 0:
        return Decimal(str(step))
    if dec is not None:
        return Decimal(1).scaleb(-int(dec))
    return None


def quantity_violation(product, base_qty):
    """None или (code, data) по шагу/минимуму товара. base_qty — в базовых единицах товара."""
    if product is None or getattr(product, "kind", None) == "service":
        return None
    q = Decimal(str(base_qty or 0))
    step = _step_of(product)
    if step is not None and q > 0:
        # q кратно step: остаток от деления на шаг равен нулю
        if (q % step) != 0:
            return "quantity_step_mismatch", {"step": format(step.normalize(), "f")}
    mn = getattr(product, "min_sale_quantity", None)
    if mn is not None and Decimal(str(mn)) > 0 and q < Decimal(str(mn)):
        return "quantity_below_min", {"min": str(q3(mn))}
    return None


def check_line_quantity(request, company, product, qty, *, sale_unit=None, index=None):
    """Крючок продажи: под флагом hardware_2026_11 и только новый клиент -> 400 при нарушении шага/минимума.

    qty — количество строки (в единицах продажи, если указан sale_unit).
    """
    if product is None or not is_hardware_strict(request, company):
        return
    base = q3(qty) * (Decimal(str(sale_unit.factor)) if sale_unit is not None else 1)
    v = quantity_violation(product, base)
    if not v:
        return
    code, data = v
    if code == "quantity_step_mismatch":
        detail = f"Количество должно быть кратно шагу {data['step']}."
    else:
        detail = f"Количество меньше минимума продажи {data['min']}."
    if index is not None:
        from apps.main.kassa_views import _line_error

        err = _line_error(code, index, detail, **{"data": data})
        err.extra_data = data
        raise err
    raise ApiError(code, detail, data=data, top={"errors": [{"index": None, "code": code, "detail": detail, "data": data}]})
