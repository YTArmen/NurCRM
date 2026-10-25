import io
import zipfile
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile

from apps.arenda.models import ArendaSettings, Counter, Employee, EmploymentContract, Lease, Template
from apps.arenda.services import docx_io
from apps.arenda.services import templates as eng

from .base import ArendaBase


def make_docx(paragraphs):
    """paragraphs: список списков кусков (runs) — как Word разбивает текст."""
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = ""
    for runs in paragraphs:
        body += "<w:p>" + "".join(f'<w:r><w:t xml:space="preserve">{r}</w:t></w:r>' for r in runs) + "</w:p>"
    doc = f'<?xml version="1.0" encoding="UTF-8"?><w:document {ns}><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


class WizardMixin:
    def prep_company(self):
        self.client.patch("/api/arenda/settings/", {
            "org_name": "ОсОО Лендлорд", "director": "Иванов И.И.", "inn": "12345678901234",
            "legal_address": "г. Бишкек, ул. Ленина 1"}, format="json")
        self.client.patch(f"/api/arenda/branches/{self.branch.id}/", {"city": "Бишкек"}, format="json")

    def body(self, units, **kw):
        b = {"unit_ids": [u["id"] for u in units],
             "tenant": {"name": "ИП Асанов", "form": "ip", "inn": "11111111111111", "contact_person": "Асанов А.",
                        "passport": "ID123", "phone": "+996700111222", "activity": "Одежда"},
             "terms": {"start_date": "2026-11-01", "end_date": "2027-10-31", "pay_day": 5, "deposit_months": 2,
                       "penalty_rate": "0.100", "indexation_percent": "5", "utilities": "по счётчикам"}}
        b.update(kw)
        return b


class EngineTests(ArendaBase):
    def test_money_and_dates(self):
        self.assertEqual(eng.fmt_som(470000), "4 700 сом")
        self.assertEqual(eng.fmt_som(470050), "4 700,50 сом")
        self.assertEqual(eng.fmt_date(date(2026, 10, 25)), "25 октября 2026")
        self.assertEqual(eng.som_words(470000), "четыре тысячи семьсот сом")
        self.assertEqual(eng.number_words(21000), "двадцать одна тысяча")
        self.assertEqual(eng.number_words(2002000), "два миллиона две тысячи")
        self.assertEqual(eng.term_months(date(2026, 4, 1), date(2027, 3, 31)), 12)
        self.assertEqual(eng.term_months(date(2026, 1, 15), date(2026, 7, 14)), 6)

    def test_special_lines_and_blocks(self):
        r = eng.render_template("# Заголовок\n\n## Раздел\nТекст {{город}} {{ Дата }}\n[[подписи]]",
                                {"город": "Бишкек", "дата": "1 мая 2026", "арендодатель": "ОсОО А"}, "lease")
        types = [b["type"] for b in r["blocks"]]
        self.assertEqual(types, ["title", "section", "para", "table"])
        self.assertIn("Текст Бишкек 1 мая 2026", r["text"])
        self.assertIn('<h1', r["html"])
        self.assertEqual(r["missing"], [])


