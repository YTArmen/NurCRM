"""Движок шаблонов договоров: поля {{поле}}, спецстроки, HTML и структурный вид (блоки).

Синтаксис шаблона:
  {{поле}}           подстановка значения
  # текст            заголовок по центру
  ## текст           раздел
  [[подписи]]        блок подписей сторон (аренда / работодатель)
  [[подписи-работник]] блок подписей работодателя и работника
Остальные непустые строки — абзацы.
Внутри готового текста (снимка) блок подписей развёрнут в строки «~~ левая ~~ правая».
"""
import re
from datetime import date, datetime
from decimal import Decimal
from html import escape

from .doc_defaults import (DEFAULT_EMPLOYMENT_NAME, DEFAULT_EMPLOYMENT_TEXT, DEFAULT_LEASE_NAME,
                           DEFAULT_LEASE_TEXT)

FIELD_RE = re.compile(r"\{\{\s*([^{}\n]+?)\s*\}\}")
BLANK = "________"

# маркеры подсветки в предпросмотре (в чистом тексте их нет)
OK_A, OK_B, MISS_A, MISS_B = "\x01", "\x02", "\x03", "\x04"

SIGN_TOKENS = {"[[подписи]]": "party", "[[подписи-работник]]": "employee"}

# ---------------------------------------------------------------- реестр полей

LEASE_FIELDS = [
    ("номер_договора", "Номер договора"), ("дата", "Дата договора"), ("город", "Город"), ("объект", "Объект (филиал)"),
    ("арендодатель", "Арендодатель (из Настроек)"), ("директор", "Директор (из Настроек)"),
    ("арендатор", "Арендатор"), ("тип_арендатора", "Тип арендатора"), ("инн", "ИНН арендатора"),
    ("подписант", "Подписант"), ("паспорт", "Паспорт"), ("телефон", "Телефон арендатора"),
    ("деятельность", "Вид деятельности"), ("помещение", "Помещение"), ("этаж", "Этаж"), ("площадь", "Площадь, м²"),
    ("ставка", "Ставка за м²"), ("аренда_в_месяц", "Аренда в месяц"), ("начало", "Начало аренды"),
    ("окончание", "Окончание аренды"), ("срок_мес", "Срок, месяцев"), ("день_оплаты", "День оплаты"),
    ("депозит", "Депозит"), ("пеня", "Пеня"), ("индексация", "Индексация"), ("коммунальные", "Коммунальные"),
    ("сумма_договора", "Сумма договора"),
]
# реквизиты арендодателя из Настроек (дополнительно к ТЗ)
LANDLORD_FIELDS = [
    ("арендодатель_инн", "ИНН арендодателя"), ("арендодатель_адрес", "Адрес арендодателя"),
    ("арендодатель_банк", "Банк арендодателя"), ("арендодатель_счёт", "Расчётный счёт арендодателя"),
    ("арендодатель_бик", "БИК банка"), ("арендодатель_телефон", "Телефон арендодателя"),
]
EMPLOYMENT_FIELDS = [
    ("номер_договора", "Номер договора"), ("дата", "Дата договора"), ("город", "Город"), ("объект", "Объект (филиал)"),
    ("арендодатель", "Организация (из Настроек)"), ("работодатель", "Работодатель (= организация)"),
    ("директор", "Директор (из Настроек)"),
    ("сотрудник", "Сотрудник"), ("должность", "Должность"), ("оклад", "Оклад"), ("график", "График"),
    ("дата_приёма", "Дата приёма"), ("телефон_сотрудника", "Телефон сотрудника"),
]


def fields_for(kind):
    base = LEASE_FIELDS if kind == "lease" else EMPLOYMENT_FIELDS
    return [{"key": k, "label": l, "group": "main"} for k, l in base] + \
           [{"key": k, "label": l, "group": "landlord"} for k, l in LANDLORD_FIELDS]


SPECIAL_LINES = [
    {"token": "# ", "label": "Заголовок по центру"},
    {"token": "## ", "label": "Раздел"},
    {"token": "[[подписи]]", "label": "Блок подписей сторон"},
    {"token": "[[подписи-работник]]", "label": "Блок подписей работника"},
]


def norm(name):
    return re.sub(r"\s+", "_", name.strip().lower()).replace("ё", "е")


def _registry(kind):
    return {norm(f["key"]): f for f in fields_for(kind)}


def canonical_key(name, kind):
    f = _registry(kind).get(norm(name))
    return f["key"] if f else name.strip()


