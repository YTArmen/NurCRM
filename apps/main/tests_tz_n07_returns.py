"""ТЗ Н-07: возвраты — страницы по page/page_size, refunded_total/refundable_total, quantity_refunded."""
import uuid
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.main.models import SaleReturn
from apps.main.pos_views import SaleReturnAPIView
from apps.main import tests_pos_sale_return_cashflow as _base_mod

_Base = _base_mod.POSSaleReturnCashflowTests


class TzN07ReturnsTests(TestCase):
    # setUp и фабрика чека — из теста возвратов кассы
    setUp = _Base.setUp
    _create_paid_sale = _Base._create_paid_sale

    def _return(self, sale, qty=None):
        body = {"idempotency_key": str(uuid.uuid4())}
        if qty is not None:
            body["items"] = [{"sale_item_id": str(sale.items.first().id), "quantity": qty}]
        req = self.factory.post(f"/main/pos/sales/{sale.id}/return/", body, format="json")
        force_authenticate(req, user=self.cashier)
        res = SaleReturnAPIView.as_view()(req, pk=sale.id)
        self.assertEqual(res.status_code, 200)

    def _api(self):
        api = APIClient()
        api.force_authenticate(self.cashier)
        return api

    def test_list_is_bare_array_without_page_params(self):
        sale = self._create_paid_sale(quantity=2, price=Decimal("500.00"))
        self._return(sale, 1)
        res = self._api().get("/api/main/pos/returns/")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)
        self.assertEqual(len(res.json()), 1)

    def test_list_paginated_when_page_size_passed(self):
        for _ in range(3):
            sale = self._create_paid_sale(quantity=1, price=Decimal("100.00"))
            self._return(sale)
        res = self._api().get("/api/main/pos/returns/?page_size=2")
        data = res.json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 2)
        self.assertIsNotNone(data["next"])
        self.assertIsNone(data["previous"])
        res2 = self._api().get("/api/main/pos/returns/?page_size=2&page=2")
        self.assertEqual(len(res2.json()["results"]), 1)
        self.assertIsNone(res2.json()["next"])
        self.assertIsNotNone(res2.json()["previous"])
        # фильтр shift работает вместе со страницами
        res3 = self._api().get(f"/api/main/pos/returns/?page=1&shift={self.shift.id}")
        self.assertEqual(res3.json()["count"], 3)

    def test_sale_detail_refunded_and_refundable_after_partial_return(self):
        sale = self._create_paid_sale(quantity=2, price=Decimal("500.00"))
        self._return(sale, 1)
        d = self._api().get(f"/api/main/pos/sales/{sale.id}/").json()
        self.assertEqual(d["refunded_total"], "500.00")
        self.assertEqual(d["refundable_total"], "500.00")
        self.assertEqual(d["items"][0]["quantity_refunded"], "1.000")
        self.assertEqual(Decimal(str(d["items"][0]["returnable_qty"])), Decimal("1"))
        lst = self._api().get("/api/main/pos/sales/").json()
        rows = lst["results"] if isinstance(lst, dict) else lst
        row = next(r for r in rows if r["id"] == str(sale.id))
        self.assertEqual(row["refunded_total"], "500.00")
        self.assertEqual(row["refundable_total"], "500.00")

    def test_full_return_refundable_zero(self):
        sale = self._create_paid_sale(quantity=2, price=Decimal("500.00"))
        self._return(sale)
        d = self._api().get(f"/api/main/pos/sales/{sale.id}/").json()
        self.assertEqual(d["refunded_total"], "1000.00")
        self.assertEqual(d["refundable_total"], "0.00")

    def test_unreturned_sale_has_zero_refunded(self):
        sale = self._create_paid_sale(quantity=2, price=Decimal("500.00"))
        d = self._api().get(f"/api/main/pos/sales/{sale.id}/").json()
        self.assertEqual(d["refunded_total"], "0.00")
        self.assertEqual(d["refundable_total"], "1000.00")
        self.assertEqual(d["items"][0]["quantity_refunded"], "0.000")

    def test_backfill_command_dry_run_then_apply(self):
        sale = self._create_paid_sale(quantity=2, price=Decimal("500.00"))
        self._return(sale, 1)
        ret = SaleReturn.objects.get()
        SaleReturn.objects.filter(pk=ret.pk).update(shift=None)
        call_command("backfill_return_shifts")
        ret.refresh_from_db()
        self.assertIsNone(ret.shift_id)  # по умолчанию ничего не пишем
        call_command("backfill_return_shifts", "--apply")
        ret.refresh_from_db()
        self.assertEqual(ret.shift_id, self.shift.id)


del _Base, _base_mod  # чтобы загрузчик тестов не собрал базовый класс второй раз
