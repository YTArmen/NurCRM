"""Пункт 12: мелочи (тыйыны в текстах, срок налогов, исходный файл шаблона, договор через API со снимком, фильтры расходов)."""
import shutil
import tempfile
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.arenda.models import Employee, ExpenseCategory, Lease, Template, TaxPayment
from apps.arenda.services import expenses as ex
from apps.arenda.services.reminders import fmt_money

from .base import ArendaBase

A = "/api/arenda/"


class FmtMoneyTests(ArendaBase):
    def test_keeps_tyiyn(self):
        self.assertEqual(fmt_money(470_000), "4 700 с")
        self.assertEqual(fmt_money(470_050), "4 700,50 с")
        self.assertEqual(fmt_money(470_005), "4 700,05 с")
        self.assertEqual(fmt_money(99), "0,99 с")
        self.assertEqual(fmt_money(0), "0 с")
        self.assertEqual(fmt_money(-12_550), "-125,50 с")


class TaxDueDayTests(ArendaBase):
    def test_setting_default_and_used_for_calendar(self):
        r = self.client.get(A + "settings/")
        self.assertEqual(r.data["tax_due_day"], 25)
        Employee.objects.create(company=self.company, branch=self.branch, full_name="Сотр", salary=3_000_000,
                                hired_at=date(2026, 1, 1))
        ex.sync_payroll_taxes(self.company, date(2026, 3, 1))
        self.assertEqual({t.due_date for t in TaxPayment.objects.all()}, {date(2026, 4, 25)})
        TaxPayment.objects.all().delete()
        r = self.client.patch(A + "settings/", {"tax_due_day": 20}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["tax_due_day"], 20)
        self.company.arenda_settings.refresh_from_db()
        ex.sync_payroll_taxes(self.company, date(2026, 3, 1))
        self.assertEqual({t.due_date for t in TaxPayment.objects.all()}, {date(2026, 4, 20)})
        self.assertEqual(self.client.patch(A + "settings/", {"tax_due_day": 40}, format="json").status_code, 400)


class TemplateSourceFileTests(ArendaBase):
    def test_import_saves_source_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with override_settings(MEDIA_ROOT=tmp):
            up = SimpleUploadedFile("dogovor.txt", "# Договор\nТекст {{город}}".encode("utf-8"))
            r = self.client.post(A + "templates/import/", {"file": up, "kind": "lease"}, format="multipart")
            self.assertEqual(r.status_code, 201, r.data)
            t = Template.objects.get(pk=r.data["id"])
            self.assertTrue(t.source_file)
            self.assertTrue(t.source_file.name.startswith("arenda/templates/"))
            self.assertIn("Текст", t.source_file.read().decode("utf-8"))


class LeaseCreateSnapshotTests(ArendaBase):
    def setUp(self):
        super().setUp()
        self.u = self.unit("101", area="10.00", rate=100000)
        self.t = self.tenant()

    def body(self, **kw):
        return {"tenant": self.t["id"], "units": [self.u["id"]], "start_date": "2026-11-01", "pay_day": 10, **kw}

    def test_no_templates_empty_snapshot_no_error(self):
        r = self.client.post(A + "leases/", self.body(), format="json")
        self.assertEqual(r.status_code, 201, r.data)
        l = Lease.objects.get()
        self.assertEqual(l.text_snapshot, "")
        self.assertIsNone(l.template)

    def test_default_template_used_first_non_archived(self):
        Template.objects.create(company=self.company, kind="lease", name="Старый", text="# А\nстарый",
                                archived_at=__import__("django").utils.timezone.now())
        first = Template.objects.create(company=self.company, kind="lease", name="Основной",
                                        text="# Договор № {{номер_договора}}\nАрендатор {{арендатор}}, помещение {{помещение}}")
        Template.objects.create(company=self.company, kind="lease", name="Второй", text="# Второй")
        r = self.client.post(A + "leases/", self.body(), format="json")
        self.assertEqual(r.status_code, 201, r.data)
        l = Lease.objects.get()
        self.assertEqual(l.template_id, first.id)
        self.assertIn("Арендатор ИП Асанов, помещение 101", l.text_snapshot)
        self.assertIn(l.label, l.text_snapshot)
        # документ договора открывается
        self.assertEqual(self.client.get(f"{A}leases/{l.id}/document.pdf").status_code, 200)

    def test_template_id_and_archived_template(self):
        t2 = Template.objects.create(company=self.company, kind="lease", name="Второй", text="# Второй {{арендатор}}")
        r = self.client.post(A + "leases/", self.body(template_id=str(t2.id)), format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Lease.objects.get().template_id, t2.id)
        self.assertIn("Второй ИП Асанов", Lease.objects.get().text_snapshot)
        t2.archived_at = __import__("django").utils.timezone.now()
        t2.save()
        u2 = self.unit("102")
        r = self.client.post(A + "leases/", self.body(units=[u2["id"]], template_id=str(t2.id)), format="json")
        self.assertEqual(r.status_code, 409)


class ExpenseFilterTests(ArendaBase):
    def test_date_and_branch_filters(self):
        cat = ExpenseCategory.objects.create(company=self.company, name="Прочее")

        def add(day, branch, amount):
            r = self.client.post(A + "expenses/", {"branch": str(branch.id), "category": str(cat.id), "date": day,
                                                   "amount": amount, "method": "cash"}, format="json")
            self.assertEqual(r.status_code, 201, r.data)

        add("2026-10-05", self.branch, 100)
        add("2026-10-15", self.branch, 200)
        add("2026-10-25", self.branch2, 300)
        amounts = lambda q: sorted(x["amount"] for x in self.client.get(A + "expenses/" + q).data["results"])  # noqa: E731
        self.assertEqual(amounts("?date_from=2026-10-10"), [200, 300])
        self.assertEqual(amounts("?date_to=2026-10-15"), [100, 200])
        self.assertEqual(amounts("?date_from=2026-10-10&date_to=2026-10-20"), [200])
        self.assertEqual(amounts(f"?branch={self.branch2.id}"), [300])
        self.assertEqual(amounts(f"?branch={self.branch.id}&date_from=2026-10-10"), [200])
        self.assertEqual(self.client.get(A + "expenses/?date_from=bad").status_code, 400)
