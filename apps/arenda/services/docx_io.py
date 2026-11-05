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


TITLE_COLOR = "1F4E79"
GRID_HEAD = "DCE9F5"


def _run(text, bold=False, size=24, color=None):
    rpr = f'<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman"/>' \
          f'{"<w:b/>" if bold else ""}{f'<w:color w:val="{color}"/>' if color else ""}<w:sz w:val="{size}"/></w:rPr>'
    return f'<w:r>{rpr}<w:t xml:space="preserve">{xesc(text)}</w:t></w:r>'


def _para(text, *, bold=False, align="both", size=24, before=0, after=80, color=None, rule=False):
    border = (f'<w:pBdr><w:bottom w:val="single" w:sz="8" w:space="1" w:color="{color or "000000"}"/></w:pBdr>'
              if rule else "")
    return (f'<w:p><w:pPr>{border}<w:spacing w:before="{before}" w:after="{after}"/><w:jc w:val="{align}"/></w:pPr>'
            f'{_run(text, bold, size, color)}</w:p>')


def _grid(rows, header):
    n = max(len(r) for r in rows)
    w = 9000 // n
    edge = '<w:{0} w:val="single" w:sz="4" w:space="0" w:color="8FAACC"/>'
    borders = "<w:tblBorders>" + "".join(edge.format(e) for e in ("top", "left", "bottom", "right", "insideH", "insideV")) \
              + "</w:tblBorders>"
    trs = []
    for cells, head in zip(rows, header):
        cells = list(cells) + [""] * (n - len(cells))
        shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{GRID_HEAD}"/>' if head else ""
        tcs = "".join(
            f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>{shade}</w:tcPr>'
            f'<w:p><w:pPr><w:spacing w:before="60" w:after="60"/></w:pPr>{_run(c, bold=head, size=22)}</w:p></w:tc>'
            for c in cells)
        trs.append(f"<w:tr>{tcs}</w:tr>")
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for _ in range(n))
    return (f'<w:tbl><w:tblPr><w:tblW w:w="{w * n}" w:type="dxa"/>{borders}</w:tblPr>'
            f'<w:tblGrid>{grid}</w:tblGrid>{"".join(trs)}</w:tbl><w:p/>')


def _cell(text, width):
    paras = "".join(
        f'<w:p><w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr>{_run(line)}</w:p>' for line in (text.split("\n") or [""]))
    return f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>{paras}</w:tc>'


def build_docx(blocks):
    body = []
    for b in blocks:
        t = b["type"]
        if t == "title":
            body.append(_para(b["text"], bold=True, align="center", size=28, after=200, color=TITLE_COLOR, rule=True))
        elif t == "section":
            body.append(_para(b["text"], bold=True, align="left", before=160, after=60))
        elif t == "para":
            body.append(_para(b["text"], after=20 if b.get("tight") else 80))
        elif t == "grid":
            body.append(_grid(b["rows"], b["header"]))
        elif t == "break":
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
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
    for key, fname in (("ArendaMono", "DejaVuSansMono.ttf"), ("ArendaMono-Bold", "DejaVuSansMono-Bold.ttf")):
        pdfmetrics.registerFont(TTFont(key, str(d / fname)))
    _FONTS.update(regular="ArendaSerif", bold="ArendaSerif-Bold", mono="ArendaMono", mono_bold="ArendaMono-Bold")
    return _FONTS


