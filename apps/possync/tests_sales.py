"""
Тесты серверных пунктов для веб-кассы (лимит долга, упаковки, строки чеков, даты, удаления, график, возврат).
    python manage.py test apps.possync.tests_sales --settings=core.settings_test_sqlite
"""
from decimal import Decimal

from apps.construction.models import CashShift
from apps.main.models import Client, ClientDeal, Sale
from apps.users.models import CompanyAppSettings

from .models import ClientDebtLimit
from .tests import PossyncBase


class SalesBase(PossyncBase):
    def checkout(self, key, items=None, payment=None, **extra):
        body = {
            "items": items or [{"product": str(self.product.id), "qty": "1", "price": "100.00"}],
            "payment": payment or {"method": "cash", "received": "1000.00"},
        }
        body.update(extra)
        return self.api.post("/api/main/pos/checkout/", body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def debt_sale(self, key, client, amount="100.00", **extra):
        qty = str(Decimal(amount) / Decimal("100.00"))
        return self.checkout(
            key,
            items=[{"product": str(self.product.id), "qty": qty, "price": "100.00"}],
            payment={"method": "debt"},
            client=str(client.id),
            **extra,
        )

    def new_client(self, name="Должник"):
        return Client.objects.create(company=self.company, full_name=name, phone="+996555111222")


class DebtLimitTests(SalesBase):
    def test_client_limit_blocks_second_debt(self):
        self.open_shift()
        c = self.new_client()
        r = self.api.patch(f"/api/main/clients/{c.id}/", {"debt_limit": "500"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["debt_limit"], "500.00")
        self.assertEqual(r.data["debt_available"], "500.00")

        r = self.debt_sale("d1", c, "300.00")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.api.get(f"/api/main/clients/{c.id}/")
        self.assertEqual(r.data["debt_available"], "200.00")

        r = self.debt_sale("d2", c, "300.00")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_limit_exceeded")
        self.assertEqual(r.data["limit"], 500)
        self.assertEqual(r.data["current"], 300)
        self.assertIn("Лимит долга клиента 500", r.data["detail"])
        # откат: продажа и списание остатка не остались
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("47.000"))

        # в пределах лимита — проходит
        r = self.debt_sale("d3", c, "200.00")
        self.assertEqual(r.status_code, 201, r.data)

    def test_no_limit_by_default_and_null_resets(self):
        self.open_shift()
        c = self.new_client()
        r = self.api.get(f"/api/main/clients/{c.id}/")
        self.assertIsNone(r.data["debt_limit"])
        self.assertIsNone(r.data["debt_available"])
        self.assertEqual(self.debt_sale("n1", c, "900.00").status_code, 201)
        self.api.patch(f"/api/main/clients/{c.id}/", {"debt_limit": "100"}, format="json")
        self.api.patch(f"/api/main/clients/{c.id}/", {"debt_limit": None}, format="json")
        self.assertIsNone(ClientDebtLimit.objects.get(client=c).limit)
        self.assertEqual(self.debt_sale("n2", c, "100.00").status_code, 201)

    def test_prepayment_reduces_debt_part(self):
        self.open_shift()
        c = self.new_client()
        ClientDebtLimit.objects.create(client=c, company=self.company, limit=Decimal("200"))
        r = self.checkout(
            "m1",
            items=[{"product": str(self.product.id), "qty": "5", "price": "100.00"}],
            payment={"method": "debt", "cash_amount": "200.00"},  # предоплата 200, в долг 300
            client=str(c.id),
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_limit_exceeded")

    def test_cash_sale_ignores_limit(self):
        self.open_shift()
        c = self.new_client()
        ClientDebtLimit.objects.create(client=c, company=self.company, limit=Decimal("1"))
        self.assertEqual(self.checkout("c1", client=str(c.id)).status_code, 201)

    def as_cashier(self):
        cashier = type(self.owner).objects.create_user(email="kass@t.kg", password="x", role="cashier")
        cashier.company = self.company
        cashier.save()
        CashShift.objects.create(company=self.company, cashbox=self.cashbox, cashier=cashier,
                                 status=CashShift.Status.OPEN, opening_cash=Decimal("0"))
        self.api.force_authenticate(cashier)
        return cashier

    def test_cashier_limit_from_debt_access(self):
        cashier = self.as_cashier()
        CompanyAppSettings.objects.create(
            company=self.company, app="nurmarket-mobile",
            settings={"DebtAccess": [{"user": str(cashier.id), "mode": "limit", "max_receipts": 1, "max_sum": 1000}]},
        )
        c1, c2 = self.new_client("А"), self.new_client("Б")
        self.assertEqual(self.debt_sale("k1", c1, "300.00").status_code, 201)
        r = self.debt_sale("k2", c2, "100.00")  # второй открытый чек
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_limit_exceeded")
        self.assertEqual(r.data["limit"], 1)
        self.assertEqual(r.data["current"], 1)

    def test_cashier_sum_limit_and_deny(self):
        cashier = self.as_cashier()
        row = CompanyAppSettings.objects.create(
            company=self.company, app="nurmarket-mobile",
            settings={"DebtAccess": [{"user": str(cashier.id), "mode": "limit", "max_sum": 400}]},
        )
        c = self.new_client()
        self.assertEqual(self.debt_sale("s1", c, "300.00").status_code, 201)
        r = self.debt_sale("s2", c, "200.00")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["limit"], 400)
        self.assertEqual(r.data["current"], 300)
        row.settings = {"DebtAccess": [{"user": str(cashier.id), "mode": "deny"}]}
        row.save()
        r = self.debt_sale("s3", c, "100.00")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_forbidden")

    def test_wildcard_rule_and_own_rule_priority(self):
        cashier = self.as_cashier()
        CompanyAppSettings.objects.create(
            company=self.company, app="nurmarket-mobile",
            settings={"DebtAccess": [{"user": "*", "mode": "deny"}]})
        c = self.new_client()
        r = self.debt_sale("w1", c, "100.00")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_forbidden")
        # своё правило сильнее общего
        CompanyAppSettings.objects.filter(company=self.company).update(
            settings={"DebtAccess": [{"user": "*", "mode": "deny"},
                                     {"user": str(cashier.id), "mode": "allow"}]})
        self.assertEqual(self.debt_sale("w2", c, "100.00").status_code, 201)

    def test_owner_exempt_from_cashier_rules_but_not_client_limit(self):
        self.open_shift()
        CompanyAppSettings.objects.create(
            company=self.company, app="nurmarket-mobile",
            settings={"DebtAccess": [{"user": "*", "mode": "deny"},
                                     {"user": str(self.owner.id), "mode": "deny"}]})
        c = self.new_client()
        self.assertEqual(self.debt_sale("o1", c, "100.00").status_code, 201)
        ClientDebtLimit.objects.create(client=c, company=self.company, limit=Decimal("150"))
        r = self.debt_sale("o2", c, "100.00")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["scope"], "client")


