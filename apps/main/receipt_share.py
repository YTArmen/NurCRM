"""ТЗ 30.10.2026 №9: публичная ссылка на электронный чек (QR на чеке).

POST/DELETE api/main/pos/sales/{id}/share-link/ — создать (идемпотентно) / отозвать.
GET /r/<token> — страница чека без входа (5 языков по Accept-Language), /r/<token>.pdf — PDF.
JSON того же чека — api/public/receipts/<token>/ (media_receipts.py, ТЗ Н-32).

Авторы: Умар и Claude (Anthropic), октябрь 2026.
"""
import os
from datetime import timedelta
from html import escape

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import permissions
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.media_receipts import (
    PublicReceiptThrottle,
    _company_of,
    _err,
    _link_or_none,
    _noindex,
    build_receipt_pdf,
    receipt_public_data,
)
from apps.main.models import Sale, SaleReceiptLink, _new_receipt_token

SHARE_TTL_DAYS = int(os.getenv("RECEIPT_SHARE_TTL_DAYS", "90"))


def share_url(token):
    return settings.RECEIPT_SHARE_URL_TEMPLATE.format(token=token)


def share_url_for_sale(sale):
    """`share_url` для ответа продажи: null, если ссылки нет или она не действует."""
    try:
        link = sale.public_link
    except SaleReceiptLink.DoesNotExist:
        return None
    if link.revoked_at is not None or (link.expires_at is not None and link.expires_at <= timezone.now()):
        return None
    return share_url(link.token)


def _share_payload(link, created):
    url = share_url(link.token)
    return {
        "url": url,
        "token": link.token,
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "pdf_url": f"{url}.pdf",
        "created": created,
    }


class SaleShareLinkAPIView(APIView):
    """POST — ссылка на чек (повтор → та же, created:false); DELETE — отозвать (страница → 404)."""

    permission_classes = [permissions.IsAuthenticated]

    def _sale(self, request, pk):
        company = _company_of(request.user)
        return Sale.objects.filter(id=pk, company=company).first() if company else None

    def post(self, request, pk):
        sale = self._sale(request, pk)
        if sale is None:
            return _err("not_found", "Продажа не найдена.", 404)
        now = timezone.now()
        expires = now + timedelta(days=SHARE_TTL_DAYS)
        with transaction.atomic():
            link = SaleReceiptLink.objects.select_for_update().filter(sale=sale).first()
            if link is not None:
                dead = link.revoked_at is not None or (link.expires_at is not None and link.expires_at <= now)
                if not dead:
                    if link.expires_at is None:  # ссылка из Н-32 — даём ей срок
                        link.expires_at = expires
                        link.save(update_fields=["expires_at"])
                    return Response(_share_payload(link, False))
                # отозванная/просроченная: новый токен, старый больше не откроется
                link.token = _new_receipt_token()
                link.revoked_at = None
                link.expires_at = expires
                link.save(update_fields=["token", "revoked_at", "expires_at"])
                return Response(_share_payload(link, True))
        try:
            with transaction.atomic():
                link = SaleReceiptLink.objects.create(sale=sale, company=sale.company, expires_at=expires)
            created = True
        except IntegrityError:  # параллельный запрос успел первым
            link = SaleReceiptLink.objects.get(sale=sale)
            created = False
        return Response(_share_payload(link, created))

    def delete(self, request, pk):
        sale = self._sale(request, pk)
        if sale is None:
            return _err("not_found", "Продажа не найдена.", 404)
        n = SaleReceiptLink.objects.filter(sale=sale, revoked_at__isnull=True).update(revoked_at=timezone.now())
        return Response({"revoked": bool(n)})


# --------------------------------------------------------------------------
# Публичная страница /r/<token>
# --------------------------------------------------------------------------

LANGS = ("ru", "ky", "uz", "tr", "en")