def normalize_template_text(text, kind):
    """Приводит {{ Поле }} к каноническому {{поле}}, [[ подписи ]] к [[подписи]]."""
    def fld(m):
        return "{{%s}}" % canonical_key(m.group(1), kind)

    text = FIELD_RE.sub(fld, text)
    text = re.sub(r"\[\[\s*([^\[\]\n]+?)\s*\]\]", lambda m: "[[%s]]" % m.group(1).strip().lower(), text)
    return text


def used_fields(text, kind):
    """(известные, неизвестные) ключи полей в порядке появления."""
    reg = _registry(kind)
    known, unknown = [], []
    for m in FIELD_RE.finditer(text or ""):
        n = norm(m.group(1))
        if n in reg:
            key = reg[n]["key"]
            if key not in known:
                known.append(key)
        elif m.group(1).strip() not in unknown:
            unknown.append(m.group(1).strip())
    return known, unknown


# ---------------------------------------------------------------- форматирование

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


def fmt_date(d):
    if not d:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _group(n):
    return f"{n:,}".replace(",", " ")


def fmt_som(tyiyn):
    """4 700 сом; 4 700,50 сом."""
    tyiyn = int(tyiyn)
    sign = "-" if tyiyn < 0 else ""
    som, ty = divmod(abs(tyiyn), 100)
    s = _group(som) + (f",{ty:02d}" if ty else "")
    return f"{sign}{s} сом"


def fmt_decimal(x):
    """42.50 -> 42,5; 10.00 -> 10."""
    x = Decimal(str(x)).normalize()
    s = format(x, "f")
    return s.replace(".", ",")


_ONES = {False: ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"],
         True: ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]}
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
          "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUND = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
_UNITS = [(("тысяча", "тысячи", "тысяч"), True), (("миллион", "миллиона", "миллионов"), False),
          (("миллиард", "миллиарда", "миллиардов"), False)]


def plural(n, forms):
    n = abs(n) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