def build_pdf(blocks, title="Договор"):
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    f = _fonts()
    blue = colors.HexColor("#" + TITLE_COLOR)
    base = ParagraphStyle("b", fontName=f["regular"], fontSize=10, leading=14, alignment=TA_JUSTIFY, spaceAfter=3)
    tight = ParagraphStyle("t", parent=base, spaceAfter=0)
    h1 = ParagraphStyle("h1", parent=base, fontName=f["bold"], fontSize=13, leading=17, alignment=TA_CENTER,
                        spaceAfter=2, textColor=blue)
    cellh = ParagraphStyle("ch", parent=base, alignment=TA_LEFT, spaceAfter=0, leading=13, fontName=f["bold"])
    h2 = ParagraphStyle("h2", parent=base, fontName=f["bold"], alignment=TA_LEFT, spaceBefore=8, spaceAfter=3)
    cell = ParagraphStyle("c", parent=base, alignment=TA_LEFT, spaceAfter=0, leading=13)
    story = []
    for b in blocks:
        t = b["type"]
        if t == "title":
            story.append(Paragraph(xesc(b["text"]), h1))
            story.append(HRFlowable(width="100%", thickness=1.2, color=blue, spaceBefore=0, spaceAfter=8))
        elif t == "section":
            story.append(Paragraph(xesc(b["text"]), h2))
        elif t == "para":
            story.append(Paragraph(xesc(b["text"]), tight if b.get("tight") else base))
        elif t == "break":
            story.append(PageBreak())
        elif t == "grid":
            n = max(len(r) for r in b["rows"])
            data = [[Paragraph(xesc(c) or "&nbsp;", cellh if head else cell) for c in list(r) + [""] * (n - len(r))]
                    for r, head in zip(b["rows"], b["header"])]
            tbl = Table(data, colWidths=[170 * mm / n] * n)
            style = [("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#8FAACC")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                     ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
            style += [("BACKGROUND", (0, i), (-1, i), colors.HexColor("#" + GRID_HEAD))
                      for i, head in enumerate(b["header"]) if head]
            tbl.setStyle(TableStyle(style))
            story += [Spacer(1, 2 * mm), tbl, Spacer(1, 2 * mm)]
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


def _som(t) -> str:
    """Тыйыны -> «4 700» или «4 700,50» (как в тексте чека)."""
    t = int(t)
    sign = "-" if t < 0 else ""
    t = abs(t)
    out = f"{t // 100:,}".replace(",", " ")
    return sign + out + (f",{t % 100:02d}" if t % 100 else "")


def build_receipt_pdf(receipt_label, snap, *, paid_text, reversed_=False):
    """Чек шириной 80 мм (высота по содержимому), тем же движком reportlab + DejaVu, что и договор."""
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    f = _fonts()
    width, margin, size, lead = 80 * mm, 3 * mm, 8, 10.5
    inner = width - 2 * margin
    items = []  # (kind, left, right, bold)

    def center(t, bold=False):
        for line in simpleSplit(t or "", f["mono_bold" if bold else "mono"], size, inner) or [""]:
            items.append(("c", line, "", bold))

    def row(l, r="", bold=False):
        fn = f["mono_bold" if bold else "mono"]
        right_w = len(r) * size * 0.602
        for i, line in enumerate(simpleSplit(l, fn, size, max(inner - right_w - 4, inner * 0.4)) or [""]):
            items.append(("r", line, r if i == 0 else "", bold))

    def hr():
        items.append(("h", "", "", False))

    c = snap["company"]
    center(c.get("name") or "", True)
    if c.get("inn"):
        center("ИНН " + c["inn"])
    if c.get("address"):
        center(c["address"])
    hr()
    center(f"ЧЕК № {receipt_label}", True)
    center(paid_text)
    hr()
    row("Арендатор: " + snap["tenant"]["name"])
    if snap.get("units"):
        row("Помещение: " + ", ".join(snap["units"]))
    if snap.get("period"):
        row("За период: " + snap["period"])
    hr()
    for ln in snap["lines"]:
        row(ln["title"], _som(ln["amount"]))
    row("ИТОГО, сом", _som(snap["amount"]), True)
    hr()
    row("Способ: " + snap["method_title"])
    if snap.get("cashier"):
        row("Кассир: " + snap["cashier"])
    row("Остаток долга: " + _som(snap["debt_after"]) + " сом")
    if reversed_:
        center("ОПЛАТА СТОРНИРОВАНА", True)
    center("Спасибо!")
    height = 2 * margin + len(items) * lead + 6
    buf = io.BytesIO()
    cv = canvas.Canvas(buf, pagesize=(width, height))
    cv.setTitle(f"Чек {receipt_label}")
    y = height - margin - size
    for kind, left, right, bold in items:
        fn = f["mono_bold" if bold else "mono"]
        if kind == "h":
            cv.setDash(1, 2)
            cv.line(margin, y + size / 2, width - margin, y + size / 2)
            cv.setDash()
        elif kind == "c":
            cv.setFont(fn, size)
            cv.drawCentredString(width / 2, y, left)
        else:
            cv.setFont(fn, size)
            cv.drawString(margin, y, left)
            if right:
                cv.drawRightString(width - margin, y, right)
        y -= lead
    cv.save()
    return buf.getvalue()


def build_act_pdf(label, snap):
    """Акт сверки (A4): таблица проводок, сальдо, фраза итога и подписи сторон (в т.ч. арендатора)."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    f = _fonts()
    base = ParagraphStyle("b", fontName=f["regular"], fontSize=9.5, leading=13, spaceAfter=3)
    h1 = ParagraphStyle("h1", parent=base, fontName=f["bold"], fontSize=13, leading=17, alignment=TA_CENTER)
    ctr = ParagraphStyle("ct", parent=base, alignment=TA_CENTER)
    cell = ParagraphStyle("c", parent=base, spaceAfter=0, leading=12, alignment=TA_LEFT)
    cellb = ParagraphStyle("cb", parent=cell, fontName=f["bold"])
    num = ParagraphStyle("n", parent=cell, alignment=TA_RIGHT)
    numb = ParagraphStyle("nb", parent=num, fontName=f["bold"])

    def d(x):
        return f"{x[8:10]}.{x[5:7]}.{x[:4]}"

    P = lambda t, st=cell: Paragraph(xesc(str(t)) or "&nbsp;", st)  # noqa: E731
    data = [[P("Дата", cellb), P("Документ", cellb), P("Дебет, сом", numb), P("Кредит, сом", numb)],
            [P("Сальдо на начало", cellb), "", "", P(_som(snap["opening_balance"]), numb)]]
    for r in snap["rows"]:
        data.append([P(d(r["date"])), P(r["document"]), P(_som(r["debit"]) if r["debit"] else "", num),
                     P(_som(r["credit"]) if r["credit"] else "", num)])
    data.append([P("Обороты за период", cellb), "", P(_som(snap["turnover_debit"]), numb),
                 P(_som(snap["turnover_credit"]), numb)])
    data.append([P("Сальдо на конец", cellb), "", "", P(_som(snap["closing_balance"]), numb)])
    last = len(data) - 1
    tbl = Table(data, colWidths=[24 * mm, 80 * mm, 33 * mm, 33 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("SPAN", (0, 1), (2, 1)), ("SPAN", (0, last), (2, last)), ("SPAN", (0, last - 1), (1, last - 1)),
        ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    sg = snap.get("signatures") or {}
    signer = sg.get("tenant_signer") or sg.get("tenant") or snap["tenant"]["name"]
    story = [
        Paragraph(xesc(f"АКТ СВЕРКИ № {label}"), h1),
        Paragraph(xesc(f"взаимных расчётов за период с {d(snap['period_from'])} по {d(snap['period_to'])}"), ctr),
        Spacer(1, 3 * mm),
        Paragraph(xesc(f"Между: {snap['company'].get('name') or ''} и {snap['tenant']['name']}"), base),
        Spacer(1, 2 * mm), tbl, Spacer(1, 4 * mm),
        Paragraph(xesc(snap["summary"]), base), Spacer(1, 12 * mm),
        Paragraph(xesc(f"Директор: ______________ / {sg.get('director') or ''} /"), base), Spacer(1, 4 * mm),
        Paragraph(xesc(f"Бухгалтер: ______________ / {sg.get('chief_accountant') or ''} /"), base), Spacer(1, 4 * mm),
        Paragraph(xesc(f"Арендатор: ______________ / {signer} /"), base),
    ]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm,
                      bottomMargin=15 * mm, title=f"Акт сверки {label}").build(story)
    return buf.getvalue()