T = {
    "ru": {"receipt": "Чек", "qty": "Кол-во", "price": "Цена", "sum": "Сумма", "discount": "Скидка",
           "subtotal": "Без скидки", "total": "Итого", "paid_by": "Оплата", "cashier": "Кассир",
           "client": "Покупатель", "bonus_earned": "Бонусов начислено", "bonus_redeemed": "Оплачено бонусами",
           "returned": "Возвращено", "purchase_total": "Сумма покупки", "to_pay": "К оплате", "pdf": "Скачать PDF", "print": "Печать", "not_found": "Чек не найден",
           "not_found_text": "Ссылка неверная, отозвана или срок её действия истёк.",
           "status": {"paid": "Оплачен", "partially_returned": "Частично возвращён", "returned": "Возвращён",
                      "debt": "В долг"},
           "pay": {"cash": "Наличные", "card": "Карта", "transfer": "Перевод", "debt": "Долг", "mixed": "Смешанная",
                   "offset": "Зачёт", "bonus": "Бонусы"}},
    "ky": {"receipt": "Чек", "qty": "Саны", "price": "Баасы", "sum": "Суммасы", "discount": "Арзандатуу",
           "subtotal": "Арзандатуусуз", "total": "Жыйынтык", "paid_by": "Төлөм", "cashier": "Кассир",
           "client": "Сатып алуучу", "bonus_earned": "Бонус кошулду", "bonus_redeemed": "Бонус менен төлөндү",
           "returned": "Кайтарылды", "purchase_total": "Сатып алуу суммасы", "to_pay": "Төлөнүүчү сумма", "pdf": "PDF жүктөө", "print": "Басып чыгаруу", "not_found": "Чек табылган жок",
           "not_found_text": "Шилтеме туура эмес, жокко чыгарылган же мөөнөтү бүткөн.",
           "status": {"paid": "Төлөнгөн", "partially_returned": "Жарым-жартылай кайтарылган",
                      "returned": "Кайтарылган", "debt": "Карызга"},
           "pay": {"cash": "Накталай", "card": "Карта", "transfer": "Которуу", "debt": "Карыз", "mixed": "Аралаш",
                   "offset": "Эсепке алуу", "bonus": "Бонустар"}},
    "uz": {"receipt": "Chek", "qty": "Soni", "price": "Narxi", "sum": "Summa", "discount": "Chegirma",
           "subtotal": "Chegirmasiz", "total": "Jami", "paid_by": "To'lov", "cashier": "Kassir",
           "client": "Xaridor", "bonus_earned": "Bonus qo'shildi", "bonus_redeemed": "Bonus bilan to'landi",
           "returned": "Qaytarildi", "purchase_total": "Xarid summasi", "to_pay": "To'lov uchun", "pdf": "PDF yuklab olish", "print": "Chop etish", "not_found": "Chek topilmadi",
           "not_found_text": "Havola noto'g'ri, bekor qilingan yoki muddati tugagan.",
           "status": {"paid": "To'langan", "partially_returned": "Qisman qaytarilgan", "returned": "Qaytarilgan",
                      "debt": "Qarzga"},
           "pay": {"cash": "Naqd", "card": "Karta", "transfer": "O'tkazma", "debt": "Qarz", "mixed": "Aralash",
                   "offset": "Hisobga olish", "bonus": "Bonuslar"}},
    "tr": {"receipt": "Fiş", "qty": "Adet", "price": "Fiyat", "sum": "Tutar", "discount": "İndirim",
           "subtotal": "İndirimsiz", "total": "Toplam", "paid_by": "Ödeme", "cashier": "Kasiyer",
           "client": "Müşteri", "bonus_earned": "Kazanılan bonus", "bonus_redeemed": "Bonusla ödenen",
           "returned": "İade edildi", "purchase_total": "Alışveriş tutarı", "to_pay": "Ödenecek tutar", "pdf": "PDF indir", "print": "Yazdır", "not_found": "Fiş bulunamadı",
           "not_found_text": "Bağlantı geçersiz, iptal edilmiş veya süresi dolmuş.",
           "status": {"paid": "Ödendi", "partially_returned": "Kısmen iade", "returned": "İade edildi",
                      "debt": "Borç"},
           "pay": {"cash": "Nakit", "card": "Kart", "transfer": "Havale", "debt": "Borç", "mixed": "Karma",
                   "offset": "Mahsup", "bonus": "Bonus"}},
    "en": {"receipt": "Receipt", "qty": "Qty", "price": "Price", "sum": "Amount", "discount": "Discount",
           "subtotal": "Before discount", "total": "Total", "paid_by": "Payment", "cashier": "Cashier",
           "client": "Customer", "bonus_earned": "Bonus earned", "bonus_redeemed": "Paid with bonus",
           "returned": "Refunded", "purchase_total": "Purchase total", "to_pay": "To pay", "pdf": "Download PDF", "print": "Print", "not_found": "Receipt not found",
           "not_found_text": "The link is invalid, revoked or expired.",
           "status": {"paid": "Paid", "partially_returned": "Partially refunded", "returned": "Refunded",
                      "debt": "On credit"},
           "pay": {"cash": "Cash", "card": "Card", "transfer": "Transfer", "debt": "Debt", "mixed": "Mixed",
                   "offset": "Offset", "bonus": "Bonus"}},
}


