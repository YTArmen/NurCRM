"""ТЗ Н-22 (23.10.2026): клиентские документы — КП, счёт, накладная."""
import uuid

from apps.construction.models import CashFlow
from apps.main.models import ClientDocument
from apps.main.tests_kassa_api import KassaBase

URL = "/api/main/client-documents/"


class ClientDocumentTests(KassaBase):
    def _create(self, type="invoice", **extra):
        body = {"type": type, "client": str(self.client_obj.id),
                "lines": [{"name": "Услуга", "qty": "2", "price": "100", "discount": "0"}], **extra}
        r = self.api.post(URL, body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_numbers_unique_per_type(self):
        a, b = self._create(), self._create()
        q = self._create("quote")
        self.assertEqual((a["number"], b["number"], q["number"]), (1, 2, 1))
        self.assertEqual(a["total"], "200.00")
        self.assertEqual(a["status"], "draft")

    def test_filters_pagination_and_soft_delete(self):
        a = self._create()
        self._create("quote")
        self._create("waybill", client=None)
        r = self.api.get(f"{URL}?client={self.client_obj.id}&type=invoice").data
        self.assertEqual((r["count"], len(r["results"])), (1, 1))
        self.assertEqual(self.api.get(f"{URL}?limit=1").data["count"], 3)
        self.assertEqual(self.api.get(f"{URL}?search=Айбек").data["count"], 2)
        self.assertEqual(self.api.get(f"{URL}?status=sent").data["count"], 0)
        self.assertEqual(self.api.delete(f"{URL}{a['id']}/").status_code, 204)
        self.assertEqual(self.api.get(f"{URL}").data["count"], 2)
        self.assertEqual(self.api.get(f"{URL}?include_deleted=1").data["count"], 3)
        self.assertEqual(self.api.get(f"{URL}{a['id']}/").status_code, 404)
        self.assertTrue(ClientDocument.objects.get(pk=a["id"]).is_deleted)

    def test_patch(self):
        a = self._create()
        r = self.api.patch(f"{URL}{a['id']}/", {"status": "sent", "discount": "50",
                           "lines": [{"name": "X", "qty": "1", "price": "300"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data["status"], r.data["total"], r.data["number"]), ("sent", "250.00", 1))
        self.assertEqual(self.api.patch(f"{URL}{a['id']}/", {"status": "paid"}, format="json").status_code, 400)
        self.assertEqual(self.api.patch(f"{URL}{a['id']}/", {"paid_amount": "5"}, format="json").status_code, 400)

    def test_company_scoping(self):
        a = self._create()
        from django.contrib.auth import get_user_model
        from apps.users.models import Company
        o2 = get_user_model().objects.create_user(email="o2@t.kg", password="x", role="owner")
        c2 = Company.objects.create(name="C2", owner=o2, is_active=True, subscription_plan=self.plan)
        o2.company = c2
        o2.save()
        self.api.force_authenticate(o2)
        self.assertEqual(self.api.get(URL).data["count"], 0)
        self.assertEqual(self.api.get(f"{URL}{a['id']}/").status_code, 404)

    def test_pay(self):
        a = self._create()
        before = self.shift.calc_live_totals(refresh=True)["expected_cash"]
        key = str(uuid.uuid4())
        r = self.api.post(f"{URL}{a['id']}/pay/", {"amount": "50", "shift": str(self.shift.id)},
                          format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["paid_amount"], "50.00")
        self.assertEqual(r.data["status"], "draft")
        self.assertEqual(self.shift.calc_live_totals(refresh=True)["expected_cash"], before + 50)
        r2 = self.api.post(f"{URL}{a['id']}/pay/", {"amount": "50", "shift": str(self.shift.id)},
                           format="json", HTTP_IDEMPOTENCY_KEY=key)
        self.assertTrue(r2.data["replayed"])
        self.assertEqual(r2.data["paid_amount"], "50.00")
        self.assertEqual(CashFlow.objects.filter(source_business_operation_id="client_document_pay").count(), 1)
        r3 = self.api.post(f"{URL}{a['id']}/pay/", {"amount": "150"}, format="json",
                           HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual((r3.data["status"], r3.data["paid_amount"]), ("paid", "200.00"))
        r4 = self.api.post(f"{URL}{a['id']}/pay/", {"amount": "1"}, format="json")
        self.assertEqual(r4.data["code"], "overpayment")

    def test_pay_rules(self):
        q = self._create("quote")
        self.assertEqual(self.api.post(f"{URL}{q['id']}/pay/", {"amount": "1"}, format="json").data["code"], "not_invoice")
        a = self._create()
        self.shift.status = "closed"
        self.shift.save()
        r = self.api.post(f"{URL}{a['id']}/pay/", {"amount": "1", "shift": str(self.shift.id)}, format="json")
        self.assertEqual(r.data["code"], "shift_closed")
