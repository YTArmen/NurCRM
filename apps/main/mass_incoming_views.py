"""
POST /api/main/products/mass-incoming/ — «Провести приход» массового сканирования одним запросом.

Одна транзакция: создаёт недостающие карточки (из глобальной базы или вручную),
обновляет цены и прибавляет остаток к актуальному в базе. Любая ошибка — откат всего.

ТЗ 29.10 Б-42: строка может нести `unit` (код или название единицы, применяется к товару) и
`client_row_id` (возвращается в ответе). Ответ: items[{client_row_id, product_id, quantity, unit,
purchase_price, price, price_changed}], errors[{index, client_row_id, code, detail}], replayed.
Режим частичного успеха — только по явному `partial: true` в теле: ошибочная строка не откатывает
пачку, а попадает в errors. Без флага вся пачка по-прежнему атомарна.
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.construction import audit as admin_audit
from apps.integrations.idempotency import idempotent
from apps.main.models import (
    GlobalProduct,
    Product,
    ProductAlternateBarcode,
    ProductBrand,
    ProductCategory,
)
from apps.main.views import (
    CompanyBranchRestrictedMixin,
    _calc_markup,
    _calc_price,
    _filter_products_company_only,
)
from apps.main.models import Client
from apps.users.models import Company

MAX_ITEMS = 500


class MassIncomingError(Exception):
    def __init__(self, http_status, detail, index=None, code=None, client_row_id=None):
        super().__init__(detail)
        self.http_status = http_status
        self.detail = detail
        self.index = index
        self.code = code or {404: "not_found", 409: "conflict"}.get(http_status, "validation_error")
        self.client_row_id = client_row_id

    def as_error(self):
        return {"index": self.index, "client_row_id": self.client_row_id, "code": self.code, "detail": self.detail}

    def response(self):
        body = {"detail": self.detail, "code": self.code}
        if self.index is not None:
            body["index"] = self.index
        body["errors"] = [self.as_error()]
        return Response(body, status=self.http_status)


# ТЗ 29.10 Б-42: код единицы -> название, которое хранится в Product.unit (код — в Product.unit_code).
UNIT_LABELS = {
    "pcs": "шт.", "m": "м", "m2": "м²", "m3": "м³", "kg": "кг", "l": "л", "t": "т",
    "pack": "упак.", "box": "кор.", "bag": "меш.", "roll": "рул.", "sheet": "лист", "set": "компл.", "pair": "пара",
}
# Частые написания (в нижнем регистре, без точки) -> код.
UNIT_ALIASES = {
    "шт": "pcs", "штука": "pcs", "штук": "pcs", "pc": "pcs", "piece": "pcs",
    "м": "m", "метр": "m", "м2": "m2", "м²": "m2", "м3": "m3", "м³": "m3",
    "кг": "kg", "килограмм": "kg", "kgs": "kg", "л": "l", "литр": "l", "т": "t", "тонна": "t",
    "упак": "pack", "упаковка": "pack", "пачка": "pack", "пач": "pack",
    "кор": "box", "коробка": "box", "меш": "bag", "мешок": "bag", "рул": "roll", "рулон": "roll",
    "лист": "sheet", "компл": "set", "комплект": "set", "пара": "pair", "пар": "pair",
}


def resolve_unit(raw):
    """Код единицы по коду или названию; None — неизвестная единица."""
    key = str(raw or "").strip().lower().rstrip(".").strip()
    if key in UNIT_LABELS:
        return key
    return UNIT_ALIASES.get(key)


def _decimal(raw, field, index, *, required):
    if raw in (None, ""):
        if required:
            raise MassIncomingError(400, f"Позиция {index + 1}: укажите {field}.", index)
        return None
    try:
        value = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise MassIncomingError(400, f"Позиция {index + 1}: {field} должно быть числом.", index)
    if not value.is_finite() or value < 0:
        raise MassIncomingError(400, f"Позиция {index + 1}: {field} не может быть отрицательным.", index)
    return value


def _markup(raw, index):
    """ТЗ 29.10 Б-42: наценка строки; None — не передана; ошибка — markup_invalid."""
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        value = None
    if value is None or not value.is_finite() or value < 0:
        raise MassIncomingError(
            400, f"Позиция {index + 1}: наценка должна быть неотрицательным числом.", index, code="markup_invalid"
        )
    return value


def _ref_id(raw):
    return str(raw).strip() if raw not in (None, "") else None


def _lookup_refs(company, p):
    """ТЗ 29.10 Б-42: category / supplier строки — только из своей компании."""
    category = supplier = None
    if p.get("category_id"):
        try:
            category = ProductCategory.objects.filter(company=company, pk=p["category_id"]).first()
        except (ValueError, DjangoValidationError):
            category = None
        if category is None:
            raise MassIncomingError(
                404, f"Позиция {p['index'] + 1}: категория не найдена.", p["index"], code="category_not_found"
            )
    if p.get("supplier_id"):
        try:
            supplier = Client.objects.filter(
                company=company, type=Client.StatusClient.SUPPLIERS, pk=p["supplier_id"]
            ).first()
        except (ValueError, DjangoValidationError):
            supplier = None
        if supplier is None:
            raise MassIncomingError(
                404, f"Позиция {p['index'] + 1}: поставщик не найден.", p["index"], code="supplier_not_found"
            )
    return category, supplier


def _bool(raw):
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class ProductMassIncomingAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [IsAuthenticated]

    @idempotent(replay_flag=True)
    def post(self, request, *args, **kwargs):
        company = self._company()
        items = request.data.get("items") if isinstance(request.data, dict) else None
        if not isinstance(items, list) or not items:
            return Response({"detail": "Передайте непустой список items."}, status=status.HTTP_400_BAD_REQUEST)
        if len(items) > MAX_ITEMS:
            return Response(
                {"detail": f"Не больше {MAX_ITEMS} позиций за один приход."}, status=status.HTTP_400_BAD_REQUEST
            )
        # ТЗ 29.10 Б-42: частичный успех — только по явному флагу, иначе вся пачка атомарна как раньше.
        partial = _bool(request.data.get("partial"))
        errors = []
        try:
            parsed = []
            for i, raw in enumerate(items):
                try:
                    parsed.append(self._parse_item(i, raw))
                except MassIncomingError as e:
                    if not partial:
                        raise
                    errors.append(e.as_error())
            dup_errors = self._check_duplicate_barcodes(parsed, partial)
            errors.extend(dup_errors)
            if dup_errors:
                bad = {e["index"] for e in dup_errors}
                parsed = [p for p in parsed if p["index"] not in bad]
            with transaction.atomic():
                # Создание карточек по штрихкоду в компании — по очереди: в модели нет
                # уникальности (company, barcode), это последняя защита от дублей при гонке.
                if any(p["kind"] == "create" for p in parsed):
                    Company.objects.select_for_update().filter(pk=company.pk).first()
                created, result, apply_errors = self._apply(company, parsed, partial)
                errors.extend(apply_errors)
        except MassIncomingError as e:
            return e.response()

        errors.sort(key=lambda e: e["index"] if e["index"] is not None else -1)
        return Response(
            {"created": created, "items": result, "errors": errors, "replayed": False},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    # --- разбор ---

    def _parse_item(self, index, raw):
        if not isinstance(raw, dict):
            raise MassIncomingError(400, f"Позиция {index + 1}: ожидается объект.", index)
        crid = str(raw["client_row_id"]).strip() if raw.get("client_row_id") not in (None, "") else None
        try:
            return self._parse_row(index, raw, crid)
        except MassIncomingError as e:
            e.client_row_id = crid
            raise

    def _parse_row(self, index, raw, crid):
        quantity = _decimal(raw.get("quantity"), "quantity", index, required=True)
        unit_code = None
        if raw.get("unit") not in (None, ""):
            unit_code = resolve_unit(raw.get("unit"))
            if unit_code is None:
                raise MassIncomingError(
                    400, f"Позиция {index + 1}: неизвестная единица «{raw.get('unit')}».", index, code="unit_unknown"
                )
        extra = {
            "markup_percent": _markup(raw.get("markup_percent"), index),
            "category_id": _ref_id(raw.get("category")),
            "supplier_id": _ref_id(raw.get("supplier")),
        }
        if raw.get("product_id"):
            return {
                **extra,
                "kind": "add",
                "index": index,
                "client_row_id": crid,
                "unit_code": unit_code,
                "product_id": str(raw["product_id"]).strip(),
                "quantity": quantity,
                "price": _decimal(raw.get("price"), "price", index, required=False),
                # Б-06: новая закупочная (цену продажи не меняет) и флаг пересчёта цены из наценки.
                "purchase_price": _decimal(raw.get("purchase_price"), "purchase_price", index, required=False),
                "recalc_price": _bool(raw.get("recalc_price")),
            }
        barcode = str(raw.get("barcode") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not barcode or not name or "from_global" not in raw:
            raise MassIncomingError(
                400,
                f"Позиция {index + 1}: нужен product_id или barcode, name, price, quantity и from_global.",
                index,
            )
        price = _decimal(raw.get("price"), "price", index, required=extra["markup_percent"] is None)
        purchase = _decimal(raw.get("purchase_price"), "purchase_price", index, required=False)
        from_markup = price is None
        if from_markup:  # цена = закупка × (1 + наценка/100)
            if not purchase:
                raise MassIncomingError(
                    400, f"Позиция {index + 1}: для расчёта цены по наценке укажите purchase_price.", index
                )
            price = _calc_price(purchase, extra["markup_percent"])
        return {
            **extra,
            "purchase_price": purchase if from_markup else None,
            "from_markup": from_markup,
            "kind": "create",
            "index": index,
            "client_row_id": crid,
            "unit_code": unit_code,
            "barcode": barcode,
            "name": name,
            "quantity": quantity,
            "price": price,
            "from_global": _bool(raw.get("from_global")),
        }

    @staticmethod
    def _check_duplicate_barcodes(parsed, partial=False):
        seen = set()
        errors = []
        for p in parsed:
            if p["kind"] != "create":
                continue
            if p["barcode"] in seen:
                err = MassIncomingError(
                    400, f"Штрихкод {p['barcode']} указан в приходе дважды.", p["index"],
                    code="barcode_duplicate", client_row_id=p.get("client_row_id"),
                )
                if not partial:
                    raise err
                errors.append(err.as_error())
                continue
            seen.add(p["barcode"])
        return errors

    # --- применение ---

    def _apply(self, company, parsed, partial=False):
        adds = [p for p in parsed if p["kind"] == "add"]
        products = {}
        if adds:
            ids = {p["product_id"] for p in adds}
            qs = _filter_products_company_only(self, Product.objects.all()).filter(id__in=ids)
            products = {str(p.id): p for p in qs.select_for_update()}

        created = []
        result = []
        errors = []
        for p in parsed:
            try:
                if partial:
                    with transaction.atomic():  # ошибочная строка откатывает только себя
                        row, new_card = self._apply_row(company, p, products)
                else:
                    row, new_card = self._apply_row(company, p, products)
            except MassIncomingError as e:
                e.client_row_id = p.get("client_row_id")
                if not partial:
                    raise
                errors.append(e.as_error())
                continue
            if new_card:
                created.append({"barcode": p["barcode"], "product_id": row["product_id"]})
            result.append(row)
        return created, result, errors

    def _apply_row(self, company, p, products):
        if p["kind"] == "add":
            if p["product_id"] not in products:
                raise MassIncomingError(
                    404, f"Товар {p['product_id']} не найден", p["index"], code="product_not_found"
                )
            product = products[p["product_id"]]
            category, supplier = _lookup_refs(company, p)
            product.quantity = Decimal(str(product.quantity or 0)) + p["quantity"]
            fields = ["quantity", "updated_at"]
            old_price = Decimal(str(product.price or 0))
            if p.get("unit_code"):
                product.unit = UNIT_LABELS[p["unit_code"]]
                product.unit_code = p["unit_code"]
                fields += ["unit", "unit_code"]
            if p.get("purchase_price") is not None:
                product.purchase_price = p["purchase_price"]
                fields.append("purchase_price")
            if category is not None:
                product.category = category
                fields.append("category")
            if p["price"] is not None and p["price"] != product.price:
                product.price = p["price"]
                product.markup_percent = _calc_markup(product.purchase_price or Decimal("0"), p["price"])
                fields += ["price", "markup_percent"]
            elif p["price"] is None and p.get("markup_percent") is not None:
                # ТЗ 29.10 Б-42: цены в строке нет — цена продажи от закупки и наценки.
                product.markup_percent = p["markup_percent"]
                product.price = _calc_price(Decimal(str(product.purchase_price or 0)), p["markup_percent"])
                fields += ["price", "markup_percent"]
            elif p.get("recalc_price") and Decimal(str(product.markup_percent or 0)) != Decimal("0"):
                # Б-06: пересчёт цены из закупки и наценки — только по явному флагу строки.
                product.price = _calc_price(
                    Decimal(str(product.purchase_price or 0)), Decimal(str(product.markup_percent or 0))
                )
                fields.append("price")
            elif p.get("purchase_price") is not None:
                # Закупка изменилась, цена продажи прежняя — пересчитываем только наценку.
                product.markup_percent = _calc_markup(
                    Decimal(str(product.purchase_price or 0)), Decimal(str(product.price or 0))
                )
                fields.append("markup_percent")
            product.save(update_fields=fields)
            if supplier is not None:
                product.suppliers.add(supplier)
            new_price = Decimal(str(product.price or 0))
            price_changed = new_price.quantize(Decimal("0.01")) != old_price.quantize(Decimal("0.01"))
            if price_changed:
                admin_audit.log_request(
                    self.request, "price_change", "product", product.pk, product.name,
                    before={"price": str(old_price)}, after={"price": str(new_price)}, company=company,
                )
            new_card = False
        else:
            category, supplier = _lookup_refs(company, p)
            product = self._create(company, p, category)
            if supplier is not None:
                product.suppliers.add(supplier)
            price_changed = False
            new_card = True
        row = {
            "client_row_id": p.get("client_row_id"),
            "product_id": str(product.id),
            "quantity": str(product.quantity),
            "unit": product.unit,
            "purchase_price": str(Decimal(str(product.purchase_price or 0)).quantize(Decimal("0.01"))),
            "price": str(Decimal(str(product.price or 0)).quantize(Decimal("0.01"))),
            "price_changed": price_changed,
        }
        if p.get("supplier_id"):
            row["supplier"] = p["supplier_id"]
        if p.get("category_id"):
            row["category"] = p["category_id"]
        return row, new_card

    def _create(self, company, p, ref_category=None):
        barcode = p["barcode"]
        if (
            Product.objects.filter(company=company, barcode=barcode).exists()
            or ProductAlternateBarcode.objects.filter(company=company, barcode=barcode).exists()
        ):
            raise MassIncomingError(
                409, f"Товар со штрихкодом {barcode} уже есть в каталоге компании", p["index"],
                code="barcode_exists",
            )
        brand = category = None
        if p["from_global"]:
            gp = GlobalProduct.objects.select_related("brand", "category").filter(barcode=barcode).first()
            if gp is None:
                raise MassIncomingError(
                    400, f"Штрихкод {barcode} не найден в глобальной базе NurCRM.", p["index"],
                    code="global_product_not_found",
                )
            if gp.brand:
                brand = ProductBrand.objects.get_or_create(company=company, name=gp.brand.name)[0]
            if gp.category:
                category = ProductCategory.objects.get_or_create(company=company, name=gp.category.name)[0]
        # Как create-by-barcode / create-manual: карточка уровня компании, закупочная цена 0.
        unit_kwargs = {}
        if p.get("unit_code"):  # ТЗ 29.10 Б-42: единица создаваемого товара
            unit_kwargs = {"unit": UNIT_LABELS[p["unit_code"]], "unit_code": p["unit_code"]}
        if ref_category is not None:
            category = ref_category
        purchase_price = p.get("purchase_price") or Decimal("0")
        product = Product(
            company=company,
            branch=None,
            name=p["name"],
            barcode=barcode,
            brand=brand,
            category=category,
            purchase_price=purchase_price,
            markup_percent=(
                p["markup_percent"] if p.get("from_markup")
                else _calc_markup(purchase_price, p["price"])
            ),
            price=p["price"],
            quantity=p["quantity"],
            date=timezone.now(),
            created_by=self.request.user,
            **unit_kwargs,
        )
        product._manual_price = True
        product.save()
        return product