def pick_lang(request):
    q = (request.GET.get("lang") or "").lower()[:2]
    if q in LANGS:
        return q
    for part in (request.META.get("HTTP_ACCEPT_LANGUAGE") or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
    return "ru"


def _pay_label(t, method):
    m = str(method or "")
    return t["pay"].get(m) or m.capitalize()


CSS = """*{box-sizing:border-box}body{margin:0;background:#f3f4f6;color:#111;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.r{max-width:420px;margin:0 auto;padding:16px}.card{background:#fff;border-radius:12px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:18px;margin:0 0 2px;text-align:center}.muted{color:#6b7280;font-size:13px;text-align:center}
.meta{display:flex;justify-content:space-between;margin:12px 0 8px;font-size:13px;color:#374151}
.st{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#dcfce7;color:#166534}
.st.partially_returned,.st.debt{background:#fef3c7;color:#92400e}.st.returned{background:#fee2e2;color:#991b1b}
.it{border-top:1px dashed #e5e7eb;padding:8px 0}.it.ret{opacity:.75}.it .n{font-weight:500;word-break:break-word}
.row{display:flex;justify-content:space-between;gap:8px;font-size:14px}.row.s{color:#6b7280;font-size:13px}
.tot{border-top:2px solid #111;margin-top:8px;padding-top:8px}.tot .row.b{font-weight:700;font-size:17px}
.btns{display:flex;gap:8px;margin-top:16px}.btns a,.btns button{flex:1;text-align:center;padding:11px;border-radius:10px;
border:1px solid #d1d5db;background:#fff;color:#111;font:inherit;text-decoration:none;cursor:pointer}
.btns a{background:#111;color:#fff;border-color:#111}
@media print{body{background:#fff}.card{box-shadow:none}.btns{display:none}}"""


def render_receipt_html(data, lang, pdf_href):
    t = T[lang]
    e = escape
    c = data["company"]
    head = f'<h1>{e(c["name"])}</h1>'
    for line in (c.get("address"), c.get("phone")):
        if line:
            head += f'<div class="muted">{e(line)}</div>'
    date = data["date"][:16].replace("T", " ")
    st = data.get("status") or "paid"
    rows = []
    for it in data["items"]:
        disc = ""
        if it.get("line_discount") and it["line_discount"] != "0.00":
            disc = f'<div class="row s"><span>{e(t["discount"])}</span><span>−{e(it["line_discount"])}</span></div>'
        tag = f' <span class="st returned">{e(t["returned"])}</span>' if it.get("returned") else ""
        r_qty = it.get("returned_qty")
        if not it.get("returned") and r_qty and r_qty not in ("0", "0.000"):
            # ТЗ 31.10 п.3: строка вернулась частично — сколько вернули
            tag = f' <span class="st returned">{e(t["returned"])}: {e(r_qty)}</span>'
        rows.append(
            f'<div class="it{" ret" if it.get("returned") else ""}"><div class="n">{e(it["name"])}{tag}</div>'
            f'<div class="row s"><span>{e(it["qty"])} × {e(it["unit_price"])}</span>'
            f'<span>{e(it["total"])}</span></div>{disc}</div>'
        )
    tt = data["totals"]

    def line(label, value, cls="row"):
        return f'<div class="{cls}"><span>{e(label)}</span><span>{e(value)}</span></div>'

    tot = []
    if tt.get("discount_total") and tt["discount_total"] != "0.00":
        tot.append(line(t["subtotal"], tt["subtotal"]))
        tot.append(line(t["discount"], "−" + tt["discount_total"]))
    has_return = bool(tt.get("returned_total") and tt["returned_total"] != "0.00")
    if has_return:  # 31.10: сумма покупки, возвращено, к оплате
        tot.append(line(t["purchase_total"], tt["purchase_total"]))
        tot.append(line(t["returned"], "−" + tt["returned_total"]))
        tot.append(line(t["to_pay"], tt["to_pay"], "row b"))
    else:
        tot.append(line(t["total"], tt["total"], "row b"))
    for p in data.get("payments") or []:
        tot.append(line(f'{t["paid_by"]}: {_pay_label(t, p["method"])}', p["amount"], "row s"))
    b = data.get("bonus") or {}
    if b.get("redeemed") and b["redeemed"] != "0.00":
        tot.append(line(t["bonus_redeemed"], b["redeemed"], "row s"))
    if b.get("earned") and b["earned"] != "0.00":
        tot.append(line(t["bonus_earned"], "+" + b["earned"], "row s"))
    if data.get("cashier"):
        tot.append(line(t["cashier"], data["cashier"], "row s"))
    if data.get("client"):
        tot.append(line(t["client"], data["client"], "row s"))
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f'<title>{e(t["receipt"])} № {e(data["number"])} — {e(c["name"])}</title><style>{CSS}</style></head>'
        f'<body><div class="r"><div class="card">{head}'
        f'<div class="meta"><span>{e(t["receipt"])} № {e(data["number"])}<br>{e(date)}</span>'
        f'<span><span class="st {e(st)}">{e(t["status"].get(st, st))}</span></span></div>'
        f'{"".join(rows)}<div class="tot">{"".join(tot)}</div></div>'
        f'<div class="btns"><a href="{e(pdf_href)}">{e(t["pdf"])}</a>'
        f'<button type="button" onclick="window.print()">{e(t["print"])}</button></div></div></body></html>'
    )


