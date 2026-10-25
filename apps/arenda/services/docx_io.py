"""DOCX (вручную, без python-docx) и PDF (reportlab) из структурного вида договора; разбор загруженного .docx."""
import io
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xesc

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
MAX_XML = 15 * 1024 * 1024

# ---------------------------------------------------------------- сборка DOCX


def _run(text, bold=False, size=24):
    rpr = f'<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman"/>' \
          f'{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr>'
    return f'<w:r>{rpr}<w:t xml:space="preserve">{xesc(text)}</w:t></w:r>'


def _para(text, *, bold=False, align="both", size=24, before=0, after=80):
    return (f'<w:p><w:pPr><w:spacing w:before="{before}" w:after="{after}"/><w:jc w:val="{align}"/></w:pPr>'
            f'{_run(text, bold, size)}</w:p>')


def _cell(text, width):
    paras = "".join(
        f'<w:p><w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr>{_run(line)}</w:p>' for line in (text.split("\n") or [""]))
    return f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>{paras}</w:tc>'


def build_docx(blocks):
    body = []
    for b in blocks:
        t = b["type"]
        if t == "title":
            body.append(_para(b["text"], bold=True, align="center", size=28, after=200))
        elif t == "section":
            body.append(_para(b["text"], bold=True, align="left", before=160, after=60))
        elif t == "para":
            body.append(_para(b["text"]))
        else:
            rows = "".join(f"<w:tr>{_cell(l, 4500)}{_cell(r, 4500)}</w:tr>" for l, r in b["rows"])
            body.append('<w:tbl><w:tblPr><w:tblW w:w="9000" w:type="dxa"/></w:tblPr>'
                        '<w:tblGrid><w:gridCol w:w="4500"/><w:gridCol w:w="4500"/></w:tblGrid>'
                        f"{rows}</w:tbl>")
    sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" w:left="1418" w:header="708" w:footer="708" w:gutter="0"/>'
            "</w:sectPr>")
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{W}"><w:body>{"".join(body)}<w:p/>{sect}</w:body></w:document>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" '
                     'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


# ---------------------------------------------------------------- разбор DOCX


class DocxError(ValueError):
    pass


def _is_on(el):
    return el is not None and el.get(f"{{{W}}}val", "true") not in ("0", "false", "off")


def parse_docx(data: bytes) -> str:
    """Текст из .docx: абзац = строка, куски Word (runs) склеиваются, поэтому {{поле}} не рвётся."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        info = z.getinfo("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise DocxError("Файл не похож на .docx.")
    if info.file_size > MAX_XML:
        raise DocxError("Файл слишком большой.")
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        root = etree.fromstring(z.read("word/document.xml"), parser)
    except etree.XMLSyntaxError:
        raise DocxError("Не удалось прочитать содержимое .docx.")
    lines = []
    for p in root.iter(f"{{{W}}}p"):
        parts = []
        for el in p.iter():
            tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
            if tag == "t":
                parts.append(el.text or "")
            elif tag == "tab":
                parts.append(" ")
            elif tag in ("br", "cr"):
                parts.append("\n")
        text = "".join(parts).replace("\xa0", " ").strip()
        if not text:
            lines.append("")
            continue
        style = p.find("w:pPr/w:pStyle", NS)
        sid = (style.get(f"{{{W}}}val", "") if style is not None else "").lower()
        jc = p.find("w:pPr/w:jc", NS)
        centered = jc is not None and jc.get(f"{{{W}}}val") == "center"
        runs = [r for r in p.findall(".//w:r", NS) if "".join(r.itertext()).strip()]
        all_bold = bool(runs) and all(_is_on(r.find("w:rPr/w:b", NS)) for r in runs)
        if text.startswith("#"):
            pass
        elif sid in ("title", "heading1", "заголовок1") or (centered and all_bold):
            text = "# " + text
        elif sid in ("heading2", "heading3", "заголовок2", "заголовок3"):
            text = "## " + text
        lines.append(text)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out


# ---------------------------------------------------------------- PDF

_FONTS = {}


def _fonts():
    if _FONTS:
        return _FONTS
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    d = Path(__file__).resolve().parents[2] / "main" / "fonts"
    for key, fname in (("ArendaSerif", "DejaVuSerif.ttf"), ("ArendaSerif-Bold", "DejaVuSerif-Bold.ttf")):
        pdfmetrics.registerFont(TTFont(key, str(d / fname)))
    _FONTS.update(regular="ArendaSerif", bold="ArendaSerif-Bold")
    return _FONTS


def build_pdf(blocks, title="Договор"):
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    f = _fonts()
    base = ParagraphStyle("b", fontName=f["regular"], fontSize=10, leading=14, alignment=TA_JUSTIFY, spaceAfter=3)
    h1 = ParagraphStyle("h1", parent=base, fontName=f["bold"], fontSize=13, leading=17, alignment=TA_CENTER,
                        spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=base, fontName=f["bold"], alignment=TA_LEFT, spaceBefore=8, spaceAfter=3)
    cell = ParagraphStyle("c", parent=base, alignment=TA_LEFT, spaceAfter=0, leading=13)
    story = []
    for b in blocks:
        t = b["type"]
        if t == "title":
            story.append(Paragraph(xesc(b["text"]), h1))
        elif t == "section":
            story.append(Paragraph(xesc(b["text"]), h2))
        elif t == "para":
            story.append(Paragraph(xesc(b["text"]), base))
        else:
            story.append(Spacer(1, 6 * mm))
            data = [[Paragraph(xesc(l) or "&nbsp;", cell), Paragraph(xesc(r) or "&nbsp;", cell)] for l, r in b["rows"]]
            tbl = Table(data, colWidths=[85 * mm, 85 * mm])
            tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                     ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            story.append(tbl)
    if not story:
        story.append(Paragraph(" ", base))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=25 * mm, rightMargin=15 * mm, topMargin=20 * mm,
                            bottomMargin=20 * mm, title=title)
    doc.build(story)
    return buf.getvalue()
