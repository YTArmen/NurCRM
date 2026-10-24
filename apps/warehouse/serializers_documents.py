from decimal import Decimal
from rest_framework import serializers
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.exceptions import ObjectDoesNotExist
from django.contrib.auth import get_user_model
from apps.users.models import Company
from . import models
from . import services as warehouse_services
from .serializers import (
    WarehouseProductCharacteristicsSerializer,
    StockQuantityGuardMixin,
    save_product_changed_fields,
)
from .utils import normalize_payment_kind, normalize_payment_method

User = get_user_model()


class DocumentDateField(serializers.DateTimeField):
    """Операционная дата документа.

    На вход принимает календарную дату ``YYYY-MM-DD`` (трактуется как начало дня
    в текущей таймзоне) либо ISO-8601 datetime. На выходе — ISO datetime, как и
    хранится в БД. Поле необязательное: при создании без даты модель подставит
    текущий момент, при обновлении без даты — значение не меняется.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("input_formats", ["%Y-%m-%d", "iso-8601"])
        kwargs.setdefault("required", False)
        kwargs.setdefault("error_messages", {
            "invalid": "Неверный формат даты. Ожидается YYYY-MM-DD.",
        })
        super().__init__(**kwargs)


class PaymentKindField(serializers.CharField):
    """Принимает credit/debt/cash и нормализует к Document.PaymentKind."""

    def to_internal_value(self, data):
        if data is None or data == "":
            return None
        normalized = normalize_payment_kind(data)
        allowed = {choice for choice, _label in models.Document.PaymentKind.choices}
        if normalized not in allowed:
            raise serializers.ValidationError("Укажите cash, credit (или debt) либо external.")
        return normalized


class PaymentMethodField(serializers.CharField):
    """Принимает наличные/безналичные (в т.ч. рус.) и нормализует к Document.PaymentMethod."""

    def to_internal_value(self, data):
        if data is None or data == "":
            return None
        normalized = normalize_payment_method(data)
        allowed = {choice for choice, _label in models.Document.PaymentMethod.choices}
        if normalized not in allowed:
            raise serializers.ValidationError("Укажите cash (наличными) или cashless (безналичными).")
        return normalized


def _merge_product_discount_into_item(item_data: dict) -> dict:
    """
    Если клиент не задал скидку по строке (поле нет или null) — берём процент с карточки товара.
    Явный 0% в запросе не заменяем.
    """
    if "discount_percent" in item_data and item_data.get("discount_percent") is not None:
        return item_data
    product = item_data.get("product")
    if product is None:
        return item_data
    if hasattr(product, "discount_percent"):
        pct = getattr(product, "discount_percent", None) or Decimal("0")
    else:
        row = (
            models.WarehouseProduct.objects.filter(pk=product)
            .values_list("discount_percent", flat=True)
            .first()
        )
        pct = row or Decimal("0")
    pct = Decimal(str(pct))
    if pct > 0:
        merged = dict(item_data)
        merged["discount_percent"] = pct
        return merged
    merged = dict(item_data)
    if merged.get("discount_percent") is None:
        merged["discount_percent"] = Decimal("0.00")
    return merged


def _apply_sale_price_to_item(item_data: dict, doc) -> dict:
    """
    Для документа SALE: если цена в строке не задана (поля нет или null) — берём цену
    с карточки товара. При оптовом документе (doc.is_wholesale) подставляем оптовую цену
    (с откатом на розничную, если опт не задана), иначе — розничную.
    Явно переданную цену (в т.ч. 0) не трогаем, как и не-SALE документы.
    """
    if item_data.get("price") is not None:
        return item_data
    if getattr(doc, "doc_type", None) != models.Document.DocType.SALE:
        return item_data
    product = item_data.get("product")
    if product is None:
        return item_data

    if hasattr(product, "price"):
        retail = getattr(product, "price", None) or Decimal("0")
        wholesale = getattr(product, "wholesale_price", None) or Decimal("0")
    else:
        row = (
            models.WarehouseProduct.objects.filter(pk=product)
            .values_list("price", "wholesale_price")
            .first()
        )
        retail = (row[0] if row else None) or Decimal("0")
        wholesale = (row[1] if row else None) or Decimal("0")

    retail = Decimal(str(retail))
    wholesale = Decimal(str(wholesale))
    is_wholesale = bool(getattr(doc, "is_wholesale", False))
    chosen = wholesale if (is_wholesale and wholesale > 0) else retail

    merged = dict(item_data)
    merged["price"] = chosen.quantize(Decimal("0.01"))
    return merged


def _raise_service_error(exc):
    """Ошибка сервиса склада → DRF-ответ с тем же телом/кодом (403 для прав, иначе 400)."""
    from rest_framework.exceptions import PermissionDenied

    payload = warehouse_services.error_payload(exc)
    if warehouse_services.error_status(exc) == 403:
        raise PermissionDenied(payload)
    raise serializers.ValidationError(payload)


def _fill_item_from_base(item_data: dict, base_document) -> dict:
    """
    QA B04: строка возврата по строке продажи/закупа. Товар, цена и скидка берутся из
    строки-основания, а не из запроса.
    """
    base_item = item_data.get("base_item")
    if base_item is None:
        return item_data
    if base_document is not None and base_item.document_id != base_document.pk:
        raise serializers.ValidationError(
            {"items": {"base_item": "Строка-основание не принадлежит документу-основанию.", "code": "return_base_invalid"}}
        )
    base_doc = base_item.document
    eff_pct = warehouse_services.effective_document_line_discount_percent(
        base_item.discount_percent, base_doc.discount_percent
    )
    merged = dict(item_data)
    merged["product"] = base_item.product
    merged["price"] = Decimal(base_item.price or 0)
    merged["discount_percent"] = eff_pct
    discount_amount = Decimal("0.00")
    if eff_pct <= 0 and Decimal(base_item.discount_amount or 0) > 0 and Decimal(base_item.qty or 0) > 0:
        discount_amount = (
            Decimal(base_item.discount_amount) * Decimal(item_data.get("qty") or 0) / Decimal(base_item.qty)
        ).quantize(Decimal("0.01"))
    merged["discount_amount"] = discount_amount
    return merged


class StockMoveSerializer(serializers.ModelSerializer):
    """Сериализатор движения товара с видом: приход или расход."""

    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_article = serializers.CharField(source="product.article", read_only=True)

    class Meta:
        model = models.StockMove
        fields = (
            "id",
            "document",
            "warehouse",
            "warehouse_name",
            "product",
            "product_name",
            "product_article",
            "qty_delta",
            "move_kind",
            "source_kind",
            "source_id",
            "created_at",
        )


class DocumentItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True, allow_null=True)
    product_article = serializers.CharField(source="product.article", read_only=True, allow_null=True)
    warehouse = serializers.UUIDField(source="product.warehouse_id", read_only=True, allow_null=True)
    warehouse_name = serializers.CharField(source="product.warehouse.name", read_only=True, allow_null=True)
    product_image_url = serializers.SerializerMethodField()
    product_characteristics = WarehouseProductCharacteristicsSerializer(
        source="product.characteristics",
        read_only=True,
        allow_null=True,
    )
    product_discount_percent = serializers.DecimalField(
        source="product.discount_percent",
        max_digits=5,
        decimal_places=2,
        read_only=True,
    )
    product_price = serializers.DecimalField(
        source="product.price",
        max_digits=18,
        decimal_places=3,
        read_only=True,
    )
    product_wholesale_price = serializers.DecimalField(
        source="product.wholesale_price",
        max_digits=18,
        decimal_places=3,
        read_only=True,
    )
    product_discount_amount = serializers.SerializerMethodField()
    discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True
    )
    discount_amount = serializers.DecimalField(
        max_digits=18, decimal_places=2, required=False, allow_null=True
    )
    effective_discount_percent = serializers.SerializerMethodField()
    # QA B04: строка продажи/закупа, по которой оформлен возврат.
    base_item = serializers.PrimaryKeyRelatedField(
        queryset=models.DocumentItem.objects.select_related("document", "product"),
        required=False,
        allow_null=True,
    )
    # QA B01: куда зачислен товар на складе-получателе (перемещение).
    target_product = serializers.PrimaryKeyRelatedField(read_only=True)
    target_product_name = serializers.CharField(source="target_product.name", read_only=True, allow_null=True)

    class Meta:
        model = models.DocumentItem
        fields = (
            "id",
            "base_item",
            "target_product",
            "target_product_name",
            "product",
            "product_name",
            "product_article",
            "warehouse",
            "warehouse_name",
            "product_image_url",
            "product_characteristics",
            "product_discount_percent",
            "product_price",
            "product_wholesale_price",
            "product_discount_amount",
            "qty",
            "price",
            "discount_percent",
            "discount_amount",
            "effective_discount_percent",
            "line_total",
        )

    def to_internal_value(self, data):
        ret = super().to_internal_value(data)
        # Необязательный склад позиции (мультискладские "Мои остатки"): если клиент
        # прислал `warehouse`, товар должен принадлежать этому складу. Само значение
        # не храним — склад строки берётся из product.warehouse при проведении.
        if isinstance(data, dict):
            wh_raw = data.get("warehouse")
            if wh_raw not in (None, ""):
                product = ret.get("product")
                prod_wh_id = getattr(product, "warehouse_id", None) if product is not None else None
                if prod_wh_id is not None and str(prod_wh_id) != str(wh_raw):
                    raise serializers.ValidationError(
                        {"warehouse": "Товар не принадлежит указанному складу."}
                    )
        return ret

    def get_effective_discount_percent(self, obj):
        doc = getattr(obj, "document", None)
        doc_dp = Decimal(getattr(doc, "discount_percent", None) or 0) if doc else Decimal("0")
        eff = warehouse_services.effective_document_line_discount_percent(obj.discount_percent, doc_dp)
        return eff.quantize(Decimal("0.01"))

    def get_product_discount_amount(self, obj):
        """Сумма скидки по проценту с карточки товара для текущих цены и количества в строке."""
        p = getattr(obj, "product", None)
        if not p:
            return Decimal("0.00").quantize(Decimal("0.01"))
        price = Decimal(obj.price or 0)
        qty = Decimal(obj.qty or 0)
        pct = Decimal(getattr(p, "discount_percent", None) or 0)
        return (price * qty * pct / Decimal("100")).quantize(Decimal("0.01"))

    def get_product_image_url(self, obj):
        request = self.context.get("request")
        p = getattr(obj, "product", None)
        if not p:
            return None
        img_row = None
        cache = getattr(p, "_prefetched_objects_cache", None)
        if cache and "images" in cache:
            imgs = list(p.images.all())
            img_row = imgs[0] if imgs else None
        else:
            img_row = (
                models.WarehouseProductImage.objects.filter(product=p)
                .order_by("-is_primary", "created_at")
                .first()
            )
        if not img_row or not getattr(img_row, "image", None):
            return None
        url = img_row.image.url
        return request.build_absolute_uri(url) if request else url


class DocumentSerializer(serializers.ModelSerializer):
    def to_representation(self, instance):
        data = super().to_representation(instance)
        if "items" in data and isinstance(data["items"], list):
            data["items"] = sorted(
                data["items"],
                key=lambda x: (
                    (x.get("product_name") or x.get("target_product_name") or "").lower()
                )
            )
        return data

    items = DocumentItemSerializer(many=True)
    moves = StockMoveSerializer(many=True, read_only=True)
    receipts = serializers.SerializerMethodField()
    expenses = serializers.SerializerMethodField()
    payment_kind = PaymentKindField(required=False, allow_null=True, allow_blank=True)
    payment_method = PaymentMethodField(required=False, allow_null=True, allow_blank=True)
    date = DocumentDateField(required=False)

    money_document_id = serializers.SerializerMethodField()
    money_document_number = serializers.SerializerMethodField()
    money_document_status = serializers.SerializerMethodField()
    money_document_amount = serializers.SerializerMethodField()

    cash_register_name = serializers.CharField(source="cash_register.name", read_only=True, allow_null=True)
    payment_category_title = serializers.CharField(source="payment_category.title", read_only=True, allow_null=True)
    cash_request_status = serializers.SerializerMethodField()
    cashflows = serializers.SerializerMethodField(read_only=True)

    counterparty_display_name = serializers.CharField(
        source="counterparty.name", read_only=True, allow_null=True
    )
    warehouse_from_name = serializers.CharField(
        source="warehouse_from.name", read_only=True, allow_null=True
    )
    warehouse_to_name = serializers.CharField(
        source="warehouse_to.name", read_only=True, allow_null=True
    )
    warehouse_from_display_name = serializers.SerializerMethodField()
    warehouses = serializers.SerializerMethodField()
    agent = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        allow_null=True,
        required=False,
        help_text="Агент по документу; при указании контрагент должен быть закреплён за этим агентом.",
    )
    agent_display = serializers.SerializerMethodField()
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)
    initiator_company = serializers.PrimaryKeyRelatedField(read_only=True)
    initiator_company_name = serializers.CharField(source="initiator_company.name", read_only=True, allow_null=True)
    base_document = serializers.PrimaryKeyRelatedField(
        queryset=models.Document.objects.all(), required=False, allow_null=True,
        help_text="Продажа (для SALE_RETURN) или закуп (для PURCHASE_RETURN), по которой оформлен возврат.",
    )
    base_document_number = serializers.CharField(source="base_document.number", read_only=True, allow_null=True)
    posted_negative_by = serializers.PrimaryKeyRelatedField(read_only=True)
    date_changed_by = serializers.PrimaryKeyRelatedField(read_only=True)
    pending_since = serializers.SerializerMethodField()

    class Meta:
        ref_name = "WarehouseDocumentSerializer"
        model = models.Document
        fields = (
            "id",
            "created_by",
            "created_by_email",
            "initiator_company",
            "initiator_company_name",
            "doc_type",
            "status",
            "number",
            "date",
            "payment_kind",
            "payment_method",
            "prepayment_amount",
            "warehouse_from",
            "warehouse_to",
            "warehouse_from_name",
            "warehouse_to_name",
            "warehouse_from_display_name",
            "warehouses",
            "counterparty",
            "cash_register",
            "cash_register_name",
            "payment_category",
            "payment_category_title",
            "cash_request_status",
            "money_document_id",
            "money_document_number",
            "money_document_status",
            "money_document_amount",
            "agent",
            "agent_display",
            "is_sale_request",
            "is_wholesale",
            "counterparty_display_name",
            "comment",
            "discount_percent",
            "discount_amount",
            "total",
            "items",
            "moves",
            "receipts",
            "expenses",
            "cashflows",
            "base_document",
            "base_document_number",
            "posted_negative_by",
            "posted_negative_at",
            "date_changed_by",
            "date_changed_at",
            "pending_since",
        )
        read_only_fields = (
            "number", "total", "status", "cash_request_status", "cashflows",
            "posted_negative_by", "posted_negative_at", "date_changed_by", "date_changed_at",
        )

    def get_pending_since(self, obj):
        """QA B43: с какого момента документ ждёт кассу (CASH_PENDING), иначе null."""
        if obj.status != models.Document.Status.CASH_PENDING:
            return None
        req = self._safe_one_to_one(obj, "cash_request")
        value = getattr(req, "requested_at", None) or obj.updated_at
        return serializers.DateTimeField().to_representation(value) if value else None

    def _request_user(self):
        request = self.context.get("request")
        user = getattr(request, "user", None) if request is not None else None
        return user if getattr(user, "is_authenticated", False) else None

    def _check_date_rules(self, attrs):
        """QA B07: смена даты — только с правом; закрытый период — по старой и новой дате."""
        user = self._request_user()
        if user is None:
            return
        new_date = attrs.get("date")
        old_date = getattr(self.instance, "date", None)
        company_id = None
        wh = attrs.get("warehouse_from") or attrs.get("warehouse_to")
        if wh is None and self.instance is not None:
            wh = self.instance.warehouse_from or self.instance.warehouse_to
        if wh is not None:
            company_id = wh.company_id
        elif self.instance is not None:
            company_id = warehouse_services.document_company(self.instance)
        try:
            if new_date is not None and (
                old_date is None
                or warehouse_services.document_local_date(new_date)
                != warehouse_services.document_local_date(old_date)
            ):
                warehouse_services.ensure_can_set_document_date(user=user, new_date=new_date)
            dates = [d for d in (old_date, new_date) if d is not None]
            if self.instance is not None or new_date is not None:
                warehouse_services.ensure_period_open(company=company_id, dates=dates, user=user)
        except (warehouse_services.BusinessRuleError, warehouse_services.OperationForbidden) as exc:
            _raise_service_error(exc)

    def get_cashflows(self, obj):
        from apps.construction.models import CashFlow
        from apps.construction.auto_cashflow import serialize_auto_cashflows
        company = (
            getattr(obj, "company", None)
            or getattr(getattr(obj, "warehouse_from", None), "company", None)
            or getattr(getattr(obj, "warehouse_to", None), "company", None)
            or getattr(getattr(obj, "agent", None), "company", None)
            or getattr(getattr(obj, "counterparty", None), "company", None)
            or getattr(getattr(obj, "cash_register", None), "company", None)
        )
        if company:
            cfs = CashFlow.objects.filter(company=company, source_id=str(obj.id))
        else:
            cfs = CashFlow.objects.filter(source_id=str(obj.id))
        return serialize_auto_cashflows(cfs)

    @staticmethod
    def _resolve_sale_status(doc_type, is_sale_request, current_status=None):
        if current_status in (models.Document.Status.POSTED, models.Document.Status.CASH_PENDING, models.Document.Status.REJECTED):
            return current_status
        return models.Document.Status.DRAFT

    def _apply_multi_warehouse_defaults(self, attrs):
        """Для продажи владельца: warehouse_from необязателен, подставляется из первой строки."""
        doc_type = attrs.get("doc_type") or getattr(self.instance, "doc_type", None)
        agent = attrs.get("agent")
        if agent is None and self.instance is not None:
            agent = getattr(self.instance, "agent", None)
        if doc_type not in warehouse_services.MULTI_WAREHOUSE_DOC_TYPES or agent:
            return attrs

        items = attrs.get("items")
        if not items:
            return attrs

        if attrs.get("warehouse_from") is not None:
            wh_company_id = attrs["warehouse_from"].company_id
            for it in items:
                product = it.get("product")
                if product is None:
                    continue
                if product.company_id != wh_company_id:
                    raise serializers.ValidationError(
                        {"items": "Все товары должны принадлежать той же компании, что и склад документа."}
                    )
            return attrs

        first_product = items[0].get("product")
        if first_product is not None and getattr(first_product, "warehouse_id", None):
            attrs = dict(attrs)
            attrs["warehouse_from"] = first_product.warehouse
        return attrs

    def validate(self, attrs):
        if warehouse_services.is_document_date_in_future(attrs.get("date")):
            raise serializers.ValidationError({"date": warehouse_services.DOCUMENT_FUTURE_DATE_ERROR})
        self._check_date_rules(attrs)
        base = attrs.get("base_document")
        if base is None and getattr(self.instance, "base_document", None) is None:
            # Строки ссылаются на продажу/закуп, а основание документа не задано — берём из строк.
            for it in attrs.get("items") or []:
                if it.get("base_item") is not None:
                    attrs = dict(attrs)
                    base = attrs["base_document"] = it["base_item"].document
                    break
        if base is not None:
            doc_type = attrs.get("doc_type") or getattr(self.instance, "doc_type", None)
            expected = warehouse_services.RETURN_BASE_TYPE.get(doc_type)
            if expected is None or base.doc_type != expected:
                raise serializers.ValidationError({
                    "base_document": "Основание указывается только для возврата: продажа для SALE_RETURN, закуп для PURCHASE_RETURN.",
                    "code": "return_base_invalid",
                })
            attrs = dict(attrs)
            # Контрагент и склад возврата — из основания.
            if not attrs.get("counterparty") and base.counterparty_id:
                attrs["counterparty"] = base.counterparty
            if not attrs.get("warehouse_from") and base.warehouse_from_id:
                attrs["warehouse_from"] = base.warehouse_from
        if attrs.get("items"):
            base_for_items = base if base is not None else getattr(self.instance, "base_document", None)
            attrs["items"] = [_fill_item_from_base(dict(it), base_for_items) for it in attrs["items"]]
        attrs = self._apply_multi_warehouse_defaults(attrs)
        return super().validate(attrs) if hasattr(super(), "validate") else attrs

    @staticmethod
    def _anchor_agent_warehouse_from(*, agent, doc_type, warehouse_from, items):
        """Склад-якорь для мультискладского документа агента.

        Когда единый ``warehouse_from`` не задан (клиент шлёт товары с разных складов
        своих остатков), берём его из первой позиции. Это нужно только как привязка
        документа к компании/филиалу/кассе и для фильтров списка — само списание идёт
        по складу каждой позиции (см. ``services.resolve_item_warehouse``).
        Возвращает выбранный склад или ``None``.
        """
        if not agent or warehouse_from is not None:
            return None
        if doc_type not in warehouse_services.AGENT_MULTI_WAREHOUSE_DOC_TYPES:
            return None
        if not items:
            return None
        first_product = items[0].get("product")
        if first_product is not None and getattr(first_product, "warehouse_id", None):
            return first_product.warehouse
        return None

    def _item_warehouses(self, obj):
        """Уникальные склады, задействованные в позициях мультискладского документа.

        Список пар (id, name) в порядке появления. Для одно-складского документа
        (не мультисклад) возвращает пусто — актуален единый warehouse_from.
        """
        if not warehouse_services.document_allows_multi_warehouse(obj):
            return []
        seen_ids = set()
        result = []
        for it in obj.items.all():
            p = getattr(it, "product", None)
            wh = getattr(p, "warehouse", None) if p is not None else None
            if wh is None or wh.id in seen_ids:
                continue
            seen_ids.add(wh.id)
            result.append((wh.id, wh.name))
        return result

    def get_warehouses(self, obj):
        return [{"id": str(wid), "name": name} for wid, name in self._item_warehouses(obj)]

    def get_warehouse_from_display_name(self, obj):
        """Название склада для отображения.

        Для смешанного мультискладского документа — список складов из позиций,
        иначе — имя единого склада-источника.
        """
        warehouses = self._item_warehouses(obj)
        if len(warehouses) > 1:
            return ", ".join(name for _wid, name in warehouses if name)
        if len(warehouses) == 1:
            return warehouses[0][1]
        return getattr(obj.warehouse_from, "name", None)

    def get_agent_display(self, obj):
        u = getattr(obj, "agent", None)
        if not u:
            return None
        if hasattr(u, "get_full_name") and u.get_full_name():
            return u.get_full_name()
        return (
            f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}".strip()
            or getattr(u, "username", None)
            or getattr(u, "email", None)
            or str(getattr(u, "id", ""))
        )

    @staticmethod
    def _safe_one_to_one(obj, attr: str):
        """Обратная OneToOne без записи бросает RelatedObjectDoesNotExist — getattr это не ловит."""
        try:
            return getattr(obj, attr)
        except ObjectDoesNotExist:
            return None

    def get_money_document_id(self, obj):
        md = self._safe_one_to_one(obj, "money_document")
        return md.id if md else None

    def get_money_document_number(self, obj):
        md = self._safe_one_to_one(obj, "money_document")
        return md.number if md else None

    def get_money_document_status(self, obj):
        md = self._safe_one_to_one(obj, "money_document")
        return md.status if md else None

    def get_money_document_amount(self, obj):
        md = self._safe_one_to_one(obj, "money_document")
        return md.amount if md else None

    def get_cash_request_status(self, obj):
        req = self._safe_one_to_one(obj, "cash_request")
        return getattr(req, "status", None) if req else None

    def get_receipts(self, obj):
        """Приходы — движения с move_kind=RECEIPT."""
        moves = getattr(obj, "_receipts_moves", None)
        if moves is None and hasattr(obj, "moves"):
            moves = [m for m in obj.moves.all() if m.move_kind == models.StockMove.MoveKind.RECEIPT]
        if moves is None:
            return []
        return StockMoveSerializer(moves, many=True).data

    def get_expenses(self, obj):
        """Расходы — движения с move_kind=EXPENSE."""
        moves = getattr(obj, "_expenses_moves", None)
        if moves is None and hasattr(obj, "moves"):
            moves = [m for m in obj.moves.all() if m.move_kind == models.StockMove.MoveKind.EXPENSE]
        if moves is None:
            return []
        return StockMoveSerializer(moves, many=True).data

    def create(self, validated_data):
        items = validated_data.pop("items", [])
        doc_type = validated_data.get("doc_type")
        is_sale_request = validated_data.get("is_sale_request", False)
        validated_data["status"] = self._resolve_sale_status(doc_type, is_sale_request)

        # Мультисклад агента: если единый склад не задан, привязываем документ к
        # складу первой позиции (компания/филиал/касса/фильтры). Движение по строкам
        # всё равно идёт со склада каждой позиции.
        anchor = self._anchor_agent_warehouse_from(
            agent=validated_data.get("agent"),
            doc_type=doc_type,
            warehouse_from=validated_data.get("warehouse_from"),
            items=items,
        )
        if anchor is not None:
            validated_data["warehouse_from"] = anchor

        # Автор документа (stock-partnership §6.4) — во всех созданиях через API
        request = self.context.get("request")
        if request is not None and getattr(request.user, "is_authenticated", False):
            validated_data.setdefault("created_by", request.user)

        # Валидация документа перед созданием
        doc = models.Document(**validated_data)
        try:
            doc.clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(getattr(e, "message_dict", {"detail": str(e)}))
        
        doc = super().create(validated_data)
        
        # Валидация и создание items
        for it in items:
            it = _merge_product_discount_into_item(dict(it))
            it = _apply_sale_price_to_item(it, doc)
            item = models.DocumentItem(document=doc, **it)
            try:
                item.clean()
            except DjangoValidationError as e:
                raise serializers.ValidationError(getattr(e, "message_dict", {"detail": str(e)}))
            item.save()

        warehouse_services.recalc_document_totals(doc)
        doc.refresh_from_db()
        return doc

    def update(self, instance, validated_data):
        items = validated_data.pop("items", None)

        with transaction.atomic():
            # Блокируем строку документа и перечитываем статус из БД.
            # instance прочитан в начале запроса: если параллельно шло проведение,
            # в памяти всё ещё DRAFT, и полное сохранение ниже вернуло бы документу
            # устаревший статус, оставив уже созданные складские движения
            # (документ выглядит черновиком, а товар со склада списан).
            current_status = (
                models.Document.objects.select_for_update()
                .filter(pk=instance.pk)
                .values_list("status", flat=True)
                .first()
            )
            if current_status is None:
                raise serializers.ValidationError({"detail": "Документ не найден."})
            instance.status = current_status

            # Проверяем, что документ не проведен
            if current_status in (instance.Status.POSTED, instance.Status.CASH_PENDING):
                raise serializers.ValidationError(
                    {"status": "Нельзя изменять проведенный/ожидающий кассу документ. Сначала отмените проведение."}
                )

            date_changed = "date" in validated_data and validated_data["date"] != instance.date

            # Валидация документа перед обновлением
            for key, value in validated_data.items():
                setattr(instance, key, value)
            if date_changed:
                user = self._request_user()
                instance.date_changed_by = user
                instance.date_changed_at = timezone.now()
                validated_data["date_changed_by"] = user
                validated_data["date_changed_at"] = instance.date_changed_at
            if ("is_sale_request" in validated_data) or ("doc_type" in validated_data):
                instance.status = self._resolve_sale_status(instance.doc_type, instance.is_sale_request, current_status=current_status)

            # Мультисклад агента: привязываем документ к складу первой позиции, если
            # единый склад не задан (аналогично созданию).
            if items:
                anchor = self._anchor_agent_warehouse_from(
                    agent=instance.agent,
                    doc_type=instance.doc_type,
                    warehouse_from=instance.warehouse_from,
                    items=items,
                )
                if anchor is not None:
                    instance.warehouse_from = anchor

            try:
                instance.clean()
            except DjangoValidationError as e:
                raise serializers.ValidationError(getattr(e, "message_dict", {"detail": str(e)}))

            instance = super().update(instance, validated_data)

            if items is not None:
                # Удаляем старые items
                instance.items.all().delete()

                # Валидация и создание новых items
                for it in items:
                    it = _merge_product_discount_into_item(dict(it))
                    it = _apply_sale_price_to_item(it, instance)
                    item = models.DocumentItem(document=instance, **it)
                    try:
                        item.clean()
                    except DjangoValidationError as e:
                        raise serializers.ValidationError(getattr(e, "message_dict", {"detail": str(e)}))
                    item.save()

            warehouse_services.recalc_document_totals(instance)

            if date_changed:
                # Денежный документ черновика (после отмены проведения) — с той же датой (B07).
                models.MoneyDocument.objects.filter(source_document=instance).exclude(
                    status=models.MoneyDocument.Status.POSTED
                ).update(date=instance.date)

        instance.refresh_from_db()
        return instance


class CashRequestDocumentMiniSerializer(serializers.ModelSerializer):
    warehouse_from_name = serializers.CharField(source="warehouse_from.name", read_only=True, allow_null=True)
    counterparty_display_name = serializers.CharField(source="counterparty.name", read_only=True, allow_null=True)

    class Meta:
        model = models.Document
        fields = (
            "id",
            "number",
            "doc_type",
            "status",
            "payment_kind",
            "payment_method",
            "date",
            "total",
            "warehouse_from",
            "warehouse_from_name",
            "counterparty",
            "counterparty_display_name",
            "cash_register",
            "payment_category",
        )


class CashApprovalRequestSerializer(serializers.ModelSerializer):
    document = CashRequestDocumentMiniSerializer(read_only=True)
    money_document_id = serializers.UUIDField(source="money_document.id", read_only=True, allow_null=True)
    decided_by_id = serializers.UUIDField(source="decided_by.id", read_only=True, allow_null=True)

    class Meta:
        model = models.CashApprovalRequest
        fields = (
            "id",
            "status",
            "requires_money",
            "money_doc_type",
            "amount",
            "decision_note",
            "requested_at",
            "decided_at",
            "decided_by_id",
            "money_document_id",
            "document",
        )


class CashApprovalDecisionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True)


class TransferItemInputSerializer(serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=models.WarehouseProduct.objects.all())
    qty = serializers.DecimalField(max_digits=18, decimal_places=3)
    price = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, default=Decimal("0.00"))
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=Decimal("0.00"))
    discount_amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, default=Decimal("0.00"))


class TransferCreateSerializer(serializers.Serializer):
    warehouse_from = serializers.PrimaryKeyRelatedField(queryset=models.Warehouse.objects.all())
    warehouse_to = serializers.PrimaryKeyRelatedField(queryset=models.Warehouse.objects.all())
    comment = serializers.CharField(required=False, allow_blank=True)
    items = TransferItemInputSerializer(many=True)

    def validate(self, attrs):
        items = attrs.get("items") or []
        if not items:
            raise serializers.ValidationError({"items": "Нельзя проводить пустое перемещение."})
        if attrs["warehouse_from"] == attrs["warehouse_to"]:
            raise serializers.ValidationError({"warehouse_to": "Склад-источник и склад-приемник должны быть разными."})
        return attrs


class ProductSimpleSerializer(StockQuantityGuardMixin, serializers.ModelSerializer):
    # NOTE: модельное поле называется product_group (поля `group` в модели нет).
    group = serializers.UUIDField(source="product_group.id", read_only=True)
    group_name = serializers.CharField(source="product_group.name", read_only=True)
    product_group_name = serializers.CharField(source="product_group.name", read_only=True, allow_null=True)
    brand_name = serializers.CharField(source="brand.name", read_only=True, allow_null=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True, allow_null=True)
    alternate_barcodes = serializers.SerializerMethodField()
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, allow_null=True)
    # Остаток: на update и create — проведённый документ INVENTORY (см. StockQuantityGuardMixin).
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, required=False, allow_null=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "supplier" in self.fields:
            supplier_qs = models.Counterparty.objects.filter(
                type__in=[models.Counterparty.Type.SUPPLIER, models.Counterparty.Type.BOTH]
            )
            req = self.context.get("request")
            user = getattr(req, "user", None) if req else None
            company = getattr(user, "company", None) or getattr(user, "owned_company", None)
            if company is not None:
                supplier_qs = supplier_qs.filter(company=company)
            self.fields["supplier"].queryset = supplier_qs

    class Meta:
        model = models.WarehouseProduct
        fields = (
            "id", "name", "article", "barcode",
            "unit", "is_weight",
            "quantity", "minimum_quantity",
            "purchase_price", "price", "wholesale_price", "discount_percent",
            "brand", "brand_name",
            "category",
            "product_group", "product_group_name",
            "warehouse", "warehouse_name",
            "supplier", "supplier_name",
            "status",
            "group", "group_name", "alternate_barcodes",
        )
        extra_kwargs = {
            "supplier": {"required": False, "allow_null": True},
            "minimum_quantity": {"required": False, "allow_null": True},
            "brand": {"required": False, "allow_null": True},
            "category": {"required": False, "allow_null": True},
            "product_group": {"required": False, "allow_null": True},
            "warehouse": {"required": False},
        }

    def get_alternate_barcodes(self, obj):
        return [
            {"barcode": b.barcode, "name": b.name}
            for b in obj.alternate_barcodes.order_by("barcode")
        ]

    def create(self, validated_data):
        initial_qty = validated_data.pop("quantity", None)
        validated_data["quantity"] = Decimal("0.000")
        with transaction.atomic():
            product = super().create(validated_data)
            self._post_initial_stock(product, initial_qty)
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        new_qty = validated_data.pop("quantity", None)  # проводится документом, см. _apply_quantity_change
        new_wh = validated_data.pop("warehouse", None)
        if new_wh is not None and new_wh.pk != instance.warehouse_id:
            # Остаток живёт в регистре склада товара; смена склада карточкой его бы «потеряла».
            raise serializers.ValidationError(
                {"warehouse": "Склад товара менять нельзя. Используйте документ перемещения."}
            )
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        save_product_changed_fields(instance, set(validated_data))
        self._apply_quantity_change(instance, new_qty)
        return instance


class WarehouseSimpleSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Warehouse
        fields = ("id", "name")


class CounterpartyBankAccountSerializer(serializers.ModelSerializer):
    """Пара реквизитов: Р/С и БИК создаются вместе."""

    class Meta:
        model = models.CounterpartyBankAccount
        fields = ("id", "score", "bik")
        read_only_fields = ("id",)

    def validate(self, attrs):
        score = (attrs.get("score") or "").strip()
        bik = (attrs.get("bik") or "").strip()
        if not score or not bik:
            raise serializers.ValidationError("Р/С и БИК должны указываться вместе.")
        attrs["score"] = score
        attrs["bik"] = bik
        return attrs


class CounterpartySerializer(serializers.ModelSerializer):
    company = serializers.ReadOnlyField(source="company.id")
    branch = serializers.ReadOnlyField(source="branch.id")
    agent_display = serializers.SerializerMethodField()
    analytics = serializers.SerializerMethodField()
    bank_accounts = CounterpartyBankAccountSerializer(many=True, required=False)
    agent = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        allow_null=True,
        required=False,
        help_text="Назначить контрагента агенту (только владелец/админ). Агент видит только контрагентов, назначенных ему.",
    )

    class Meta:
        model = models.Counterparty
        fields = (
            "id",
            "name",
            "phone",
            "type",
            "inn",
            "okpo",
            "score",
            "bik",
            "bank_accounts",
            "address",
            "company",
            "branch",
            "agent",
            "agent_display",
            "analytics",
        )
        read_only_fields = ("id", "company", "branch", "analytics")
        extra_kwargs = {
            "phone": {"required": False, "allow_blank": True},
        }

    def validate_inn(self, value):
        from .validators import normalize_inn, validate_inn

        if self.instance is not None and normalize_inn(value) == normalize_inn(self.instance.inn):
            return value
        return validate_inn(value)

    def create(self, validated_data):
        bank_accounts = validated_data.pop("bank_accounts", None)
        counterparty = super().create(validated_data)
        if bank_accounts:
            models.CounterpartyBankAccount.objects.bulk_create([
                models.CounterpartyBankAccount(counterparty=counterparty, **acc)
                for acc in bank_accounts
            ])
        return counterparty

    def update(self, instance, validated_data):
        bank_accounts = validated_data.pop("bank_accounts", None)
        counterparty = super().update(instance, validated_data)
        if bank_accounts is not None:
            # Полная замена набора реквизитов переданным списком.
            counterparty.bank_accounts.all().delete()
            models.CounterpartyBankAccount.objects.bulk_create([
                models.CounterpartyBankAccount(counterparty=counterparty, **acc)
                for acc in bank_accounts
            ])
        return counterparty

    def get_agent_display(self, obj):
        u = getattr(obj, "agent", None)
        if not u:
            return None
        if hasattr(u, "get_full_name") and u.get_full_name():
            return u.get_full_name()
        return f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}".strip() or getattr(u, "email", "") or str(u.id)

    def get_analytics(self, obj):
        m = self.context.get("counterparty_analytics_map")
        if not m:
            return None
        return m.get(obj.pk) or m.get(str(obj.pk))


class CompanyStockPartnershipRequestSerializer(serializers.ModelSerializer):
    from_company_name = serializers.CharField(source="from_company.name", read_only=True)
    to_company_name = serializers.CharField(source="to_company.name", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)
    decided_by_email = serializers.EmailField(source="decided_by.email", read_only=True, allow_null=True)

    class Meta:
        model = models.CompanyStockPartnershipRequest
        fields = (
            "id",
            "from_company",
            "from_company_name",
            "to_company",
            "to_company_name",
            "status",
            "note",
            "created_by",
            "created_by_email",
            "decided_by",
            "decided_by_email",
            "created_at",
            "decided_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "from_company",
            "status",
            "created_by",
            "decided_by",
            "created_at",
            "decided_at",
            "updated_at",
        )


class CompanyStockPartnershipRequestCreateSerializer(serializers.Serializer):
    to_company = serializers.PrimaryKeyRelatedField(queryset=Company.objects.all())
    note = serializers.CharField(required=False, allow_blank=True, max_length=512)



class PartnerOperationSerializer(serializers.ModelSerializer):
    """Операция «забрать у партнёра», ожидающая подтверждения (stock-partnership §7.10)."""

    initiator_company_name = serializers.CharField(source="initiator_company.name", read_only=True)
    source_company_name = serializers.CharField(source="source_company.name", read_only=True)
    warehouse_from_name = serializers.CharField(source="warehouse_from.name", read_only=True, allow_null=True)
    warehouse_to_name = serializers.CharField(source="warehouse_to.name", read_only=True, allow_null=True)
    cash_register_from_name = serializers.CharField(source="cash_register_from.name", read_only=True, allow_null=True)
    cash_register_to_name = serializers.CharField(source="cash_register_to.name", read_only=True, allow_null=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True, allow_null=True)
    decided_by_email = serializers.EmailField(source="decided_by.email", read_only=True, allow_null=True)
    document_number = serializers.CharField(source="document.number", read_only=True, allow_null=True)
    items = serializers.SerializerMethodField()

    class Meta:
        model = models.PartnerOperationRequest
        fields = (
            "id", "kind", "status",
            "initiator_company", "initiator_company_name", "source_company", "source_company_name",
            "warehouse_from", "warehouse_from_name", "warehouse_to", "warehouse_to_name", "items",
            "cash_register_from", "cash_register_from_name", "cash_register_to", "cash_register_to_name",
            "amount", "comment", "reject_reason", "error",
            "created_by_email", "decided_by_email", "created_at", "decided_at",
            "document", "document_number", "incassation",
        )

    def get_items(self, obj):
        rows = obj.items or []
        names = self.context.get("_product_names")
        if names is None:
            ids = [r.get("product") for r in rows]
            names = {str(p.id): p for p in models.WarehouseProduct.objects.filter(id__in=ids).only("id", "name", "unit")}
        out = []
        for r in rows:
            p = names.get(str(r.get("product")))
            out.append({
                "product": r.get("product"),
                "product_name": p.name if p else None,
                "unit": (p.unit if p else "") or "",
                "qty": r.get("qty"),
            })
        return out
