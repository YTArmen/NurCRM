"""
POST /api/main/products/mass-incoming/ — «Провести приход» массового сканирования одним запросом.

Одна транзакция: создаёт недостающие карточки (из глобальной базы или вручную),
обновляет цены и прибавляет остаток к актуальному в базе. Любая ошибка — откат всего.
"""
from decimal import Decimal, InvalidOperation

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
from apps.users.models import Company

MAX_ITEMS = 500


class MassIncomingError(Exception):
    def __init__(self, http_status, detail, index=None):
        super().__init__(detail)
        self.http_status = http_status
        self.detail = detail
        self.index = index

    def response(self):
        body = {"detail": self.detail}
        if self.index is not None:
            body["index"] = self.index
        return Response(body, status=self.http_status)


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


def _bool(raw):
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class ProductMassIncomingAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [IsAuthenticated]

    @idempotent
    def post(self, request, *args, **kwargs):
        company = self._company()
        items = request.data.get("items") if isinstance(request.data, dict) else None
        if not isinstance(items, list) or not items:
            return Response({"detail": "Передайте непустой список items."}, status=status.HTTP_400_BAD_REQUEST)
        if len(items) > MAX_ITEMS:
            return Response(
                {"detail": f"Не больше {MAX_ITEMS} позиций за один приход."}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            parsed = [self._parse_item(i, raw) for i, raw in enumerate(items)]
            self._check_duplicate_barcodes(parsed)
            with transaction.atomic():
                # Создание карточек по штрихкоду в компании — по очереди: в модели нет
                # уникальности (company, barcode), это последняя защита от дублей при гонке.
                if any(p["kind"] == "create" for p in parsed):
                    Company.objects.select_for_update().filter(pk=company.pk).first()
                created, result = self._apply(company, parsed)
        except MassIncomingError as e:
            return e.response()

        return Response(
            {"created": created, "items": result},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    # --- разбор ---

    def _parse_item(self, index, raw):
        if not isinstance(raw, dict):
            raise MassIncomingError(400, f"Позиция {index + 1}: ожидается объект.", index)
        quantity = _decimal(raw.get("quantity"), "quantity", index, required=True)
        if raw.get("product_id"):
            return {
                "kind": "add",
                "index": index,
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
        return {
            "kind": "create",
            "index": index,
            "barcode": barcode,
            "name": name,
            "quantity": quantity,
            "price": _decimal(raw.get("price"), "price", index, required=True),
            "from_global": _bool(raw.get("from_global")),
        }

    @staticmethod
    def _check_duplicate_barcodes(parsed):
        seen = set()
        for p in parsed:
            if p["kind"] != "create":
                continue
            if p["barcode"] in seen:
                raise MassIncomingError(
                    400, f"Штрихкод {p['barcode']} указан в приходе дважды.", p["index"]
                )
            seen.add(p["barcode"])

    # --- применение ---

    def _apply(self, company, parsed):
        adds = [p for p in parsed if p["kind"] == "add"]
        products = {}
        if adds:
            ids = {p["product_id"] for p in adds}
            qs = _filter_products_company_only(self, Product.objects.all()).filter(id__in=ids)
            products = {str(p.id): p for p in qs.select_for_update()}
            for p in adds:
                if p["product_id"] not in products:
                    raise MassIncomingError(404, f"Товар {p['product_id']} не найден", p["index"])

        created = []
        result = []
        for p in parsed:
            if p["kind"] == "add":
                product = products[p["product_id"]]
                product.quantity = Decimal(str(product.quantity or 0)) + p["quantity"]
                fields = ["quantity", "updated_at"]
                old_price = Decimal(str(product.price or 0))
                if p.get("purchase_price") is not None:
                    product.purchase_price = p["purchase_price"]
                    fields.append("purchase_price")
                if p["price"] is not None and p["price"] != product.price:
                    product.price = p["price"]
                    product.markup_percent = _calc_markup(product.purchase_price or Decimal("0"), p["price"])
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
                new_price = Decimal(str(product.price or 0))
                if new_price.quantize(Decimal("0.01")) != old_price.quantize(Decimal("0.01")):
                    admin_audit.log_request(
                        self.request, "price_change", "product", product.pk, product.name,
                        before={"price": str(old_price)}, after={"price": str(new_price)}, company=company,
                    )
            else:
                product = self._create(company, p)
                created.append({"barcode": p["barcode"], "product_id": str(product.id)})
            result.append({"product_id": str(product.id), "quantity": str(product.quantity)})
        return created, result

    def _create(self, company, p):
        barcode = p["barcode"]
        if (
            Product.objects.filter(company=company, barcode=barcode).exists()
            or ProductAlternateBarcode.objects.filter(company=company, barcode=barcode).exists()
        ):
            raise MassIncomingError(
                409, f"Товар со штрихкодом {barcode} уже есть в каталоге компании", p["index"]
            )
        brand = category = None
        if p["from_global"]:
            gp = GlobalProduct.objects.select_related("brand", "category").filter(barcode=barcode).first()
            if gp is None:
                raise MassIncomingError(
                    400, f"Штрихкод {barcode} не найден в глобальной базе NurCRM.", p["index"]
                )
            if gp.brand:
                brand = ProductBrand.objects.get_or_create(company=company, name=gp.brand.name)[0]
            if gp.category:
                category = ProductCategory.objects.get_or_create(company=company, name=gp.category.name)[0]
        # Как create-by-barcode / create-manual: карточка уровня компании, закупочная цена 0.
        return Product.objects.create(
            company=company,
            branch=None,
            name=p["name"],
            barcode=barcode,
            brand=brand,
            category=category,
            purchase_price=Decimal("0"),
            markup_percent=_calc_markup(Decimal("0"), p["price"]),
            price=p["price"],
            quantity=p["quantity"],
            date=timezone.now(),
            created_by=self.request.user,
        )