class TemplateApiTests(WizardMixin, ArendaBase):
    def setUp(self):
        super().setUp()
        self.prep_company()

    def test_defaults_seeded_and_fields(self):
        r = self.client.get("/api/arenda/templates/?kind=lease")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        r = self.client.get("/api/arenda/templates/?kind=employment")
        self.assertEqual(r.data["count"], 1)
        r = self.client.get("/api/arenda/templates/fields/?kind=employment")
        keys = {f["key"] for f in r.data["fields"]}
        self.assertTrue({"сотрудник", "должность", "оклад", "график", "дата_приёма", "телефон_сотрудника"} <= keys)
        r = self.client.get("/api/arenda/templates/fields/?kind=lease")
        keys = {f["key"] for f in r.data["fields"]}
        self.assertIn("сумма_договора", keys)
        self.assertNotIn("сотрудник", keys)
        # типовой шаблон аренды использует все поля аренды
        t = Template.objects.get(company=self.company, kind="lease")
        known, unknown = eng.used_fields(t.text, "lease")
        self.assertEqual(unknown, [])
        for f, _ in eng.LEASE_FIELDS:
            self.assertIn(f, known)

    def test_crud_copy_archive_delete(self):
        r = self.client.post("/api/arenda/templates/", {"kind": "lease", "name": "Мой", "text": "# Д {{ город }}"},
                             format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["text"], "# Д {{город}}")
        tid = r.data["id"]
        r = self.client.post(f"/api/arenda/templates/{tid}/copy/")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.data["name"].startswith("Копия"))
        r = self.client.patch(f"/api/arenda/templates/{tid}/", {"name": "Новое"}, format="json")
        self.assertEqual(r.data["name"], "Новое")
        self.assertEqual(self.client.delete(f"/api/arenda/templates/{tid}/").status_code, 204)

    def test_template_in_use_cannot_be_deleted(self):
        u = self.unit("101")
        r = self.client.post("/api/arenda/leases/sign/", self.body([u]), format="json")
        self.assertEqual(r.status_code, 201, r.data)
        tid = r.data["lease"]["template"]
        r = self.client.delete(f"/api/arenda/templates/{tid}/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "template_in_use")
        r = self.client.delete(f"/api/arenda/templates/{tid}/?archive=1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["archived"])
        # архивный не виден по умолчанию и не годится для новых договоров
        self.assertEqual(self.client.get("/api/arenda/templates/?kind=lease").data["count"], 0)
        u2 = self.unit("102")
        r = self.client.post("/api/arenda/leases/sign/", self.body([u2], template_id=tid), format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "template_archived")

    def test_import_docx_with_split_fields(self):
        data = make_docx([
            ["Договор № {", "{номер_", "договора}}"],
            ["Арендатор: {{ар", "ендатор}} ИНН {{", " инн ", "}}"],
            ["Неизвестное {{чужое}}"],
            ["[[под", "писи]]"],
        ])
        up = SimpleUploadedFile("t.docx", data)
        r = self.client.post("/api/arenda/templates/import/", {"file": up, "kind": "lease", "name": "Из Word"},
                             format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIn("{{номер_договора}}", r.data["text"])
        self.assertIn("{{арендатор}}", r.data["text"])
        self.assertIn("{{инн}}", r.data["text"])
        self.assertIn("[[подписи]]", r.data["text"])
        self.assertEqual(set(r.data["fields_found"]), {"номер_договора", "арендатор", "инн"})
        self.assertEqual(r.data["unknown_fields"], ["чужое"])
        # рендерится
        rr = eng.render_template(r.data["text"], {"номер_договора": "А-1", "арендатор": "ИП Х", "инн": "123"}, "lease")
        self.assertIn("Договор № А-1", rr["text"])
        self.assertIn("Арендатор: ИП Х ИНН 123", rr["text"])

    def test_import_txt_and_bad_file(self):
        up = SimpleUploadedFile("t.txt", "# Д\nТекст {{город}}".encode("utf-8"))
        r = self.client.post("/api/arenda/templates/import/", {"file": up, "kind": "lease"}, format="multipart")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["name"], "t")
        bad = SimpleUploadedFile("t.pdf", b"x")
        r = self.client.post("/api/arenda/templates/import/", {"file": bad}, format="multipart")
        self.assertEqual(r.status_code, 400)
        bad = SimpleUploadedFile("t.docx", b"not a zip")
        r = self.client.post("/api/arenda/templates/import/", {"file": bad}, format="multipart")
        self.assertEqual(r.status_code, 400)

    def test_section_required(self):
        u, c = self.staff()
        self.assertEqual(c.get("/api/arenda/templates/").status_code, 403)


class LeaseWizardTests(WizardMixin, ArendaBase):
    def setUp(self):
        super().setUp()
        self.prep_company()

    def test_preview_fills_fields_and_flags_missing(self):
        u = self.unit("101", area="10.00", rate=100000)
        b = self.body([u])
        b["tenant"].pop("passport")
        r = self.client.post("/api/arenda/leases/preview/", b, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("паспорт", r.data["missing"])
        self.assertIn("арендатор", r.data["filled"])
        self.assertIn("f-miss", r.data["html"])
        self.assertIn("f-ok", r.data["html"])
        # ничего не создано
        self.assertEqual(Lease.objects.count(), 0)
        self.assertEqual(Counter.objects.filter(key="lease").count(), 0)

    def test_sign_creates_lease_with_all_fields(self):
        u = self.unit("101", area="10.00", rate=100000)
        r = self.client.post("/api/arenda/leases/sign/", self.body([u]), format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["missing"], [])
        self.assertEqual(r.data["unknown"], [])
        text = r.data["text"]
        year = date.today().year
        self.assertIn(f"А-{year}/001", text)
        self.assertIn("ОсОО Лендлорд", text)
        self.assertIn("Иванов И.И.", text)
        self.assertIn("ИП Асанов", text)
        self.assertIn("1 ноября 2026", text)
        self.assertIn("31 октября 2027", text)
        self.assertIn("10 000 сом (десять тысяч сом)", text)       # 10 м² × 1000 сом
        self.assertIn("120 000 сом (сто двадцать тысяч сом)", text)  # × 12 мес.
        self.assertIn("20 000 сом (двадцать тысяч сом)", text)       # депозит 2 мес.
        self.assertIn("5% в год", text)
        self.assertIn("0,1%", text)
        self.assertNotIn("{{", text)
        self.assertNotIn("________ ", text.split("## 6")[0])
        lease = Lease.objects.get()
        self.assertEqual(lease.status, "active")
        self.assertEqual(lease.deposit_amount, 2000000)
        self.assertTrue(lease.text_snapshot)
        self.assertIsNotNone(lease.signed_at)
        self.assertEqual(lease.template.kind, "lease")
        self.assertEqual(lease.tenant.name, "ИП Асанов")
        # помещение стало «сдано» (договор начинается в будущем — сдан при старте), проверим сегодняшним договором
        u2 = self.unit("102")
        b = self.body([u2])
        b["terms"]["start_date"] = date.today().isoformat()
        b["terms"]["end_date"] = None
        self.assertEqual(self.client.post("/api/arenda/leases/sign/", b, format="json").status_code, 201)
        self.assertEqual(Lease.objects.get(units__number="102").charges.count(), 1)  # первое начисление
        chess = self.client.get("/api/arenda/chess/").data
        st = {x["number"]: x["status"] for f in chess["floors"] for x in f["units"]}
        self.assertNotEqual(st["102"], "free")  # занято (rented, либо долг по первому начислению)
        self.assertEqual(st["101"], "free")  # договор 101 стартует в будущем

    def test_existing_tenant_and_second_number(self):
        u1, u2 = self.unit("101"), self.unit("102")
        t = self.tenant("ООО Бета")
        b = {"unit_ids": [u1["id"]], "tenant_id": t["id"], "terms": {"start_date": "2026-11-01"}}
        r = self.client.post("/api/arenda/leases/sign/", b, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIn("окончание", r.data["filled"])  # «бессрочно»
        self.assertIn("срок_мес", r.data["missing"])
        b["unit_ids"] = [u2["id"]]
        r2 = self.client.post("/api/arenda/leases/sign/", b, format="json")
        self.assertTrue(r2.data["label"].endswith("/002"))

    def test_only_free_units_409(self):
        u = self.unit("101")
        self.assertEqual(self.client.post("/api/arenda/leases/sign/", self.body([u]), format="json").status_code, 201)
        r = self.client.post("/api/arenda/leases/sign/", self.body([u]), format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "lease_overlap")
        r = self.client.post("/api/arenda/leases/preview/", self.body([u]), format="json")
        self.assertEqual(r.status_code, 409)
        # помещение другого филиала
        u2 = self.unit("201", branch=self.branch2)
        r = self.client.post("/api/arenda/leases/sign/", self.body([u2], branch=str(self.branch.id)), format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "unit_not_free")
        self.assertEqual(Lease.objects.count(), 1)

    def test_sign_validation(self):
        u = self.unit("101")
        b = self.body([u])
        b["terms"].pop("start_date")
        self.assertEqual(self.client.post("/api/arenda/leases/sign/", b, format="json").status_code, 400)
        b = self.body([u])
        b["tenant"]["name"] = ""
        self.assertEqual(self.client.post("/api/arenda/leases/sign/", b, format="json").status_code, 400)
        self.assertEqual(Lease.objects.count(), 0)
        self.assertEqual(Counter.objects.filter(key="lease").first().last_value if Counter.objects.filter(key="lease").exists() else 0, 0)

    def test_snapshot_unchanged_after_template_and_requisites_change(self):
        u = self.unit("101")
        r = self.client.post("/api/arenda/leases/sign/", self.body([u]), format="json")
        lid, tid = r.data["lease"]["id"], r.data["lease"]["template"]
        before = self.client.get(f"/api/arenda/leases/{lid}/document/").data
        self.assertIn("ОсОО Лендлорд", before["text"])
        self.client.patch(f"/api/arenda/templates/{tid}/", {"text": "# Совсем другой {{арендодатель}}"}, format="json")
        self.client.patch("/api/arenda/settings/", {"org_name": "Новое название", "director": "Петров"}, format="json")
        after = self.client.get(f"/api/arenda/leases/{lid}/document/").data
        self.assertEqual(before["text"], after["text"])
        self.assertEqual(before["html"], after["html"])
        self.assertNotIn("Новое название", after["text"])

    def test_pdf_and_docx_valid(self):
        u = self.unit("101")
        lid = self.client.post("/api/arenda/leases/sign/", self.body([u]), format="json").data["lease"]["id"]
        r = self.client.get(f"/api/arenda/leases/{lid}/document.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertGreater(len(r.content), 2000)
        r2 = self.client.get(f"/api/arenda/leases/{lid}/document/?format=pdf")
        self.assertTrue(r2.content.startswith(b"%PDF"))
        r = self.client.get(f"/api/arenda/leases/{lid}/document.docx")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"PK"))
        z = zipfile.ZipFile(io.BytesIO(r.content))
        self.assertIsNone(z.testzip())
        self.assertIn("word/document.xml", z.namelist())
        # docx читается обратно нашим же парсером
        back = docx_io.parse_docx(r.content)
        self.assertIn("ДОГОВОР АРЕНДЫ", back)
        self.assertIn("ИП Асанов", back)
        r = self.client.get(f"/api/arenda/leases/{lid}/document/?format=html")
        self.assertIn("text/html", r["Content-Type"])

    def test_document_without_snapshot_409(self):
        u = self.unit("101")
        lid = self.lease(self.tenant(), [u]).data["id"]
        r = self.client.get(f"/api/arenda/leases/{lid}/document/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "no_snapshot")

    def test_base_list_filters(self):
        u1, u2 = self.unit("101"), self.unit("205", floor=2)
        self.client.post("/api/arenda/leases/sign/", self.body([u1]), format="json")
        self.lease(self.tenant("ООО Бета"), [u2], start="2024-01-01", end="2024-12-31")
        r = self.client.get("/api/arenda/contracts/?type=lease")
        self.assertEqual(r.data["count"], 2)
        r = self.client.get("/api/arenda/contracts/?type=lease&status=expired")
        self.assertEqual([x["party"] for x in r.data["results"]], ["ООО Бета"])
        r = self.client.get("/api/arenda/contracts/?type=lease&search=205")
        self.assertEqual(r.data["count"], 1)
        r = self.client.get("/api/arenda/contracts/?type=employment")
        self.assertEqual(r.data["count"], 0)
        r = self.client.get("/api/arenda/leases/?search=205")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(self.client.get("/api/arenda/contracts/?status=bogus").status_code, 400)


class EmploymentContractTests(ArendaBase):
    def setUp(self):
        super().setUp()
        self.client.patch("/api/arenda/settings/", {"director": "Иванов И.И.", "inn": "12345678901234"}, format="json")
        self.client.patch(f"/api/arenda/branches/{self.branch.id}/", {"city": "Бишкек"}, format="json")
        self.emp = Employee.objects.create(
            company=self.company, branch=self.branch, full_name="Бекова Айгуль", position="Администратор",
            phone="+996555000111", hired_at=date(2026, 9, 1), schedule="2/2, 9:00-18:00", salary=2500000)

    def test_preview_sign_and_documents(self):
        url = f"/api/arenda/employees/{self.emp.id}/contracts/"
        r = self.client.post(url + "preview/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["missing"], [])
        self.assertEqual(Counter.objects.filter(key="employment").count(), 0)
        r = self.client.post(url + "sign/", {}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        year = date.today().year
        self.assertEqual(r.data["label"], f"Т-{year}/001")
        for s in ("Бекова Айгуль", "Администратор", "25 000 сом (двадцать пять тысяч сом)", "1 сентября 2026",
                  "2/2, 9:00-18:00", "РАБОТНИК"):
            self.assertIn(s, r.data["text"])
        cid = r.data["contract"]["id"]
        r = self.client.post(url + "sign/", {}, format="json")
        self.assertEqual(r.data["label"], f"Т-{year}/002")
        r = self.client.get("/api/arenda/employment-contracts/")
        self.assertEqual(r.data["count"], 2)
        r = self.client.get(f"/api/arenda/employment-contracts/{cid}/document/")
        self.assertIn("ТРУДОВОЙ ДОГОВОР", r.data["text"])
        r = self.client.get(f"/api/arenda/employment-contracts/{cid}/document.pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        r = self.client.get(f"/api/arenda/employment-contracts/{cid}/document.docx")
        self.assertTrue(r.content.startswith(b"PK"))
        self.assertEqual(self.client.get("/api/arenda/contracts/?type=employment").data["count"], 2)
        # шаблон теперь используется
        tid = EmploymentContract.objects.first().template_id
        r = self.client.delete(f"/api/arenda/templates/{tid}/")
        self.assertEqual(r.status_code, 409)

    def test_overrides_and_missing(self):
        self.emp.schedule = ""
        self.emp.save()
        url = f"/api/arenda/employees/{self.emp.id}/contracts/preview/"
        r = self.client.post(url, {}, format="json")
        self.assertIn("график", r.data["missing"])
        r = self.client.post(url, {"overrides": {"график": "5/2"}}, format="json")
        self.assertNotIn("график", r.data["missing"])
        self.assertIn("5/2", r.data["text"])
