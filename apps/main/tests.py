from decimal import Decimal, ROUND_FLOOR
from types import SimpleNamespace

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.main.models import Cart, CartItem, Client, Product, ProductAlternateBarcode
from apps.main.pos_views import (
    _effective_qty_from_scale_data,
    _finalize_scale_data_for_product,
    _parse_scale_barcode,
    _parse_scale_barcode_loose,
    _scale_barcode_variants,
    _should_use_main_stock_in_agent_sale,
    _weight_from_amount,
)
from apps.main.models import cart_line_base, scale_amount_step
from apps.main.views import ProductWarehouseBarcodeAPIView, ProductCreateManualAPIView
from apps.users.models import (
    Roles,
    Company,
    Branch,
    SCALE_BARCODE_AMOUNT_UNIT_SOM,
    SCALE_BARCODE_AMOUNT_UNIT_TIYIN,
    SCALE_BARCODE_LAYOUT_CODE,
    SCALE_BARCODE_LAYOUT_PLU,
    SCALE_BARCODE_MODE_AMOUNT,
    SCALE_BARCODE_MODE_AUTO,
)
from django.contrib.auth import get_user_model

User = get_user_model()


class PosOwnerAgentSaleTests(TestCase):
    def test_owner_uses_main_stock_even_when_agent_selected(self):
        owner = SimpleNamespace(role=Roles.OWNER, id="owner-id", company=None)
        selected_agent = SimpleNamespace(id="agent-id")

        self.assertTrue(
            _should_use_main_stock_in_agent_sale(user=owner, acting_agent=selected_agent)
        )


class PosScaleBarcodeTests(TestCase):
    # Итоговый префикс (25): в ШК зашита СТОИМОСТЬ в сомах.
    BARCODE = "2500001000441"
    # Весовой префикс (20): в ШК зашит ВЕС в граммах.
    WEIGHT_BARCODE = "2004626002149"

    def test_parse_scale_barcode_amount_in_tiyin(self):
        # По умолчанию сумма в ШК считается тыйынами: «00044» → 0.44 сом.
        data = _parse_scale_barcode(self.BARCODE)
        self.assertIsNotNone(data)
        self.assertEqual(data["prefix"], "25")
        self.assertEqual(data["plu"], 1)
        self.assertEqual(data["raw_code"], "00001")
        self.assertEqual(data["amount_raw"], "00044")
        self.assertEqual(data["amount"], Decimal("0.44"))
        self.assertEqual(data["check_digit"], "1")
        self.assertEqual(data["mode"], "amount_plain")
        self.assertNotIn("weight_kg", data)

    def test_parse_scale_barcode_amount_in_som(self):
        # Переключатель «Сом»: поле читается как целые сомы, «00044» → 44 сом.
        data = _parse_scale_barcode(
            self.BARCODE,
            SCALE_BARCODE_MODE_AUTO,
            SCALE_BARCODE_LAYOUT_PLU,
            SCALE_BARCODE_AMOUNT_UNIT_SOM,
        )
        self.assertEqual(data["amount"], Decimal("44"))
        self.assertEqual(data["amount_unit"], SCALE_BARCODE_AMOUNT_UNIT_SOM)
        self.assertEqual(data["mode"], "amount_plain")

    def test_parse_weight_barcode_grams_to_kg(self):
        # Весовой штрихкод ШТРИХ: поле = вес в граммах, без деления на цену.
        data = _parse_scale_barcode(self.WEIGHT_BARCODE)
        self.assertIsNotNone(data)
        self.assertEqual(data["prefix"], "20")
        self.assertEqual(data["plu"], 4626)
        self.assertEqual(data["mode"], "weight")
        self.assertEqual(data["weight_raw"], 214)
        self.assertEqual(data["weight_kg"], Decimal("0.214"))
        self.assertNotIn("amount", data)

    def test_weight_barcode_effective_qty_independent_of_price(self):
        # 0.214 кг берётся напрямую из ШК; цена за кг не влияет на вес.
        scale_data = _parse_scale_barcode(self.WEIGHT_BARCODE)
        product = SimpleNamespace(price=Decimal("100"))
        # Для весового режима finalize — no-op (не делит на цену).
        self.assertIsNone(_finalize_scale_data_for_product(product, scale_data))
        self.assertNotIn("quantity_kg", scale_data)
        self.assertEqual(
            _effective_qty_from_scale_data(scale_data, Decimal("9.000")),
            Decimal("0.214"),
        )

    def test_quantity_amount_44_price_44(self):
        scale_data = _parse_scale_barcode(
            self.BARCODE,
            SCALE_BARCODE_MODE_AUTO,
            SCALE_BARCODE_LAYOUT_PLU,
            SCALE_BARCODE_AMOUNT_UNIT_SOM,
        )
        product = SimpleNamespace(price=Decimal("44"))
        self.assertIsNone(_finalize_scale_data_for_product(product, scale_data))
        self.assertEqual(scale_data["quantity_kg"], Decimal("1.000"))
        self.assertEqual(scale_data["mode"], "amount_plain")
        self.assertEqual(scale_data["amount"], Decimal("44"))
        self.assertEqual(scale_data["plu"], 1)

    def test_finalize_rejects_zero_price(self):
        scale_data = _parse_scale_barcode(self.BARCODE)
        product = SimpleNamespace(price=Decimal("0"))
        err = _finalize_scale_data_for_product(product, scale_data)
        self.assertEqual(err, "Невозможно рассчитать вес: у товара не указана цена")
        self.assertNotIn("quantity_kg", scale_data)

    def test_finalize_rejects_missing_price(self):
        scale_data = _parse_scale_barcode(self.BARCODE)
        product = SimpleNamespace(price=None)
        err = _finalize_scale_data_for_product(product, scale_data)
        self.assertEqual(err, "Невозможно рассчитать вес: у товара не указана цена")

    def test_effective_qty_prefers_quantity_kg_over_request_qty(self):
        scale_data = {"quantity_kg": Decimal("1.000"), "mode": "amount_plain"}
        self.assertEqual(
            _effective_qty_from_scale_data(scale_data, Decimal("9.000")),
            Decimal("1.000"),
        )

    def test_effective_qty_legacy_weight_kg(self):
        scale_data = {"weight_kg": 0.312}
        self.assertEqual(
            _effective_qty_from_scale_data(scale_data, Decimal("1.000")),
            Decimal("0.312"),
        )

    def test_parse_scale_barcode_rejects_non_weight_prefix(self):
        self.assertIsNone(_parse_scale_barcode("0100001000441"))

    def test_loose_parser_unchanged_weight_mode(self):
        loose = _parse_scale_barcode_loose(self.BARCODE)
        self.assertIsNotNone(loose)
        self.assertEqual(loose["plu"], 1)
        self.assertIn("weight_kg", loose)
        self.assertNotIn("mode", loose)


