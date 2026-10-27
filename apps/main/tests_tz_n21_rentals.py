"""ТЗ Н-21 (23.10.2026): прокат — продление, новые поля, фильтры, единый формат списка."""
from datetime import timedelta
from decimal import Decimal
import uuid

from django.utils import timezone

from apps.construction.models import CashFlow
from apps.main.models import Product, ProductVariant, Rental
from apps.main.tests_kassa_api import KassaBase


class RentalN21Tests(KassaBase):
    def setUp(self):
        super().setUp()
        dress = Product.objects.create(company=self.company, name="Платье", price=Decimal("3000"), quantity=0)
        self.m = ProductVariant.objects.create(company=self.company, product=dress, size="M", quantity=Decimal("5"))
        dress.quantity = Decimal("5")
        dress.save()

    def _rent(self, **extra):
        today = timezone.localdate()
        body = {"client": str(self.client_obj.id), "items": [{"variant": str(self.m.id)}],
                "date_from": today.isoformat(), "date_to": (today + timedelta(days=2)).isoformat(),
                "tariff": "сутки", "deposit_type": "money", "deposit_amount": "500.00"}
        body.update(extra)
        r = self.api.post("/api/rentals/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def _extend(self, rent, days=2, key=None, **extra):
        new = (timezone.localdate() + timedelta(days=2 + days)).isoformat()
        body = {"new_date_to": new, "extra_amount": "500.00", "payment_method": "cash", **extra}
        return self.api.post(f"/api/rentals/{rent['id']}/extend/", body, format="json",
                             HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))

    def test_new_fields_and_phone_mask(self):
        rent = self._rent(late_penalty_per_day="50")
        self.assertEqual(rent["client_phone"], "+996555000222")
        self.assertEqual(rent["late_penalty_per_day"], "50.00")
        self.assertEqual(rent["shift"], str(self.shift.id))
        self.assertEqual(rent["cashbox"], str(self.cashbox.id))
        self.assertEqual(rent["created_by"], str(self.owner.id))
        self.assertEqual(rent["extensions"], [])
        self.assertEqual(rent["penalty_due"], "0.00")
        # сотрудник без права на клиентов видит маску
        from apps.main.rentals import _mask_phone
        self.assertEqual(_mask_phone("+996555000222"), "********0222")
        self.cashier.can_view_clients = False
        self.cashier.save()
        self.api.force_authenticate(self.cashier)
        row = self.api.get("/api/rentals/", HTTP_X_NURMARKET_CLIENT="kassa/1.17.61").data["results"][0]
        self.assertEqual(row["client_phone"], "********0222")

    def test_extend_cash_goes_to_shift(self):
        rent = self._rent()
        before = self.shift.calc_live_totals(refresh=True)["expected_cash"]
        r = self._extend(rent, shift=str(self.shift.id))
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data["extensions"]), 1)
        self.assertEqual(r.data["extensions"][0]["amount"], "500.00")
        self.assertEqual(r.data["paid"], "500.00")
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], before + Decimal("500.00"))
        self.assertEqual(Rental.objects.get(pk=rent["id"]).date_to.isoformat(), r.data["date_to"])

    def test_extend_idempotent(self):
        rent = self._rent()
        self.assertEqual(self._extend(rent, key="k1").status_code, 200)
        r2 = self._extend(rent, key="k1")
        self.assertEqual(r2.status_code, 200, r2.data)
        self.assertTrue(r2.data["replayed"])
        self.assertEqual(len(r2.data["extensions"]), 1)
        self.assertEqual(CashFlow.objects.filter(source_business_operation_id="rental_extend").count(), 1)

    def test_extend_errors(self):
        rent = self._rent()
        r = self.api.post(f"/api/rentals/{rent['id']}/extend/",
                          {"new_date_to": rent["date_to"], "extra_amount": "0"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "date_not_later")
        self.shift.status = "closed"
        self.shift.save()
        r = self._extend(rent, shift=str(self.shift.id))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "shift_closed")
        self.shift.status = "open"
        self.shift.save()
        self.api.post(f"/api/rentals/{rent['id']}/return/", {"condition": "ok"}, format="json")
        r = self._extend(rent)
        self.assertEqual(r.data["code"], "rental_returned")

    def test_return_with_penalty_fields(self):
        rent = self._rent()
        r = self.api.post(f"/api/rentals/{rent['id']}/return/", {"condition": "damaged", "penalty": "200"},
                          format="json")
        self.assertEqual((r.data["deposit_refunded"], r.data["deposit_withheld"]), ("300.00", "200.00"))
        d = self.api.get(f"/api/rentals/{rent['id']}/").data
        self.assertEqual((d["deposit_refunded"], d["deposit_withheld"]), ("300.00", "200.00"))

    def test_overdue_penalty_due(self):
        past = timezone.localdate() - timedelta(days=10)
        rent = self._rent(date_from=past.isoformat(), date_to=(past + timedelta(days=2)).isoformat(),
                          late_penalty_per_day="100")
        # просрочка 8 дней * 100 = 800, залог 500 -> 300 сверх залога
        self.assertEqual(rent["penalty_due"], "300.00")

    def test_list_format_and_filters(self):
        today = timezone.localdate()
        self._rent()
        past = today - timedelta(days=5)
        self._rent(date_from=past.isoformat(), date_to=(past + timedelta(days=1)).isoformat())
        # Б-02/Б-21: формат {count…} — новому клиенту; старая касса (без заголовка) получает массив
        old = self.api.get("/api/rentals/")
        self.assertIsInstance(old.data, list)
        self.assertEqual(len(old.data), 2)
        self.api.credentials(HTTP_X_NURMARKET_CLIENT="kassa/1.17.61")
        for q in ("", "?status=overdue", "?status=active&limit=20"):
            data = self.api.get("/api/rentals/" + q).data
            self.assertEqual(set(data), {"count", "next", "previous", "results"})
        self.assertEqual(self.api.get("/api/rentals/?status=overdue").data["count"], 1)
        self.assertEqual(self.api.get(f"/api/rentals/?date_from={today.isoformat()}").data["count"], 1)
        self.assertEqual(self.api.get(f"/api/rentals/?date_to={past.isoformat()}").data["count"], 1)
        self.assertEqual(self.api.get("/api/rentals/?search=Айбек").data["count"], 2)
        self.assertEqual(self.api.get("/api/rentals/?search=нетакого").data["count"], 0)
        self.assertEqual(self.api.get(f"/api/rentals/?client={self.client_obj.id}").data["count"], 2)
        res = self.api.get("/api/rentals/?ordering=number").data["results"]
        self.assertEqual([x["number"] for x in res], [1, 2])
        res = self.api.get("/api/rentals/?ordering=-number&limit=1").data
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["number"], 2)