def _triple(n, fem):
    out = []
    out.append(_HUND[n // 100])
    r = n % 100
    if 10 <= r <= 19:
        out.append(_TEENS[r - 10])
    else:
        out.append(_TENS[r // 10])
        out.append(_ONES[fem][r % 10])
    return [w for w in out if w]


def number_words(n):
    n = int(n)
    if n == 0:
        return "ноль"
    parts = []
    idx = 0
    while n > 0:
        n, t = divmod(n, 1000)
        if t:
            if idx == 0:
                parts = _triple(t, False) + parts
            else:
                forms, fem = _UNITS[min(idx, len(_UNITS)) - 1]
                parts = _triple(t, fem) + [plural(t, forms)] + parts
        idx += 1
    return " ".join(parts)


def som_words(tyiyn):
    """четыре тысячи семьсот сом (+ «50 тыйынов», если есть)."""
    som, ty = divmod(abs(int(tyiyn)), 100)
    s = f"{number_words(som)} сом"
    if ty:
        s += f" {ty:02d} " + plural(ty, ("тыйын", "тыйына", "тыйынов"))
    return s


def fmt_som_words(tyiyn):
    return f"{fmt_som(tyiyn)} ({som_words(tyiyn)})"


def term_months(start, end):
    if not start or not end:
        return None
    from datetime import timedelta
    e1 = end + timedelta(days=1)
    m = (e1.year - start.year) * 12 + e1.month - start.month + (1 if e1.day > start.day else 0)
    return max(1, m)


# ---------------------------------------------------------------- значения полей


def landlord_values(settings_obj, branch=None, city=""):
    s = settings_obj
    return {
        "арендодатель": s.org_name, "работодатель": s.org_name, "директор": s.director,
        "арендодатель_инн": s.inn, "арендодатель_адрес": s.legal_address, "арендодатель_банк": s.bank,
        "арендодатель_счёт": s.account, "арендодатель_бик": s.bik, "арендодатель_телефон": s.phone,
        "объект": branch.name if branch is not None else "",
        "город": city or "",
    }


def lease_values(*, settings_obj, branch, city, tenant, units, terms, label, on_date, tenant_form_title):
    """tenant — dict полей арендатора; terms — dict (start_date, end_date, rent_monthly, pay_day, ...)."""
    from .money import calc_rent

    v = landlord_values(settings_obj, branch, city)
    units = sorted(units, key=lambda u: (u.floor, u.number))
    area = sum((Decimal(str(u.area)) for u in units), Decimal(0))
    monthly = terms.get("rent_monthly")
    if monthly is None:
        monthly = sum(calc_rent(u.area, u.rate) for u in units)
    start, end = terms.get("start_date"), terms.get("end_date")
    months = term_months(start, end)
    deposit_months = int(terms.get("deposit_months") or 0)
    deposit = terms.get("deposit_amount")
    if deposit is None:
        deposit = monthly * deposit_months
    rates = {u.rate for u in units}
    pct = Decimal(str(terms.get("indexation_percent") or 0))
    penalty = Decimal(str(terms.get("penalty_rate") if terms.get("penalty_rate") is not None
                          else settings_obj.penalty_rate))
    floors = []
    for u in units:
        if str(u.floor) not in floors:
            floors.append(str(u.floor))
    v.update({
        "номер_договора": label, "дата": fmt_date(on_date),
        "арендатор": tenant.get("name", ""), "тип_арендатора": tenant_form_title,
        "инн": tenant.get("inn", ""), "подписант": tenant.get("contact_person", ""),
        "паспорт": tenant.get("passport", ""), "телефон": tenant.get("phone", ""),
        "деятельность": tenant.get("activity", ""),
        "помещение": ", ".join(u.number for u in units), "этаж": ", ".join(floors),
        "площадь": fmt_decimal(area) if units else "",
        "ставка": fmt_som(rates.pop()) if len(rates) == 1 else (
            fmt_som(round(int(monthly) / area)) if units and area else ""),
        "аренда_в_месяц": fmt_som_words(monthly) if units else "",
        "начало": fmt_date(start), "окончание": fmt_date(end) if end else ("бессрочно" if start else ""),
        "срок_мес": str(months) if months else "",
        "день_оплаты": str(terms.get("pay_day") or 10),
        "депозит": fmt_som_words(deposit) if deposit else "не предусмотрен",
        "пеня": f"{fmt_decimal(penalty)}%",
        "индексация": f"{fmt_decimal(pct)}% в год" if pct > 0 else "не предусмотрена",
        "коммунальные": (terms.get("utilities") or "").strip() or "оплачиваются Арендатором отдельно",
        "сумма_договора": fmt_som_words(int(monthly) * months) if months and units else "",
    })
    return v


def employment_values(*, settings_obj, branch, city, employee, label, on_date):
    v = landlord_values(settings_obj, branch, city)
    v.update({
        "номер_договора": label, "дата": fmt_date(on_date),
        "сотрудник": employee.full_name, "должность": employee.position,
        "оклад": fmt_som_words(employee.salary) if employee.salary else "",
        "график": employee.schedule, "дата_приёма": fmt_date(employee.hired_at),
        "телефон_сотрудника": employee.phone,
    })
    return v


# ---------------------------------------------------------------- подписи


def sign_rows(values, kind, which):
    """Строки таблицы подписей: список пар (левая, правая)."""
    g = lambda k: (values.get(k) or "").strip()  # noqa: E731
    left = []
    org = g("арендодатель")
    left.append(("АРЕНДОДАТЕЛЬ" if kind == "lease" and which == "party" else "РАБОТОДАТЕЛЬ"))
    left.append(org or BLANK)
    if g("арендодатель_инн"):
        left.append("ИНН " + g("арендодатель_инн"))
    if g("арендодатель_адрес"):
        left.append("Адрес: " + g("арендодатель_адрес"))
    if g("арендодатель_банк"):
        left.append("Банк: " + g("арендодатель_банк") + (f", БИК {g('арендодатель_бик')}" if g("арендодатель_бик") else ""))
    if g("арендодатель_счёт"):
        left.append("Р/с " + g("арендодатель_счёт"))
    if g("арендодатель_телефон"):
        left.append("Тел.: " + g("арендодатель_телефон"))
    left += ["", f"{BLANK} / {g('директор') or BLANK} /", "Директор"]

    right = []
    if kind == "lease" and which == "party":
        right.append("АРЕНДАТОР")
        right.append(g("арендатор") or BLANK)
        for lab, k in (("ИНН ", "инн"), ("Паспорт: ", "паспорт"), ("Тел.: ", "телефон")):
            if g(k):
                right.append(lab + g(k))
        right += ["", f"{BLANK} / {g('подписант') or g('арендатор') or BLANK} /", "Подпись"]
    else:
        right.append("РАБОТНИК")
        right.append(g("сотрудник") or BLANK)
        if g("должность"):
            right.append("Должность: " + g("должность"))
        if g("телефон_сотрудника"):
            right.append("Тел.: " + g("телефон_сотрудника"))
        right += ["", f"{BLANK} / {g('сотрудник') or BLANK} /", "Подпись"]
    n = max(len(left), len(right))
    left += [""] * (n - len(left))
    right += [""] * (n - len(right))
    return list(zip(left, right))


# ---------------------------------------------------------------- рендер


def substitute(text, values, kind, mark=False):
    """Подставляет поля. Возвращает (текст, filled, missing, unknown)."""
    reg = _registry(kind)
    filled, missing, unknown = [], [], []
    vals = {norm(k): ("" if v is None else str(v)) for k, v in values.items()}

    def repl(m):
        raw = m.group(1).strip()
        n = norm(raw)
        known = n in reg
        key = reg[n]["key"] if known else raw
        val = vals.get(n, "").strip().replace("\x01", "").replace("\x02", "").replace("\x03", "").replace("\x04", "")
        if not known and key not in unknown:
            unknown.append(key)
        if val:
            if key not in filled:
                filled.append(key)
            return f"{OK_A}{val}{OK_B}" if mark else val
        if key not in missing:
            missing.append(key)
        return f"{MISS_A}{BLANK}{MISS_B}" if mark else BLANK

    out = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        tok = SIGN_TOKENS.get(line.strip().lower())
        if tok:
            for l, r in sign_rows(values, kind, tok):
                out.append(f"~~ {l} ~~ {r}".rstrip())
            continue
        out.append(FIELD_RE.sub(repl, line))
    return "\n".join(out), filled, missing, unknown


def to_blocks(text):
    """Структурный вид: [{'type': 'title'|'section'|'para'|'table', ...}] — для HTML, DOCX, PDF."""
    blocks = []
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("~~"):
            cells = [c.strip() for c in s[2:].split("~~")]
            while len(cells) < 2:
                cells.append("")
            if blocks and blocks[-1]["type"] == "table":
                blocks[-1]["rows"].append(cells[:2])
            else:
                blocks.append({"type": "table", "rows": [cells[:2]]})
        elif s.startswith("## "):
            blocks.append({"type": "section", "text": s[3:].strip()})
        elif s.startswith("# "):
            blocks.append({"type": "title", "text": s[2:].strip()})
        else:
            blocks.append({"type": "para", "text": s})
    return blocks


def _h(s):
    s = escape(s)
    return (s.replace(OK_A, '<span class="f-ok" style="background:#e6f4ea">').replace(OK_B, "</span>")
            .replace(MISS_A, '<span class="f-miss" style="background:#fde2e2;color:#b00020">')
            .replace(MISS_B, "</span>"))


def blocks_to_html(blocks):
    out = ['<div class="contract" style="font-family:\'Times New Roman\',serif;line-height:1.5">']
    for b in blocks:
        t = b["type"]
        if t == "title":
            out.append(f'<h1 style="text-align:center;font-size:1.3em;margin:0 0 .8em">{_h(b["text"])}</h1>')
        elif t == "section":
            out.append(f'<h2 style="font-size:1.05em;margin:1em 0 .3em">{_h(b["text"])}</h2>')
        elif t == "para":
            out.append(f'<p style="margin:.3em 0;text-align:justify">{_h(b["text"])}</p>')
        else:
            out.append('<table class="signs" style="width:100%;margin-top:1.2em;border-collapse:collapse"><tbody>')
            for l, r in b["rows"]:
                out.append(f'<tr><td style="width:50%;vertical-align:top;padding:1px 8px 1px 0">{_h(l)}</td>'
                           f'<td style="width:50%;vertical-align:top;padding:1px 0">{_h(r)}</td></tr>')
            out.append("</tbody></table>")
    out.append("</div>")
    return "\n".join(out)


def strip_marks(s):
    for c in (OK_A, OK_B, MISS_A, MISS_B):
        s = s.replace(c, "")
    return s


def render_template(text, values, kind):
    """Основной вход: возвращает dict(text, html, blocks, filled, missing, unknown)."""
    plain, filled, missing, unknown = substitute(text, values, kind, mark=False)
    marked, *_ = substitute(text, values, kind, mark=True)
    return {"text": plain, "html": blocks_to_html(to_blocks(marked)), "blocks": to_blocks(plain),
            "filled": filled, "missing": missing, "unknown": unknown}


def snapshot_html(snapshot_text):
    return blocks_to_html(to_blocks(snapshot_text))


# ---------------------------------------------------------------- шаблоны по умолчанию


def ensure_default_templates(company, kind=None):
    """Один типовой шаблон на вид, если у компании ещё нет ни одного (включая архивные)."""
    from ..models import Template

    kinds = [kind] if kind else ["lease", "employment"]
    for k in kinds:
        if Template.objects.filter(company=company, kind=k).exists():
            continue
        name, text = ((DEFAULT_LEASE_NAME, DEFAULT_LEASE_TEXT) if k == "lease"
                      else (DEFAULT_EMPLOYMENT_NAME, DEFAULT_EMPLOYMENT_TEXT))
        Template.objects.create(company=company, kind=k, name=name, text=text)
