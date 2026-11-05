"""Второй типовой договор — аренда жилого помещения (05.11.2026): шаблон, таблицы, акт, счётчики."""
import io
import zipfile

from apps.arenda.models import ArendaSettings, Lease, Template
from apps.arenda.services import docx_io
from apps.arenda.services import templates as eng
from apps.arenda.services.doc_defaults import DEFAULT_LEASE_FLAT_NAME

from .base import ArendaBase
from .test_docs import WizardMixin


class FlatEngineTests(ArendaBase):
    def test_grid_break_meters_blocks(self):
        text = "|! № | Имя |\n| 1 | |\n| 2 | x |\n[[новая-страница]]\n[[счётчики]]"
        blocks = eng.to_blocks(text)
        self.assertEqual(blocks[0]["type"], "grid")
        self.assertEqual(blocks[0]["rows"], [["№", "Имя"], ["1", ""], ["2", "x"]])
        self.assertEqual(blocks[0]["header"], [True, False, False])
        self.assertEqual(blocks[1]["type"], "break")
        meters = [b["text"] for b in blocks[2:]]
        self.assertEqual(len(meters), 5)
        self.assertTrue(meters[0].startswith("Электроэнергия: ___"))
        filled = [b["text"] for b in eng.to_blocks("[[счётчики]]", {"electricity": "12345", "keys": "2"})]
        self.assertEqual(filled[0], "Электроэнергия: 12345")
        self.assertIn("в количестве: 2 комплект", filled[-1])
        html = eng.blocks_to_html(blocks)
        self.assertIn('class="grid"', html)
        self.assertIn("page-break", html)

    def test_docx_and_pdf_build(self):
        r = eng.render_template(eng.DEFAULT_LEASE_FLAT_TEXT, {"номер_договора": "А-2026/001"}, "lease")
        data = docx_io.build_docx(eng.to_blocks(r["text"]))
        xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
        self.assertIn('w:type="page"', xml)
        self.assertIn("АКТ ПРИЁМА-ПЕРЕДАЧИ", xml)
        self.assertIn("tblBorders", xml)
        self.assertTrue(docx_io.build_pdf(eng.to_blocks(r["text"])).startswith(b"%PDF"))


class FlatWizardTests(WizardMixin, ArendaBase):
    def setUp(self):
        super().setUp()
        self.prep_company()

    def flat_id(self):
        r = self.client.get("/api/arenda/templates/?kind=lease")
        names = {t["name"]: t["id"] for t in r.data["results"]}
        self.assertIn(DEFAULT_LEASE_FLAT_NAME, names)
        return names[DEFAULT_LEASE_FLAT_NAME]

    def test_second_default_template_given_once(self):
        tid = self.flat_id()
        self.assertEqual(self.client.delete(f"/api/arenda/templates/{tid}/").status_code, 204)
        r = self.client.get("/api/arenda/templates/?kind=lease")
        self.assertNotIn(DEFAULT_LEASE_FLAT_NAME, [t["name"] for t in r.data["results"]])
        self.assertTrue(ArendaSettings.objects.get(company=self.company).flat_template_added)

    def test_existing_company_gets_flat_template(self):
        Template.objects.create(company=self.company, kind="lease", name="Свой", text="x")
        ArendaSettings.objects.filter(company=self.company).update(flat_template_added=False)
        self.flat_id()

    def test_sign_flat_and_fill_meters(self):
        u = self.unit("12")
        r = self.client.post("/api/arenda/leases/sign/", self.body([u], template_id=self.flat_id()), format="json")
        self.assertEqual(r.status_code, 201, r.data)
        lid = r.data["lease"]["id"]
        self.assertIn("[[счётчики]]", Lease.objects.get(pk=lid).text_snapshot)
        d = self.client.get(f"/api/arenda/leases/{lid}/document/")
        self.assertEqual(d.status_code, 200)
        self.assertTrue(d.data["has_meters"])
        self.assertEqual(d.data["handover"]["electricity"], "")
        self.assertIn("Электроэнергия: ___", d.data["html"])
        r = self.client.patch(f"/api/arenda/leases/{lid}/handover/",
                              {"electricity": " 1520 кВт·ч ", "keys": 2, "bogus": "x"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["handover"]["electricity"], "1520 кВт·ч")
        self.assertEqual(r.data["handover"]["keys"], "2")
        self.assertNotIn("bogus", r.data["handover"])
        d = self.client.get(f"/api/arenda/leases/{lid}/document/")
        self.assertIn("Электроэнергия: 1520 кВт·ч", d.data["html"])
        f = self.client.get(f"/api/arenda/leases/{lid}/document/?format=docx")
        xml = zipfile.ZipFile(io.BytesIO(f.content)).read("word/document.xml").decode()
        self.assertIn("1520", xml)
        # слишком длинное значение и не текст
        r = self.client.patch(f"/api/arenda/leases/{lid}/handover/", {"gas": "1" * 65, "water_hot": {"a": 1}},
                              format="json")
        self.assertEqual(r.status_code, 400)