class PosScaleBarcodeVariantsTests(TestCase):
    """Запасной разбор весового ШК, когда scale_barcode_layout настроен не так,
    как печатают весы: 2 00453 00054 9 → PLU 453 (plu) либо 4530 (code)."""

    BARCODE = "2000453000549"

    def test_code_layout_falls_back_to_plu_layout(self):
        variants = _scale_barcode_variants(
            self.BARCODE, SCALE_BARCODE_MODE_AUTO, SCALE_BARCODE_LAYOUT_CODE
        )
        self.assertEqual([v["plu"] for v in variants], [4530, 453])
        self.assertEqual(variants[0]["weight_kg"], Decimal("0.054"))
        self.assertEqual(variants[1]["weight_kg"], Decimal("0.054"))

    def test_plu_layout_falls_back_to_code_layout(self):
        variants = _scale_barcode_variants(
            self.BARCODE, SCALE_BARCODE_MODE_AUTO, SCALE_BARCODE_LAYOUT_PLU
        )
        self.assertEqual([v["plu"] for v in variants], [453, 4530])

    def test_amount_barcode_fallback_stays_amount(self):
        # Префикс 25 → в поле зашита сумма. Запасная раскладка даёт другой PLU,
        # но поле по-прежнему читается как сумма (а не как вес).
        variants = _scale_barcode_variants(
            "2500001000441", SCALE_BARCODE_MODE_AUTO, SCALE_BARCODE_LAYOUT_PLU
        )
        self.assertEqual([v["plu"] for v in variants], [1, 10])
        self.assertEqual([v["mode"] for v in variants], ["amount_plain", "amount_plain"])

    def test_prefix_20_amount_in_som_reads_36(self):
        """Этикетка «Банан вес»: 2 00001 00036 6 — префикс 20, но в поле зашита
        СУММА целыми сомами (36), а не вес. Читается переключателями
        scale_barcode_mode=amount + scale_barcode_amount_unit=som."""
        variants = _scale_barcode_variants(
            "2000001000366",
            SCALE_BARCODE_MODE_AMOUNT,
            SCALE_BARCODE_LAYOUT_PLU,
            SCALE_BARCODE_AMOUNT_UNIT_SOM,
        )
        self.assertEqual([v["plu"] for v in variants], [1, 10])
        self.assertEqual([v["amount"] for v in variants], [Decimal("36"), Decimal("36")])
        self.assertEqual(variants[0]["mode"], "amount_plain")

        # 36 сом при цене 190 сом/кг → на этикетке 0.190 кг (36.10 сом округлено
        # весами до 36). Наивное 36/190 = 0.18947 дало бы «0.189».
        product = SimpleNamespace(price=Decimal("190"))
        self.assertIsNone(_finalize_scale_data_for_product(product, variants[0]))
        self.assertEqual(
            _effective_qty_from_scale_data(variants[0], Decimal("1.000")),
            Decimal("0.190"),
        )

    def test_prefix_20_default_settings_still_weight(self):
        # Без переключателей поведение прежнее: префикс 20 = вес в граммах.
        variants = _scale_barcode_variants(
            "2000001000366", SCALE_BARCODE_MODE_AUTO, SCALE_BARCODE_LAYOUT_PLU,
            SCALE_BARCODE_AMOUNT_UNIT_TIYIN,
        )
        self.assertEqual(variants[0]["mode"], "weight")
        self.assertEqual(variants[0]["weight_kg"], Decimal("0.036"))

    def test_non_scale_barcode_has_no_variants(self):
        self.assertEqual(
            _scale_barcode_variants("0100001000441", SCALE_BARCODE_MODE_AUTO,
                                    SCALE_BARCODE_LAYOUT_PLU),
            [],
        )


class PosWeightFromAmountTests(TestCase):
    """Восстановление веса из суммы на этикетке (mode=amount).

    Весы ОТБРАСЫВАЮТ дробную часть суммы, поэтому amount/price «недобирает»
    несколько грамм: фактические 0.170 кг показывались как 0.169.
    """

    def _printed_amount(self, weight_kg, price, amount_unit):
        """Сумма так, как её печатают весы: дробная часть отбрасывается.

        Проверено по этикетке «Колбаса Сервелат»: 0.170 кг × 540 сом/кг = 91.80,
        на этикетке — 91 (а не 92, как было бы при округлении).
        """
        exact = Decimal(weight_kg) * Decimal(price)
        places = Decimal("1") if amount_unit == SCALE_BARCODE_AMOUNT_UNIT_SOM else Decimal("0.01")
        return exact.quantize(places, rounding=ROUND_FLOOR)

    def _weight(self, weight_kg, price, amount_unit=SCALE_BARCODE_AMOUNT_UNIT_SOM):
        """Печатает сумму как весы и восстанавливает вес обратно."""
        printed = self._printed_amount(weight_kg, price, amount_unit)
        return _weight_from_amount(printed, Decimal(price), amount_unit)

    def test_label_from_photo(self):
        # Этикетка из жалобы: 0.170 кг, 540 сом/кг, сумма 91 (ШК 2000093000916).
        self.assertEqual(
            self._printed_amount("0.170", "540", SCALE_BARCODE_AMOUNT_UNIT_SOM),
            Decimal("91"),
        )
        self.assertEqual(
            _weight_from_amount(Decimal("91"), Decimal("540"), SCALE_BARCODE_AMOUNT_UNIT_SOM),
            Decimal("0.170"),
        )

    def test_reported_case_170_grams(self):
        for price in ("540", "390", "350", "250", "199", "190"):
            with self.subTest(price=price):
                self.assertEqual(self._weight("0.170", price), Decimal("0.170"))

    def test_grid_weights_survive_round_trip_in_som(self):
        # Интервал весов, допускаемый суммой, имеет ширину 1/цена. При цене от
        # 200 сом/кг он не шире шага весов (5 г) — вес восстанавливается однозначно.
        for weight in ("0.050", "0.185", "0.190", "0.500", "1.245", "2.000"):
            for price in ("200", "250", "390", "540", "755"):
                with self.subTest(weight=weight, price=price):
                    self.assertEqual(self._weight(weight, price), Decimal(weight))

    def test_cheap_goods_fall_back_to_plain_division(self):
        """Ниже ~200 сом/кг шаг в 1 сом грубее шага весов: сумму 22 при цене
        120 сом/кг дают и 0.185, и 0.190 кг. Не гадаем — обычное деление."""
        for weight in ("0.185", "0.190"):
            with self.subTest(weight=weight):
                self.assertEqual(self._weight(weight, "120"), Decimal("0.183"))

    def test_tiyin_amounts_keep_off_grid_weights(self):
        # Сумма в тыйынах точная → интервал узкий, вес с шага 1 г не «примагничивается».
        self.assertEqual(
            self._weight("0.168", "190", SCALE_BARCODE_AMOUNT_UNIT_TIYIN),
            Decimal("0.168"),
        )

    def test_ambiguous_stays_plain_division(self):
        """Дешёвый товар: 44 сом при цене 44 сом/кг допускает веса 1.000…1.020 кг.
        Не гадаем — отдаём обычное деление, как было до фикса."""
        self.assertEqual(
            _weight_from_amount(Decimal("44"), Decimal("44"), SCALE_BARCODE_AMOUNT_UNIT_SOM),
            Decimal("1.000"),
        )