class PackSaleTests(SalesBase):
    def setUp(self):
        super().setUp()
        from apps.main.models import ProductPackage

        self.product.quantity = Decimal("100")
        self.product.price = Decimal("10.00")
        self.product.purchase_price = Decimal("5.00")
        self.product.save()
        self.pack = ProductPackage.objects.create(
            company=self.company, product=self.product, name="Пачка", quantity_in_package=Decimal("20")
        )
        self.open_shift()

    def sell_packs(self, key, qty="2", price="180.00", **line):
        item = {"product": str(self.product.id), "sale_package_id": str(self.pack.id), "qty": qty}
        if price:
            item["price"] = price
        item.update(line)
        return self.checkout(key, items=[item], payment={"method": "cash", "received": "1000.00"})

    def test_stock_drops_by_qty_times_quantity_in_package_and_response(self):
        r = self.sell_packs("p1")
        self.assertEqual(r.status_code, 201, r.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("60.000"))
        it = r.data["items"][0]
        self.assertEqual(it["sale_package_id"], str(self.pack.id))
        self.assertEqual(it["package_name"], "Пачка")
        self.assertEqual(it["packs_qty"], "2.000")
        self.assertEqual(it["qty"], "40.000")  # базовые единицы
        self.assertEqual(it["pack_price"], "180.00")
        self.assertEqual(r.data["total"], "360.00")

    def test_default_pack_price_is_unit_price_times_quantity(self):
        r = self.sell_packs("p2", price=None)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["total"], "400.00")  # 2 × 20 × 10

    def test_inexact_unit_price_total_is_exact(self):
        from apps.main.models import ProductPackage

        pack3 = ProductPackage.objects.create(
            company=self.company, product=self.product, name="Тройка", quantity_in_package=Decimal("3")
        )
        r = self.checkout("p3", items=[{"product": str(self.product.id), "sale_package_id": str(pack3.id),
                                         "qty": "2", "price": "100.00"}],
                          payment={"method": "cash", "received": "1000.00"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["total"], "200.00")

    def test_return_gives_back_base_units(self):
        r = self.sell_packs("p4")
        sale_id = r.data["id"]
        item = r.data["items"][0]
        rr = self.api.post(
            f"/api/main/pos/sales/{sale_id}/return/",
            {"items": [{"sale_item_id": item["id"], "quantity": "20"}]},  # 1 пачка = 20 ед.
            format="json",
        )
        self.assertEqual(rr.status_code, 200, rr.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("80.000"))

    def test_pack_plus_loose_same_product_merges(self):
        r = self.checkout("p5", items=[
            {"product": str(self.product.id), "sale_package_id": str(self.pack.id), "qty": "1", "price": "180.00"},
            {"product": str(self.product.id), "qty": "5", "price": "10.00"},
        ], payment={"method": "cash", "received": "1000.00"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["total"], "230.00")
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("75.000"))


class DateFilterTests(SalesBase):
    def make_sale_at(self, local_dt_str):
        from datetime import datetime, timedelta, timezone as tz

        if not CashShift.objects.filter(company=self.company).exists():
            self.open_shift()
        r = self.checkout(f"dt-{local_dt_str}")
        self.assertEqual(r.status_code, 201, r.data)
        dt = datetime.fromisoformat(local_dt_str).replace(tzinfo=tz(timedelta(hours=6)))
        Sale.objects.filter(pk=r.data["id"]).update(created_at=dt)
        return r.data["id"]

    def test_sales_date_range_inclusive_bishkek(self):
        a = self.make_sale_at("2026-10-18T00:30:00")
        b = self.make_sale_at("2026-10-18T23:59:00")
        self.make_sale_at("2026-10-19T00:05:00")
        self.make_sale_at("2026-10-17T23:50:00")
        r = self.api.get("/api/main/pos/sales/?date_from=2026-10-18&date_to=2026-10-18")
        self.assertEqual(r.status_code, 200, r.data)
        ids = {x["id"] for x in r.data["results"]}
        self.assertEqual(ids, {a, b})
        self.assertEqual(r.data["count"], 2)
        r = self.api.get("/api/main/pos/sales/?date_from=2026-10-18&date_to=2026-10-19")
        self.assertEqual(r.data["count"], 3)
        r = self.api.get("/api/main/pos/sales/?date_to=2026-10-17")
        self.assertEqual(r.data["count"], 1)

    def test_sales_bad_date_is_400(self):
        r = self.api.get("/api/main/pos/sales/?date_from=вчера")
        self.assertEqual(r.status_code, 400)

    def test_cashflows_date_range(self):
        from datetime import datetime, timedelta, timezone as tz
        from apps.construction.models import CashFlow

        self.open_shift()
        self.assertEqual(self.checkout("cf-1").status_code, 201)
        flow = CashFlow.objects.filter(company=self.company).first()
        self.assertIsNotNone(flow)
        CashFlow.objects.filter(pk=flow.pk).update(
            created_at=datetime(2026, 10, 18, 0, 30, tzinfo=tz(timedelta(hours=6)))
        )
        r = self.api.get("/api/construction/cashflows/?date_from=2026-10-18&date_to=2026-10-18")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["count"], 1)
        r = self.api.get("/api/construction/cashflows/?date_from=2026-10-19&date_to=2026-10-19")
        self.assertEqual(r.data["count"], 0)