def render_not_found_html(lang):
    t = T[lang]
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex,nofollow"><title>{escape(t["not_found"])}</title>'
        f'<style>{CSS}</style></head><body><div class="r"><div class="card"><h1>{escape(t["not_found"])}</h1>'
        f'<div class="muted">{escape(t["not_found_text"])}</div></div></div></body></html>'
    )


class _IgnoreAcceptNegotiation(BaseContentNegotiation):
    """ТЗ 31.10 п.17: страница сама отдаёт HTML/PDF (HttpResponse) — Accept не проверяем, иначе DRF отвечал 406
    на `Accept: application/pdf` ещё до get(), и даже неизвестный токен .pdf давал 406 вместо 404."""

    def select_parser(self, request, parsers):
        return parsers[0] if parsers else None

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class PublicReceiptPageView(APIView):
    """GET /r/<token> — HTML; GET /r/<token>.pdf — PDF. Неизвестная/отозванная/просроченная → 404."""

    content_negotiation_class = _IgnoreAcceptNegotiation

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [PublicReceiptThrottle]

    def get(self, request, token, fmt=None):
        lang = pick_lang(request)
        link = _link_or_none(token)
        if link is None:
            return _noindex(HttpResponse(render_not_found_html(lang), status=404,
                                         content_type="text/html; charset=utf-8"))
        data = receipt_public_data(link)
        if fmt == "pdf":
            resp = HttpResponse(build_receipt_pdf(data), content_type="application/pdf")
            resp["Content-Disposition"] = f'inline; filename="receipt-{data["number"]}.pdf"'
        else:
            resp = HttpResponse(render_receipt_html(data, lang, f"{token}.pdf"),
                                content_type="text/html; charset=utf-8")
            resp["Content-Language"] = lang
        resp["Cache-Control"] = "private, no-store"
        return _noindex(resp)


def robots_txt(request):
    return HttpResponse("User-agent: *\nDisallow: /r/\nDisallow: /api/\n", content_type="text/plain")