class CartLineBaseTests(TestCase):
    """Сумма строки для весового товара считается как на весах — с отбрасыванием
    дробной части. Цена и вес при этом не трогаются и скидка не начисляется."""

    def _item(self, unit_price, quantity, is_weight):
        return SimpleNamespace(
            unit_price=Decimal(unit_price),
            quantity=Decimal(quantity),
            product=SimpleNamespace(is_weight=is_weight),
        )

    def test_weight_line_truncated_to_som(self):
        # Этикетка: 0.170 кг × 540 сом/кг = 91.80, напечатано 91.
        item = self._item("540", "0.170", True)
        self.assertEqual(cart_line_base(item, Decimal("1")), Decimal("91"))

    def test_non_weight_line_untouched(self):
        item = self._item("540", "0.170", False)
        self.assertEqual(cart_line_base(item, Decimal("1")), Decimal("91.80"))

    def test_line_without_product_untouched(self):
        item = SimpleNamespace(unit_price=Decimal("19.99"), quantity=Decimal("3"), product=None)
        self.assertEqual(cart_line_base(item, Decimal("1")), Decimal("59.97"))

    def test_tiyin_step_keeps_kopeks(self):
        # Весы в тыйынах печатают 91.80 — отбрасывать нечего.
        item = self._item("540", "0.170", True)
        self.assertEqual(cart_line_base(item, Decimal("0.01")), Decimal("91.80"))

    def test_exact_sum_not_reduced(self):
        # 0.200 кг × 540 = 108.00 ровно — отбрасывание ничего не меняет.
        item = self._item("540", "0.200", True)
        self.assertEqual(cart_line_base(item, Decimal("1")), Decimal("108"))

    def test_step_from_company_setting(self):
        self.assertEqual(
            scale_amount_step(SimpleNamespace(scale_barcode_amount_unit=SCALE_BARCODE_AMOUNT_UNIT_SOM)),
            Decimal("1"),
        )
        self.assertEqual(
            scale_amount_step(SimpleNamespace(scale_barcode_amount_unit=SCALE_BARCODE_AMOUNT_UNIT_TIYIN)),
            Decimal("0.01"),
        )


class CartRecalcWeightRoundingTestCase(TestCase):
    """Пересчёт корзины целиком: весовая строка даёт сумму как на этикетке."""

    def setUp(self):
        self.owner = User.objects.create_user(email="owner-scale@test.com", password="testpass123")
        self.company = Company.objects.create(
            name="Scale Market",
            owner=self.owner,
            scale_barcode_amount_unit=SCALE_BARCODE_AMOUNT_UNIT_SOM,
        )
        self.branch = Branch.objects.create(name="Scale Branch", company=self.company)
        self.owner.company = self.company
        self.owner.save(update_fields=["company"])
        self.sausage = Product.objects.create(
            company=self.company,
            branch=self.branch,
            name="Колбаса Сервелат Салих",
            code="S-1",
            unit="кг",
            is_weight=True,
            quantity=Decimal("50.000"),
            price=Decimal("540.00"),
            purchase_price=Decimal("400.00"),
        )
        self.cart = Cart.objects.create(
            company=self.company, branch=self.branch, user=self.owner
        )

    def _add(self, product, quantity):
        item = CartItem(
            cart=self.cart,
            company=self.company,
            branch=self.branch,
            product=product,
            quantity=Decimal(quantity),
            unit_price=product.price,
        )
        item.save(skip_full_clean=True)
        return item

    def test_weight_line_total_matches_label(self):
        # Этикетка: 0.170 кг × 540 сом/кг = 91.80 → напечатано 91.
        self._add(self.sausage, "0.170")
        self.cart.recalc()
        self.assertEqual(self.cart.subtotal, Decimal("91.00"))
        self.assertEqual(self.cart.total, Decimal("91.00"))
        self.assertEqual(self.cart.discount_total, Decimal("0.00"))

    def test_price_and_quantity_are_untouched(self):
        item = self._add(self.sausage, "0.170")
        self.cart.recalc()
        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal("540.00"))
        self.assertEqual(item.quantity, Decimal("0.170"))
        self.assertEqual(item.line_discount, Decimal("0.00"))

    def test_non_weight_product_keeps_kopeks(self):
        piece = Product.objects.create(
            company=self.company,
            branch=self.branch,
            name="Сок",
            code="J-1",
            unit="шт",
            is_weight=False,
            quantity=Decimal("10.000"),
            price=Decimal("99.90"),
            purchase_price=Decimal("70.00"),
        )
        self._add(piece, "3")
        self.cart.recalc()
        self.assertEqual(self.cart.subtotal, Decimal("299.70"))

    def test_mixed_cart_rounds_only_weight_line(self):
        piece = Product.objects.create(
            company=self.company,
            branch=self.branch,
            name="Сок",
            code="J-2",
            unit="шт",
            is_weight=False,
            quantity=Decimal("10.000"),
            price=Decimal("99.90"),
            purchase_price=Decimal("70.00"),
        )
        self._add(self.sausage, "0.170")
        self._add(piece, "1")
        self.cart.recalc()
        self.assertEqual(self.cart.subtotal, Decimal("190.90"))  # 91 + 99.90


class ProductWarehouseBarcodeAPITestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner-wh@test.com", password="testpass123")
        self.company = Company.objects.create(name="Warehouse Market Co", owner=self.owner)
        self.branch = Branch.objects.create(name="WH Branch", company=self.company)
        self.owner.company = self.company
        self.owner.save(update_fields=["company"])
        self.product = Product.objects.create(
            company=self.company,
            branch=self.branch,
            name="Сабиз",
            code="001",
            article="ART-1",
            barcode="0693123456789",
            unit="кг",
            quantity=Decimal("100.000"),
            price=Decimal("85.00"),
            purchase_price=Decimal("70.00"),
        )
        ProductAlternateBarcode.objects.create(product=self.product, barcode="0693999999999")
        self.api_factory = APIRequestFactory()

    def _get(self, barcode):
        req = self.api_factory.get(f"/main/products/warehouse-barcode/{barcode}/")
        force_authenticate(req, user=self.owner)
        return ProductWarehouseBarcodeAPIView.as_view()(req, barcode=barcode)

    def test_lookup_by_primary_barcode(self):
        resp = self._get("0693123456789")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["product"]["id"], str(self.product.id))
        self.assertEqual(resp.data["matched_barcode"], "0693123456789")
        self.assertEqual(resp.data["product"]["barcode"], "0693123456789")

    def test_lookup_by_upc_equivalent_ean13(self):
        self.product.barcode = "0762497741537"
        self.product.save(update_fields=["barcode"])

        resp = self._get("762497741537")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["product"]["id"], str(self.product.id))
        self.assertEqual(resp.data["matched_barcode"], "762497741537")

    def test_lookup_by_alternate_barcode(self):
        resp = self._get("0693999999999")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["product"]["id"], str(self.product.id))

    def test_lookup_preserves_leading_zeros(self):
        resp = self._get("0693123456789")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["matched_barcode"], "0693123456789")

    def test_not_found_returns_404(self):
        resp = self._get("0000000000000")
        self.assertEqual(resp.status_code, 404)

    def test_empty_barcode_returns_400(self):
        req = self.api_factory.get("/main/products/warehouse-barcode//")
        force_authenticate(req, user=self.owner)
        resp = ProductWarehouseBarcodeAPIView.as_view()(req, barcode="")
        self.assertEqual(resp.status_code, 400)

    def test_sync_and_search_by_alternate_barcode_name(self):
        from apps.main.serializers import sync_product_alternate_barcodes, ProductSerializer
        from apps.main.views import ProductListView

        sync_product_alternate_barcodes(
            self.product,
            [{"barcode": "0693888888888", "name": "Упаковка 10 шт"}]
        )
        self.product.refresh_from_db()
        alt = self.product.alternate_barcodes.first()
        self.assertIsNotNone(alt)
        self.assertEqual(alt.barcode, "0693888888888")
        self.assertEqual(alt.name, "Упаковка 10 шт")

        serializer = ProductSerializer(self.product)
        self.assertEqual(
            serializer.data["alternate_barcodes"],
            [{"barcode": "0693888888888", "name": "Упаковка 10 шт", "quantity": None}]
        )

        req = self.api_factory.get("/main/products/?search=Упаковка")
        force_authenticate(req, user=self.owner)
        resp = ProductListView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        found_ids = [p["id"] for p in resp.data.get("results", [])]
        self.assertIn(str(self.product.id), found_ids)

    def test_sync_alternate_barcode_quantity(self):
        from apps.main.serializers import sync_product_alternate_barcodes, ProductSerializer
        from rest_framework.exceptions import ValidationError

        sync_product_alternate_barcodes(
            self.product,
            [{"barcode": "0693888888889", "name": "Коробка 24 шт", "quantity": 24}]
        )
        self.product.refresh_from_db()
        alt = self.product.alternate_barcodes.get(barcode="0693888888889")
        self.assertEqual(alt.quantity, 24)

        serializer = ProductSerializer(self.product)
        self.assertEqual(
            serializer.data["alternate_barcodes"],
            [{"barcode": "0693888888889", "name": "Коробка 24 шт", "quantity": 24}]
        )

        # Non-positive or non-integer quantity must raise validation error
        with self.assertRaises(ValidationError) as ctx:
            sync_product_alternate_barcodes(
                self.product,
                [{"barcode": "0693888888890", "name": "Неверно", "quantity": 0}]
            )
        self.assertIn(
            "Количество в доп. штрихкоде '069388888890' должно быть положительным числом.",
            str(ctx.exception.detail.get("alternate_barcodes"))
        )

        with self.assertRaises(ValidationError) as ctx2:
            sync_product_alternate_barcodes(
                self.product,
                [{"barcode": "0693888888891", "name": "Дробное", "quantity": 2.5}]
            )
        self.assertIn(
            "Количество в доп. штрихкоде '069388888891' должно быть положительным числом.",
            str(ctx2.exception.detail.get("alternate_barcodes"))
        )


class ProductCreateManualWholesalePriceTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner-wholesale@test.com", password="testpass123")
        self.company = Company.objects.create(name="Wholesale Market Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save(update_fields=["company"])
        self.api_factory = APIRequestFactory()

    def test_create_manual_saves_wholesale_price(self):
        req = self.api_factory.post(
            "/main/products/create-manual/",
            {
                "name": "TEST wholesale",
                "barcode": "8056241343999",
                "article": "0343",
                "unit": "шт",
                "is_weight": False,
                "price": "12.24",
                "wholesale_price": "31",
                "discount_percent": "0",
                "purchase_price": "12",
                "markup_percent": "2",
                "quantity": 10,
                "stock": False,
                "packages_input": [],
                "promotion_rules_input": [],
            },
            format="json",
        )
        force_authenticate(req, user=self.owner)
        resp = ProductCreateManualAPIView.as_view()(req)
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", resp.content))
        self.assertEqual(Decimal(str(resp.data["wholesale_price"])), Decimal("31"))
        product = Product.objects.get(pk=resp.data["id"])
        self.assertEqual(product.wholesale_price, Decimal("31"))


class ServiceKindTestCase(TestCase):
    def setUp(self):
        from apps.construction.models import Cashbox, CashShift
        from apps.main.models import Cart, CartItem
        from apps.main.services import checkout_cart, apply_product_list_filters

        self.owner = User.objects.create_user(email="service-owner@test.com", password="testpass123")
        self.company = Company.objects.create(name="Service Test Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save(update_fields=["company"])
        self.cashbox = Cashbox.objects.create(name="Main Cashbox", company=self.company)
        self.shift = CashShift.objects.create(cashbox=self.cashbox, cashier=self.owner, status=CashShift.Status.OPEN)
        self.api_factory = APIRequestFactory()

    def test_create_service_manual_sets_quantity_zero(self):
        req = self.api_factory.post(
            "/main/products/create-manual/",
            {
                "name": "Стрижка модельная",
                "kind": "service",
                "barcode": "2000000000015",
                "price": "800",
                "quantity": 0,
            },
            format="json",
        )
        force_authenticate(req, user=self.owner)
        resp = ProductCreateManualAPIView.as_view()(req)
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", resp.content))
        self.assertEqual(resp.data["kind"], "service")
        self.assertEqual(Decimal(str(resp.data["quantity"])), Decimal("0"))

    def test_checkout_service_does_not_check_or_decrement_stock(self):
        from apps.main.models import Cart, CartItem, Sale
        from apps.main.services import checkout_cart

        service_prod = Product.objects.create(
            company=self.company,
            name="Консультация",
            kind=Product.Kind.SERVICE,
            price=Decimal("1500"),
            quantity=Decimal("0"),
        )
        cart = Cart.objects.create(
            company=self.company,
            user=self.owner,
            shift=self.shift,
            cashbox=self.cashbox,
            status=Cart.Status.ACTIVE,
        )
        CartItem.objects.create(
            cart=cart,
            company=self.company,
            product=service_prod,
            quantity=Decimal("5"),
            unit_price=Decimal("1500"),
        )

        sale = checkout_cart(cart)
        self.assertEqual(sale.status, Sale.Status.NEW)
        service_prod.refresh_from_db()
        # Quantity remains 0, stock not checked or decremented
        self.assertEqual(service_prod.quantity, Decimal("0"))

    def test_preset_filters_exclude_services(self):
        from apps.main.services.product_list_filters import apply_product_list_filters

        service_prod = Product.objects.create(
            company=self.company,
            name="Услуга 1",
            kind=Product.Kind.SERVICE,
            price=Decimal("500"),
            quantity=Decimal("0"),
        )
        physical_prod = Product.objects.create(
            company=self.company,
            name="Товар 1",
            kind=Product.Kind.PRODUCT,
            price=Decimal("100"),
            quantity=Decimal("0"),
        )

        qs = Product.objects.filter(company=self.company)
        filtered_qs = apply_product_list_filters(qs, {"preset": "out_of_stock"})

        res_ids = list(filtered_qs.values_list("id", flat=True))
        self.assertIn(physical_prod.id, res_ids)
        self.assertNotIn(service_prod.id, res_ids)


class SaleConsultantCommissionTests(TestCase):
    def setUp(self):
        from apps.construction.models import Cashbox, CashShift
        from apps.main.models import Cart, CartItem, MarketSaleEmployeePayProfile
        from apps.main.services import checkout_cart

        self.owner = User.objects.create_user(
            email="owner@test.com",
            password="pass",
            first_name="Владелец",
            last_name="Тест",
        )
        self.company = Company.objects.create(name="Тест Маркет Компани", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()

        self.branch = Branch.objects.create(company=self.company, name="Филиал 1")

        self.cashier = User.objects.create_user(
            email="cashier@test.com",
            password="pass",
            company=self.company,
            first_name="Кассир",
            last_name="Кассович",
        )
        self.consultant = User.objects.create_user(
            email="consultant@test.com",
            password="pass",
            company=self.company,
            first_name="Консультант",
            last_name="Тестовый",
        )

        MarketSaleEmployeePayProfile.objects.create(
            company=self.company,
            user=self.consultant,
            pay_scheme=MarketSaleEmployeePayProfile.PayScheme.PERCENT,
            sales_percent=Decimal("5.00"),
        )
        MarketSaleEmployeePayProfile.objects.create(
            company=self.company,
            user=self.cashier,
            pay_scheme=MarketSaleEmployeePayProfile.PayScheme.SALARY_PLUS_PERCENT,
            monthly_base_salary=Decimal("15000.00"),
            sales_percent=Decimal("3.00"),
        )

        self.cashbox = Cashbox.objects.create(company=self.company, branch=self.branch, name="Касса 1")
        self.shift = CashShift.objects.create(
            company=self.company,
            branch=self.branch,
            cashbox=self.cashbox,
            cashier=self.cashier,
            status=CashShift.Status.OPEN,
        )

        self.product = Product.objects.create(
            company=self.company,
            name="Консультационный товар",
            price=Decimal("1000.00"),
            quantity=Decimal("100"),
        )

    def test_checkout_with_consultant_commission_calculates_amount(self):
        from apps.main.models import Cart, CartItem, Sale
        from apps.main.services import checkout_cart

        cart = Cart.objects.create(
            company=self.company,
            branch=self.branch,
            user=self.cashier,
            shift=self.shift,
            status=Cart.Status.ACTIVE,
        )
        CartItem.objects.create(
            cart=cart,
            company=self.company,
            product=self.product,
            quantity=Decimal("2"),
            unit_price=Decimal("1000.00"),
        )

        sale = checkout_cart(
            cart,
            consultant=self.consultant,
            consultant_commission_enabled=True,
            consultant_commission_percent=Decimal("5.00"),
        )

        self.assertEqual(sale.consultant, self.consultant)
        self.assertTrue(sale.consultant_commission_enabled)
        self.assertEqual(sale.consultant_commission_percent, Decimal("5.00"))
        self.assertEqual(sale.total, Decimal("2000.00"))
        self.assertEqual(sale.consultant_commission_amount, Decimal("100.00"))

    def test_checkout_with_consultant_commission_disabled(self):
        from apps.main.models import Cart, CartItem, Sale
        from apps.main.services import checkout_cart

        cart = Cart.objects.create(
            company=self.company,
            branch=self.branch,
            user=self.cashier,
            shift=self.shift,
            status=Cart.Status.ACTIVE,
        )
        CartItem.objects.create(
            cart=cart,
            company=self.company,
            product=self.product,
            quantity=Decimal("2"),
            unit_price=Decimal("1000.00"),
        )

        sale = checkout_cart(
            cart,
            consultant=self.consultant,
            consultant_commission_enabled=False,
            consultant_commission_percent=Decimal("5.00"),
        )

        self.assertEqual(sale.consultant, self.consultant)
        self.assertFalse(sale.consultant_commission_enabled)
        self.assertEqual(sale.consultant_commission_amount, Decimal("0.00"))

    def test_salary_analytics_calculates_commission(self):
        from apps.main.models import Cart, CartItem, Sale
        from apps.main.services import checkout_cart
        from apps.main.analytics_market import AnalyticsView, Period
        from django.utils import timezone
        import datetime

        # Продажа 1: с консультантом и 5% комиссией на 2000
        cart1 = Cart.objects.create(
            company=self.company, branch=self.branch, user=self.cashier, shift=self.shift, status=Cart.Status.ACTIVE
        )
        CartItem.objects.create(cart=cart1, company=self.company, product=self.product, quantity=Decimal("2"), unit_price=Decimal("1000.00"))
        sale1 = checkout_cart(cart1, consultant=self.consultant, consultant_commission_enabled=True, consultant_commission_percent=Decimal("5.00"))
        sale1.mark_paid()

        # Продажа 2: без консультанта на 1000 (только кассир)
        cart2 = Cart.objects.create(
            company=self.company, branch=self.branch, user=self.cashier, shift=self.shift, status=Cart.Status.ACTIVE
        )
        CartItem.objects.create(cart=cart2, company=self.company, product=self.product, quantity=Decimal("1"), unit_price=Decimal("1000.00"))
        sale2 = checkout_cart(cart2)
        sale2.mark_paid()

        now = timezone.now()
        period = Period(start=now - datetime.timedelta(days=1), end=now + datetime.timedelta(days=1))

        rf = APIRequestFactory()
        req = rf.get("/main/analytics/market/?tab=salary")
        req.user = self.cashier

        res = AnalyticsView()._salary(req, self.company, self.branch, period)
        rows = res["rows"] if "rows" in res else res["tables"]["rows"]
        row_map = {r["user_id"]: r for r in rows}

        cashier_row = row_map[str(self.cashier.id)]
        consultant_row = row_map[str(self.consultant.id)]

        # База для % кассира (3%) — только продажа 2 (1000.00), т.к. продажа 1 перешла на консультанта
        self.assertEqual(cashier_row["cashier_sales_period"], "3000.00")
        self.assertEqual(cashier_row["employee_sales_period"], "1000.00")

        # Для консультанта: комиссия за период = 100.00
        self.assertEqual(consultant_row["consultant_sales_period"], "2000.00")
        self.assertEqual(consultant_row["consultant_commission_period"], "100.00")
        self.assertEqual(consultant_row["percent_bonus"], "100.00")

    def test_partial_return_recalculates_commission(self):
        from apps.main.models import Cart, CartItem, Sale, SaleItem
        from apps.main.services import checkout_cart
        from apps.main.pos_views import _execute_sale_return

        cart = Cart.objects.create(
            company=self.company, branch=self.branch, user=self.cashier, shift=self.shift, status=Cart.Status.ACTIVE
        )
        CartItem.objects.create(cart=cart, company=self.company, product=self.product, quantity=Decimal("2"), unit_price=Decimal("1000.00"))
        sale = checkout_cart(cart, consultant=self.consultant, consultant_commission_enabled=True, consultant_commission_percent=Decimal("5.00"))
        sale.mark_paid()

        item = sale.items.first()
        # Возврат 1 штуки из 2
        _execute_sale_return(sale, [(item.id, Decimal("1"))], user=self.cashier)

        sale.refresh_from_db()
        self.assertEqual(sale.total, Decimal("1000.00"))
        # Комиссия от 1000 @ 5% должна стать 50.00
        self.assertEqual(sale.consultant_commission_amount, Decimal("50.00"))

    def test_sale_consultants_endpoint(self):
        rf = APIRequestFactory()
        req = rf.get("/main/pos/sale-consultants/")
        force_authenticate(req, user=self.cashier)

        from apps.main.pos_views import SaleConsultantsAPIView
        view = SaleConsultantsAPIView.as_view()
        res = view(req)

        self.assertEqual(res.status_code, 200)
        c_map = {item["id"]: item for item in res.data}
        self.assertIn(str(self.consultant.id), c_map)
        self.assertEqual(c_map[str(self.consultant.id)]["default_commission_percent"], "5.00")


class DebtSaleReturnTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company Debt", is_active=True)
        self.branch = Branch.objects.create(name="Main Branch Debt", company=self.company)
        self.cashier = User.objects.create_user(
            username="cashier_debt",
            company=self.company,
            branch=self.branch,
            role=Roles.CASHIER,
        )
        self.client = Client.objects.create(
            company=self.company,
            branch=self.branch,
            full_name="Иванов Иван",
            phone="+996555111222",
        )
        self.product = Product.objects.create(
            company=self.company,
            name="Товар 1",
            price=Decimal("1000.00"),
            quantity=Decimal("100"),
        )
        from apps.main.models import Cashbox, CashShift
        self.cashbox = Cashbox.objects.create(company=self.company, branch=self.branch, name="Касса 1")
        self.shift = CashShift.objects.create(
            company=self.company,
            branch=self.branch,
            cashbox=self.cashbox,
            cashier=self.cashier,
            status=CashShift.Status.OPEN,
        )

    def test_checkout_debt_requires_client_id(self):
        from apps.main.models import Cart, CartItem
        from apps.main.pos_serializers import CheckoutSerializer

        cart = Cart.objects.create(
            company=self.company,
            branch=self.branch,
            user=self.cashier,
            shift=self.shift,
            status=Cart.Status.ACTIVE,
        )
        CartItem.objects.create(
            cart=cart,
            company=self.company,
            product=self.product,
            quantity=Decimal("2"),
            unit_price=Decimal("1000.00"),
        )

        # 1. Без client_id при payment_method=debt -> ValidationError
        ser = CheckoutSerializer(
            data={"payment_method": "debt"},
            context={"cart": cart},
        )
        self.assertFalse(ser.is_valid())
        self.assertIn("client_id", ser.errors)

        # 2. С client_id -> валидно
        ser_valid = CheckoutSerializer(
            data={"payment_method": "debt", "client_id": str(self.client.id)},
            context={"cart": cart},
        )
        self.assertTrue(ser_valid.is_valid(), ser_valid.errors)

    def test_full_return_debt_sale_annuls_client_deal(self):
        from apps.main.models import Cart, CartItem, ClientDeal
        from apps.main.services import checkout_cart
        from apps.main.pos_views import _execute_sale_return

        cart = Cart.objects.create(
            company=self.company,
            branch=self.branch,
            user=self.cashier,
            shift=self.shift,
            status=Cart.Status.ACTIVE,
        )
        CartItem.objects.create(
            cart=cart,
            company=self.company,
            product=self.product,
            quantity=Decimal("2"),
            unit_price=Decimal("1000.00"),
        )
        sale = checkout_cart(
            cart,
            client=self.client,
            payment_method="debt",
        )

        # Сделка создаётся автоматически при чекауте
        deal = ClientDeal.objects.filter(sale=sale).first()
        self.assertIsNotNone(deal)
        self.assertEqual(deal.remaining_debt, Decimal("2000.00"))
        self.assertTrue(deal.installments.filter(paid_on__isnull=True).exists())

        # Полный возврат чека
        debt_adj = _execute_sale_return(sale, None, user=self.cashier)

        self.assertIsNotNone(debt_adj)
        self.assertEqual(debt_adj["deal_id"], str(deal.id))
        self.assertEqual(debt_adj["reduced_by"], "2000.00")
        self.assertEqual(debt_adj["remaining_debt"], "0.00")
        self.assertEqual(debt_adj["deal_status"], "canceled")
        self.assertEqual(debt_adj["cash_refund_due"], "0.00")

        deal.refresh_from_db()
        self.assertEqual(deal.remaining_debt, Decimal("0.00"))
        # Все взносы должны быть закрыты
        self.assertFalse(deal.installments.filter(paid_on__isnull=True).exists())

    def test_full_return_with_prepayment_returns_cash_refund_due(self):
        from apps.main.models import Cart, CartItem, ClientDeal
        from apps.main.services import checkout_cart
        from apps.main.pos_views import _execute_sale_return

        cart = Cart.objects.create(
            company=self.company,
            branch=self.branch,
            user=self.cashier,
            shift=self.shift,
            status=Cart.Status.ACTIVE,
        )
        CartItem.objects.create(
            cart=cart,
            company=self.company,
            product=self.product,
            quantity=Decimal("2"),
            unit_price=Decimal("1000.00"),
        )
        sale = checkout_cart(
            cart,
            client=self.client,
            payments=[
                {"method": "cash", "amount": Decimal("500.00")},
                {"method": "debt", "amount": Decimal("1500.00")},
            ],
            cash_received=Decimal("500.00"),
        )

        # Сделка: сумма 2000, предоплата 500 -> долг 1500
        deal = ClientDeal.objects.create(
            company=self.company,
            branch=self.branch,
            client=self.client,
            sale=sale,
            title="Продажа с предоплатой",
            kind=ClientDeal.Kind.DEBT,
            amount=Decimal("2000.00"),
            prepayment=Decimal("500.00"),
            debt_days=30,
        )
        self.assertEqual(deal.remaining_debt, Decimal("1500.00"))

        # Полный возврат чека (2000)
        debt_adj = _execute_sale_return(sale, None, user=self.cashier)

        self.assertIsNotNone(debt_adj)
        self.assertEqual(debt_adj["reduced_by"], "1500.00")
        self.assertEqual(debt_adj["remaining_debt"], "0.00")
        self.assertEqual(debt_adj["cash_refund_due"], "500.00")
        self.assertEqual(debt_adj["deal_status"], "closed")

    def test_partial_return_reduces_debt_from_end(self):
        from apps.main.models import Cart, CartItem, ClientDeal
        from apps.main.services import checkout_cart
        from apps.main.pos_views import _execute_sale_return

        cart = Cart.objects.create(
            company=self.company,
            branch=self.branch,
            user=self.cashier,
            shift=self.shift,
            status=Cart.Status.ACTIVE,
        )
        CartItem.objects.create(
            cart=cart,
            company=self.company,
            product=self.product,
            quantity=Decimal("2"),
            unit_price=Decimal("1000.00"),
        )
        sale = checkout_cart(
            cart,
            client=self.client,
            payment_method="debt",
        )

        # 2 взноса по 1000 (v2: debt_months=2, interval_months=1)
        deal = ClientDeal.objects.create(
            company=self.company,
            branch=self.branch,
            client=self.client,
            sale=sale,
            title="График на 2 месяца",
            kind=ClientDeal.Kind.DEBT,
            amount=Decimal("2000.00"),
            prepayment=Decimal("0.00"),
            schedule_version="v2",
            debt_months=2,
            interval_months=1,
        )
        self.assertEqual(deal.installments.count(), 2)

        # Частичный возврат 1 штуки (1000 сом)
        item = sale.items.first()
        debt_adj = _execute_sale_return(sale, [(item.id, Decimal("1"))], user=self.cashier)

        self.assertIsNotNone(debt_adj)
        self.assertEqual(debt_adj["reduced_by"], "1000.00")
        self.assertEqual(debt_adj["remaining_debt"], "1000.00")
        self.assertEqual(debt_adj["deal_status"], "open")
        self.assertEqual(debt_adj["cash_refund_due"], "0.00")

        deal.refresh_from_db()
        self.assertEqual(deal.remaining_debt, Decimal("1000.00"))
        # Последний взнос (№2) должен быть закрыт (paid_on set, amount=0)
        inst2 = deal.installments.get(number=2)
        self.assertIsNotNone(inst2.paid_on)
        self.assertEqual(inst2.amount, Decimal("0.00"))
        # Первый взнос (№1) остаётся активным на 1000
        inst1 = deal.installments.get(number=1)
        self.assertIsNone(inst1.paid_on)
        self.assertEqual(inst1.amount, Decimal("1000.00"))


class SupplierReceiptSellingPriceTests(TestCase):
    def setUp(self):
        # Company.owner обязателен (NOT NULL): без владельца фикстура падала ещё до теста.
        owner = User.objects.create_user(email="receipt_owner@t.kg", password="x", role=Roles.OWNER)
        self.company = Company.objects.create(name="Receipt Test Co", is_active=True, owner=owner)
        owner.company = self.company
        owner.save()
        self.branch = Branch.objects.create(name="Receipt Branch", company=self.company)
        self.admin = User.objects.create_user(
            email="receipt_admin@t.kg",
            password="x",
            company=self.company,
            role=Roles.OWNER,
        )
        self.supplier = Client.objects.create(
            company=self.company,
            branch=self.branch,
            type=Client.StatusClient.SUPPLIERS,
            full_name="Поставщик ООО",
            phone="+996777000111",
        )

    def test_scenario_1_markup_recalculates_price_on_new_purchase_price(self):
        prod = Product.objects.create(
            company=self.company,
            name="Товар с наценкой",
            client=self.supplier,
            purchase_price=Decimal("100.00"),
            markup_percent=Decimal("10.0000"),
            price=Decimal("110.00"),
            quantity=Decimal("10"),
        )
        rf = APIRequestFactory()
        req = rf.post(
            f"/main/suppliers/{self.supplier.id}/receipt/",
            data={
                "items": [
                    {"product": str(prod.id), "qty": 5, "purchase_price": 120}
                ],
                "payment_type": "cash",
            },
            format="json",
        )
        force_authenticate(req, user=self.admin)
        from apps.main.views import SupplierReceiptAPIView
        view = SupplierReceiptAPIView.as_view()
        res = view(req, supplier_id=self.supplier.id)
        self.assertEqual(res.status_code, 200)

        prod.refresh_from_db()
        self.assertEqual(prod.quantity, Decimal("15"))
        self.assertEqual(prod.purchase_price, Decimal("120.000"))
        # Б-06 (27.10.2026): приход не меняет цену продажи, пересчитывается только наценка
        # ((110 − 120) / 120 = −8.33 %). Пересчёт цены — только с "recalc_price": true.
        self.assertEqual(prod.price, Decimal("110.000"))
        self.assertEqual(prod.markup_percent, Decimal("-8.3300"))

    def test_scenario_2_same_purchase_price_preserves_price(self):
        prod = Product.objects.create(
            company=self.company,
            name="Товар без изменения закупки",
            client=self.supplier,
            purchase_price=Decimal("100.00"),
            markup_percent=Decimal("10.0000"),
            price=Decimal("110.00"),
            quantity=Decimal("10"),
        )
        rf = APIRequestFactory()
        req = rf.post(
            f"/main/suppliers/{self.supplier.id}/receipt/",
            data={
                "items": [
                    {"product": str(prod.id), "qty": 5, "purchase_price": 100}
                ],
                "payment_type": "cash",
            },
            format="json",
        )
        force_authenticate(req, user=self.admin)
        from apps.main.views import SupplierReceiptAPIView
        view = SupplierReceiptAPIView.as_view()
        res = view(req, supplier_id=self.supplier.id)
        self.assertEqual(res.status_code, 200)

        prod.refresh_from_db()
        self.assertEqual(prod.quantity, Decimal("15"))
        self.assertEqual(prod.purchase_price, Decimal("100.000"))
        self.assertEqual(prod.markup_percent, Decimal("10.0000"))
        self.assertEqual(prod.price, Decimal("110.000"))

    def test_scenario_3_no_markup_preserves_manual_selling_price(self):
        prod = Product.objects.create(
            company=self.company,
            name="Товар с ручной ценой без наценки",
            client=self.supplier,
            purchase_price=Decimal("100.00"),
            markup_percent=Decimal("0.0000"),
            price=Decimal("150.00"),
            quantity=Decimal("10"),
        )
        rf = APIRequestFactory()
        req = rf.post(
            f"/main/suppliers/{self.supplier.id}/receipt/",
            data={
                "items": [
                    {"product": str(prod.id), "qty": 5, "purchase_price": 120}
                ],
                "payment_type": "cash",
            },
            format="json",
        )
        force_authenticate(req, user=self.admin)
        from apps.main.views import SupplierReceiptAPIView
        view = SupplierReceiptAPIView.as_view()
        res = view(req, supplier_id=self.supplier.id)
        self.assertEqual(res.status_code, 200)

        prod.refresh_from_db()
        self.assertEqual(prod.quantity, Decimal("15"))
        self.assertEqual(prod.purchase_price, Decimal("120.000"))
        self.assertEqual(prod.price, Decimal("150.000"))

    def test_scenario_4_mixed_products_in_receipt(self):
        prod_a = Product.objects.create(
            company=self.company,
            name="Товар A (с наценкой 10%)",
            client=self.supplier,
            purchase_price=Decimal("100.00"),
            markup_percent=Decimal("10.0000"),
            price=Decimal("110.00"),
            quantity=Decimal("10"),
        )
        prod_b = Product.objects.create(
            company=self.company,
            name="Товар B (без наценки)",
            client=self.supplier,
            purchase_price=Decimal("100.00"),
            markup_percent=Decimal("0.0000"),
            price=Decimal("150.00"),
            quantity=Decimal("10"),
        )
        rf = APIRequestFactory()
        req = rf.post(
            f"/main/suppliers/{self.supplier.id}/receipt/",
            data={
                "items": [
                    {"product": str(prod_a.id), "qty": 5, "purchase_price": 120},
                    {"product": str(prod_b.id), "qty": 5, "purchase_price": 120},
                ],
                "payment_type": "cash",
            },
            format="json",
        )
        force_authenticate(req, user=self.admin)
        from apps.main.views import SupplierReceiptAPIView
        view = SupplierReceiptAPIView.as_view()
        res = view(req, supplier_id=self.supplier.id)
        self.assertEqual(res.status_code, 200)

        prod_a.refresh_from_db()
        prod_b.refresh_from_db()

        self.assertEqual(prod_a.purchase_price, Decimal("120.000"))
        self.assertEqual(prod_a.price, Decimal("110.000"))  # Б-06: цена продажи не трогается
        self.assertEqual(prod_b.purchase_price, Decimal("120.000"))
        self.assertEqual(prod_b.price, Decimal("150.000"))

    def test_scenario_5_receipt_without_purchase_price(self):
        prod = Product.objects.create(
            company=self.company,
            name="Товар без указания закупки",
            client=self.supplier,
            purchase_price=Decimal("100.00"),
            markup_percent=Decimal("10.0000"),
            price=Decimal("110.00"),
            quantity=Decimal("10"),
        )
        rf = APIRequestFactory()
        req = rf.post(
            f"/main/suppliers/{self.supplier.id}/receipt/",
            data={
                "items": [
                    {"product": str(prod.id), "qty": 5}
                ],
                "payment_type": "cash",
            },
            format="json",
        )
        force_authenticate(req, user=self.admin)
        from apps.main.views import SupplierReceiptAPIView
        view = SupplierReceiptAPIView.as_view()
        res = view(req, supplier_id=self.supplier.id)
        self.assertEqual(res.status_code, 200)

        prod.refresh_from_db()
        self.assertEqual(prod.quantity, Decimal("15"))
        self.assertEqual(prod.purchase_price, Decimal("100.000"))
        self.assertEqual(prod.price, Decimal("110.000"))