class SaleItemsTests(SalesBase):
    def test_items_filters_pagination_returned(self):
        self.open_shift()
        r1 = self.checkout("si-1", items=[
            {"product": str(self.product.id), "qty": "3", "price": "100.00", "discount": "20.00"}])
        other = self.product.__class__.objects.create(
            company=self.company, name="Молоко", price=Decimal("60.00"), quantity=Decimal("10"))
        r2 = self.checkout("si-2", items=[{"product": str(other.id), "qty": "2", "price": "60.00"}])
        self.assertEqual(r1.status_code, 201, r1.data)
        # частичный возврат 1 шт
        item_id = r1.data["items"][0]["id"]
        rr = self.api.post(f"/api/main/pos/sales/{r1.data['id']}/return/",
                           {"items": [{"sale_item_id": item_id, "quantity": "1"}]}, format="json")
        self.assertEqual(rr.status_code, 200, rr.data)

        r = self.api.get("/api/main/pos/sale-items/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["count"], 2)
        by_name = {x["name"]: x for x in r.data["results"]}
        bread = by_name["Хлеб"]
        self.assertEqual(bread["qty"], "2.000")
        self.assertEqual(bread["returned_qty"], "1.000")
        self.assertEqual(bread["sale_id"], r1.data["id"])
        self.assertEqual(bread["number"], r1.data["number"])
        self.assertEqual(bread["cashier_id"], str(self.owner.id))
        self.assertEqual(bread["payment_method"], "cash")

        r = self.api.get(f"/api/main/pos/sale-items/?product={other.id}")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["sum"], "120.00")
        r = self.api.get("/api/main/pos/sale-items/?page_size=1")
        self.assertEqual(len(r.data["results"]), 1)
        self.assertIsNotNone(r.data["next"])
        r = self.api.get(f"/api/main/pos/sale-items/?cashier={uuid_other()}")
        self.assertEqual(r.data["count"], 0)
        r = self.api.get("/api/main/pos/sale-items/?date_from=2000-01-01&date_to=2000-01-01")
        self.assertEqual(r.data["count"], 0)
        r = self.api.get("/api/main/pos/sale-items/?date_from=2020-01-01&date_to=2099-01-01&page_size=500")
        self.assertEqual(r.data["count"], 2)

    def test_other_company_not_visible(self):
        from apps.users.models import Company

        self.open_shift()
        self.checkout("si-3")
        other_owner = type(self.owner).objects.create_user(email="x@y.kg", password="x", role="owner")
        co = Company.objects.create(name="Чужая", owner=other_owner, is_active=True, subscription_plan=self.plan)
        other_owner.company = co
        other_owner.save()
        self.api.force_authenticate(other_owner)
        r = self.api.get("/api/main/pos/sale-items/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 0)

    def test_pack_info_in_items(self):
        from apps.main.models import ProductPackage

        self.open_shift()
        pk = ProductPackage.objects.create(company=self.company, product=self.product, name="Блок",
                                           quantity_in_package=Decimal("10"))
        r = self.checkout("si-4", items=[{"product": str(self.product.id), "sale_package_id": str(pk.id),
                                          "qty": "2", "price": "400.00"}])
        self.assertEqual(r.status_code, 201, r.data)
        row = self.api.get("/api/main/pos/sale-items/").data["results"][0]
        self.assertEqual(row["sale_package_id"], str(pk.id))
        self.assertEqual(row["packs_qty"], "2.000")
        self.assertEqual(row["qty"], "20.000")


def uuid_other():
    import uuid

    return str(uuid.uuid4())


class DeletedItemsTests(SalesBase):
    def test_checkout_writes_deleted_items(self):
        from apps.main.models import CartItemDeletionLog

        self.open_shift()
        r = self.checkout("del-1", deleted_items=[
            {"product": str(self.product.id), "qty": "2"},
            {"name": "Ручной товар", "qty": "1.5"},
        ])
        self.assertEqual(r.status_code, 201, r.data)
        logs = CartItemDeletionLog.objects.filter(company=self.company).order_by("product_name")
        self.assertEqual(logs.count(), 2)
        self.assertEqual({l.product_name for l in logs}, {"Хлеб", "Ручной товар"})
        self.assertEqual(logs.get(product_name="Хлеб").deleted_by_id, self.owner.id)
        # повтор того же чека не дублирует журнал
        self.checkout("del-1", deleted_items=[{"product": str(self.product.id), "qty": "2"}])
        self.assertEqual(CartItemDeletionLog.objects.filter(company=self.company).count(), 2)
        r = self.api.get("/api/main/pos/cart-item-deletions/")
        self.assertEqual(r.status_code, 200)

    def test_bad_deleted_item_rolls_back_sale(self):
        self.open_shift()
        r = self.checkout("del-2", deleted_items=[{"qty": "1"}])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 0)

    def test_standalone_endpoint(self):
        r = self.api.post("/api/main/pos/cart-item-deletions/",
                          {"items": [{"product": str(self.product.id), "qty": "3"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["created"], 1)
        r = self.api.post("/api/main/pos/cart-item-deletions/", {"items": []}, format="json")
        self.assertEqual(r.status_code, 400)


class DebtScheduleTests(SalesBase):
    def test_custom_installments_in_checkout(self):
        self.open_shift()
        c = self.new_client()
        r = self.debt_sale("sch-1", c, "900.00", debt_schedule=[
            {"due_date": "2026-11-01", "amount": "300.00"},
            {"due_date": "2026-12-01", "amount": "300.00"},
            {"due_date": "2027-01-01", "amount": "300.00"},
        ], schedule_version="v2")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual([x["due_date"] for x in r.data["debt_schedule"]], ["2026-11-01", "2026-12-01", "2027-01-01"])
        deal = ClientDeal.objects.get(sale_id=r.data["id"])
        self.assertEqual(deal.installments.count(), 3)
        self.assertEqual(r.data["deal_id"], str(deal.id))

    def test_params_schedule(self):
        self.open_shift()
        c = self.new_client()
        r = self.debt_sale("sch-2", c, "600.00", debt_schedule={
            "debt_months": 3, "interval_months": 1, "first_due_date": "2026-11-15"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual([x["amount"] for x in r.data["debt_schedule"]], ["200.00"] * 3)
        self.assertEqual(r.data["debt_schedule"][1]["due_date"], "2026-12-15")

    def test_sum_mismatch_rolls_back(self):
        self.open_shift()
        c = self.new_client()
        r = self.debt_sale("sch-3", c, "900.00", debt_schedule=[{"due_date": "2026-11-01", "amount": "100.00"}])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "invalid_debt_schedule")
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal("50.000"))

    def test_default_without_schedule_is_30_days(self):
        self.open_shift()
        c = self.new_client()
        r = self.debt_sale("sch-4", c, "300.00")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(r.data["debt_schedule"]), 1)

    def test_schedule_with_prepayment(self):
        self.open_shift()
        c = self.new_client()
        r = self.checkout("sch-5", items=[{"product": str(self.product.id), "qty": "5", "price": "100.00"}],
                          payment={"method": "debt", "cash_amount": "200.00"}, client=str(c.id),
                          debt_schedule=[{"due_date": "2026-11-01", "amount": "100.00"},
                                         {"due_date": "2026-12-01", "amount": "200.00"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(r.data["debt_schedule"]), 2)


class DealReversalTests(SalesBase):
    def setUp(self):
        super().setUp()
        self.open_shift()
        self.client_obj = self.new_client()
        r = self.debt_sale("rev-1", self.client_obj, "500.00")
        self.assertEqual(r.status_code, 201, r.data)
        self.deal = ClientDeal.objects.get(sale_id=r.data["id"])

    def pay(self, amount="300.00"):
        import uuid

        r = self.api.post(
            f"/api/main/clients/{self.client_obj.id}/deals/{self.deal.id}/pay/",
            {"amount": amount, "idempotency_key": str(uuid.uuid4()), "payment_method": "cash"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def flows(self):
        from apps.construction.models import CashFlow

        return CashFlow.objects.filter(company=self.company, source_id=str(self.deal.id))

    def test_refund_creates_reverse_expense(self):
        import uuid
        from apps.construction.models import CashFlow

        self.pay("300.00")
        self.assertEqual(self.flows().filter(type=CashFlow.Type.INCOME).count(), 1)
        key = str(uuid.uuid4())
        body = {"amount": "100.00", "idempotency_key": key}
        r = self.api.post(f"/api/main/clients/{self.client_obj.id}/deals/{self.deal.id}/refund/", body, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        exp = self.flows().filter(type=CashFlow.Type.EXPENSE)
        self.assertEqual(exp.count(), 1)
        self.assertEqual(exp.first().amount, Decimal("100.00"))
        self.assertEqual(r.data["reverse_cashflow"]["type"], "expense")
        # исходный приход в той же кассе, обратная операция тоже
        self.assertEqual(exp.first().cashbox_id, self.flows().filter(type=CashFlow.Type.INCOME).first().cashbox_id)
        # повтор того же запроса не дублирует расход
        r = self.api.post(f"/api/main/clients/{self.client_obj.id}/deals/{self.deal.id}/refund/", body, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.flows().filter(type=CashFlow.Type.EXPENSE).count(), 1)

    def test_refund_error_creates_nothing(self):
        import uuid

        r = self.api.post(f"/api/main/clients/{self.client_obj.id}/deals/{self.deal.id}/refund/",
                          {"idempotency_key": str(uuid.uuid4())}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.flows().count(), 0)

    def test_delete_deal_with_payments_reverses_net(self):
        import uuid
        from apps.construction.models import CashFlow

        self.pay("300.00")
        self.api.post(f"/api/main/clients/{self.client_obj.id}/deals/{self.deal.id}/refund/",
                      {"amount": "50.00", "idempotency_key": str(uuid.uuid4())}, format="json")
        deal_id = str(self.deal.id)
        r = self.api.delete(f"/api/main/clients/{self.client_obj.id}/deals/{self.deal.id}/")
        self.assertEqual(r.status_code, 204, getattr(r, "data", None))
        self.assertFalse(ClientDeal.objects.filter(pk=deal_id).exists())
        expenses = CashFlow.objects.filter(company=self.company, source_id=deal_id, type=CashFlow.Type.EXPENSE)
        # 50 (возврат) + 250 (чистая оплата при удалении)
        self.assertEqual(sum(e.amount for e in expenses), Decimal("300.00"))

    def test_delete_deal_without_payments_no_flows(self):
        from apps.construction.models import CashFlow

        deal_id = str(self.deal.id)
        r = self.api.delete(f"/api/main/clients/{self.client_obj.id}/deals/{self.deal.id}/")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(CashFlow.objects.filter(source_id=deal_id, type=CashFlow.Type.EXPENSE).count(), 0)


class CompanyDebtLimitsTests(SalesBase):
    """Б-14 п. 3: общий лимит долга компании."""

    def test_patch_and_get(self):
        r = self.api.patch("/api/main/debt-limits/", {"max_sales_per_day": 2, "max_amount_per_day": "500"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        r = self.api.get("/api/main/debt-limits/")
        self.assertEqual(r.data["max_sales_per_day"], 2)
        self.assertEqual(r.data["max_amount_per_day"], "500.00")
        self.assertEqual(r.data["applies_to"], "all")
        r = self.api.patch("/api/main/debt-limits/", {"applies_to": "x"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_daily_count_limit_across_cashiers(self):
        self.open_shift()
        self.api.patch("/api/main/debt-limits/", {"max_sales_per_day": 2}, format="json")
        c = self.new_client()
        self.assertEqual(self.debt_sale("l1", c, "100.00").status_code, 201)
        self.assertEqual(self.debt_sale("l2", c, "100.00").status_code, 201)
        r = self.debt_sale("l3", c, "100.00")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["code"], "debt_daily_limit_exceeded")
        self.assertEqual(Sale.objects.filter(company=self.company).count(), 2)
        r = self.api.get("/api/main/debt-limits/usage/")
        self.assertEqual(r.data["sales_count"], 2)
        self.assertEqual(r.data["sales_left"], 0)

    def test_daily_amount_and_client_limit(self):
        self.open_shift()
        self.api.patch("/api/main/debt-limits/", {"max_amount_per_day": "250", "max_debt_per_client": "400"}, format="json")
        c = self.new_client()
        self.assertEqual(self.debt_sale("a1", c, "200.00").status_code, 201)
        r = self.debt_sale("a2", c, "100.00")
        self.assertEqual(r.data["code"], "debt_daily_limit_exceeded")
        self.api.patch("/api/main/debt-limits/", {"max_amount_per_day": None}, format="json")
        r = self.debt_sale("a3", c, "300.00")
        self.assertEqual(r.data["code"], "debt_client_limit_exceeded")

    def test_cashier_cannot_patch(self):
        from django.contrib.auth import get_user_model

        u = get_user_model().objects.create_user(email="k@t.kg", password="x", role="employee", company=self.company)
        api = type(self.api)()
        api.force_authenticate(u)
        r = api.patch("/api/main/debt-limits/", {"max_sales_per_day": 1}, format="json")
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(api.get("/api/main/debt-limits/").status_code, 200)

    def test_offline_not_rejected_gives_warning(self):
        from django.utils import timezone

        self.open_shift()
        self.api.patch("/api/main/debt-limits/", {"max_sales_per_day": 1}, format="json")
        c = self.new_client()
        self.assertEqual(self.debt_sale("o1", c, "100.00").status_code, 201)
        at = (timezone.now() - timezone.timedelta(minutes=5)).isoformat()
        r = self.debt_sale("o2", c, "100.00", offline=True, offline_created_at=at)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(any(w["code"] == "debt_daily_limit_exceeded" for w in r.data.get("warnings", [])), r.data)
