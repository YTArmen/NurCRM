"""ТЗ Н-19: статьи расходов (slug/is_system), разбивка по статьям, инкассация."""
import uuid
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.construction.categories import ensure_system_categories
from apps.construction.models import Cashbox, CashFlow, CashFlowCategory, CashShift
from apps.users.models import Company, User

D = Decimal


class CategoriesSlugTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email=f"o{uuid.uuid4().hex[:6]}@t.kg", password="x", role="owner")
        self.company = Company.objects.create(name="Co", owner=self.owner)
        self.owner.company = self.company
        self.owner.save()
        self.cb = Cashbox.objects.create(company=self.company, name="Касса", role=Cashbox.CashboxRole.POS_MAIN)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def _cat(self, title, **kw):
        return CashFlowCategory.objects.create(company=self.company, title=title, type="expense", **kw)

    def _flow(self, cat, amount, **kw):
        return CashFlow.objects.create(company=self.company, cashbox=self.cb, type=CashFlow.Type.EXPENSE,
                                       amount=D(amount), status=CashFlow.Status.APPROVED, category=cat,
                                       payment_method="cash", **kw)

    def _get(self, url):
        d = timezone.localdate().isoformat()
        r = self.api.get(url, {"date_from": d, "date_to": d})
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        return r.data

    def test_slug_mapping_idempotent_and_case_insensitive(self):
        self._cat("налог")
        self._cat("АРЕНДА")
        self._cat("Транспорт")
        ensure_system_categories(self.company)
        ensure_system_categories(self.company)
        by = {c.title.lower(): c for c in CashFlowCategory.objects.filter(company=self.company)}
        self.assertEqual(by["налог"].slug, "taxes")
        self.assertEqual(by["аренда"].slug, "rent")
        self.assertTrue(by["аренда"].is_system)
        self.assertEqual(by["транспорт"].slug, "")
        slugs = sorted(CashFlowCategory.objects.filter(company=self.company).exclude(slug="").values_list("slug", flat=True))
        self.assertEqual(slugs, sorted(["salaries", "rent", "taxes", "utilities", "purchases"]))  # без дублей

    def test_list_returns_slug_and_system_cannot_be_deleted(self):
        r = self.api.get("/api/construction/cashflow-categories/")
        self.assertEqual(r.status_code, 200)
        rows = {x["slug"]: x for x in r.data if x["slug"]}
        self.assertEqual(set(rows), {"salaries", "rent", "taxes", "utilities", "purchases"})
        self.assertTrue(all(x["is_system"] for x in rows.values()))
        rd = self.api.delete(f"/api/construction/cashflow-categories/{rows['rent']['id']}/")
        self.assertEqual(rd.status_code, 400)
        self.assertEqual(rd.data["code"], "category_is_system")
        custom = self.api.post("/api/construction/cashflow-categories/", {"title": "Реклама 2", "type": "expense"}, format="json")
        self.assertEqual(custom.status_code, 201, custom.data)
        self.assertEqual(custom.data["slug"], "")
        self.assertEqual(self.api.delete(f"/api/construction/cashflow-categories/{custom.data['id']}/").status_code, 204)

    def test_create_by_system_title_gets_slug(self):
        r = self.api.post("/api/construction/cashflow-categories/", {"title": "коммунальные", "type": "expense"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["slug"], "utilities")
        self.assertTrue(r.data["is_system"])

    def test_cashflow_and_pnl_by_slug(self):
        rent = self._cat("Помещение", slug="rent", is_system=True)
        tr = self._cat("Транспорт")
        self._flow(rent, "1.00", name="Платёж")
        self._flow(tr, "2.00", name="Такси")
        cf = self._get("/api/main/analytics/cashflow/")
        self.assertEqual(cf["outflow"]["rent"], "1.00")
        self.assertEqual(cf["outflow"]["other_expenses"], "2.00")
        by = {x["title"]: x for x in cf["outflow"]["by_category"]}
        self.assertEqual(by["Помещение"]["slug"], "rent")
        self.assertEqual(by["Транспорт"]["amount"], "2.00")
        pnl = self._get("/api/main/analytics/pnl/")
        self.assertEqual(pnl["opex"]["rent"], "1.00")
        self.assertEqual(pnl["opex"]["other"], "2.00")
        self.assertEqual(pnl["opex"]["total"], "3.00")
        self.assertEqual({x["title"] for x in pnl["opex"]["by_category"]}, {"Помещение", "Транспорт"})

    def test_no_slug_falls_back_to_title(self):
        c = self._cat("помещение аренда")  # sqlite: icontains без регистра только для ASCII
        self._flow(c, "5.00", name="Офис")
        cf = self._get("/api/main/analytics/cashflow/")
        self.assertEqual(cf["outflow"]["rent"], "5.00")

    def test_post_cashflow_category_shift_source_kind_and_filters(self):
        shift = CashShift.objects.create(company=self.company, cashbox=self.cb, cashier=self.owner,
                                         status=CashShift.Status.OPEN, opening_cash=D("100.00"))
        cat = self._cat("Зарплата", slug="salaries", is_system=True)
        for kind in ("shift_drawer_outflow", "salary_payout", "rent_payment", "supplier_payment", "shift_drawer_collection"):
            r = self.api.post("/api/construction/cashflows/", {
                "cashbox": str(self.cb.id), "type": "expense", "name": kind, "amount": "1.00",
                "category": str(cat.id), "shift": str(shift.id), "source_kind": kind, "source_id": "x" * 120 + kind[:3],
            }, format="json")
            self.assertEqual(r.status_code, 201, (kind, r.data))
            self.assertEqual(r.data["source_kind"], kind)
            self.assertEqual(r.data["category_title"], "Зарплата")
        bad = self.api.post("/api/construction/cashflows/", {
            "cashbox": str(self.cb.id), "type": "expense", "name": "z", "amount": "1.00", "source_kind": "zzzz"}, format="json")
        self.assertEqual(bad.status_code, 400)
        r = self.api.get("/api/construction/cashflows/", {"source_kind": "salary_payout", "shift": str(shift.id),
                                                          "category": str(cat.id), "type": "expense", "employee": str(self.owner.id)})
        rows = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_kind"], "salary_payout")

    def test_collection_in_expected_cash_not_in_profit(self):
        shift = CashShift.objects.create(company=self.company, cashbox=self.cb, cashier=self.owner,
                                         status=CashShift.Status.OPEN, opening_cash=D("100.00"))
        r = self.api.post("/api/construction/cashflows/", {
            "cashbox": str(self.cb.id), "type": "expense", "name": "Инкассация", "amount": "20.00",
            "shift": str(shift.id), "source_kind": "shift_drawer_collection", "payment_method": "cash"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        flow = CashFlow.objects.get(pk=r.data["id"])
        self.assertTrue(flow.affects_shift_drawer)
        self.assertEqual(shift.calc_live_totals(refresh=True)["expected_cash"], D("80.00"))
        pnl = self._get("/api/main/analytics/pnl/")
        self.assertEqual(pnl["opex"]["total"], "0.00")
        self.assertEqual(pnl["net_profit"], "0.00")
        cf = self._get("/api/main/analytics/cashflow/")
        self.assertEqual(cf["outflow"]["collections"], "20.00")
        self.assertEqual(cf["outflow"]["other_expenses"], "0.00")
        self.assertEqual(cf["outflow"]["total"], "20.00")
