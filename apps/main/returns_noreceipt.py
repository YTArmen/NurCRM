"""П1-1 (14.11.2026, p1114): возврат без исходной продажи и справочник причин возврата.

GET  pos/return-reasons/                 — системные + свои причины компании (всем сотрудникам)
POST pos/return-reasons/                 — своя причина (владелец/админ): {name | names{ru,…}, is_defect?, requires_note?}
DELETE pos/return-reasons/<code>/        — скрыть свою причину (владелец/админ)
POST pos/returns/without-sale/           — Idempotency-Key обязателен; {items[{product, quantity, price}], reason_code, note?,
                                           client?, refund_method: cash|noncash, shift?}
GET  pos/returns/without-sale/?from&to&reason_code&shift
GET  reports/returns-by-reason/?from&to  — возвраты (по чеку и без) по причинам
Права: владелец/админ — сразу; остальные — право «Возврат» (can_view_market_employee_return) И подтверждение старшего
(заголовок X-Confirm-Token из users/pin/verify/). Брак (is_defect) на склад не возвращается.
"""
import re
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

Q2, Q3 = Decimal("0.01"), Decimal("0.001")
LANGS = ("ru", "ky", "en", "tr", "uz")
SYSTEM_REASONS = [
    # code, is_defect, requires_note, names
    ("defect", True, False, ("Брак", "Бракы бар", "Defect", "Kusurlu", "Brak")),
    ("wrong_size", False, False, ("Не подошёл размер или цвет", "Өлчөмү же түсү туура келген жок", "Wrong size or colour",
                                  "Beden veya renk uymadı", "O‘lcham yoki rang to‘g‘ri kelmadi")),
    ("cashier_error", False, True, ("Ошибка кассира", "Кассирдин катасы", "Cashier error", "Kasiyer hatası", "Kassir xatosi")),
    ("changed_mind", False, False, ("Покупатель передумал", "Сатып алуучу оюн өзгөрттү", "Customer changed mind",
                                    "Müşteri vazgeçti", "Xaridor fikridan qaytdi")),
    ("warranty", True, True, ("Гарантия", "Кепилдик", "Warranty", "Garanti", "Kafolat")),
]


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def _owner_like(user):
    from apps.users.permissions_flags import is_privileged

    return is_privileged(user)


def reason_payload(r):
    names = {lang: (r.names or {}).get(lang) or (r.names or {}).get("ru") or r.code for lang in LANGS}
    return {"code": r.code, "name_ru": names["ru"], "name_ky": names["ky"], "name_en": names["en"], "name_tr": names["tr"],
            "name_uz": names["uz"], "names": names, "is_defect": r.is_defect, "requires_note": r.requires_note,
            "is_system": r.company_id is None}


def reasons_qs(company):
    from apps.main.models import ReturnReason

    return ReturnReason.objects.filter(Q(company__isnull=True) | Q(company=company), is_active=True).order_by("sort_order", "id")


def find_reason(company, code):
    code = (code or "").strip()
    if not code:
        return None
    from apps.main.models import ReturnReason

    return (ReturnReason.objects.filter(company=company, code=code, is_active=True).first()
            or ReturnReason.objects.filter(company__isnull=True, code=code, is_active=True).first())


def clean_reason_code(company, payload):
    """Для обычного возврата по чеку (pos/sales/<id>/return/): reason_code необязателен; если прислан — проверяем.
    Возвращает код или "" ; ошибки — ApiError (400 reason_invalid / note_required)."""
    from core.exceptions import ApiError

    code = str((payload or {}).get("reason_code") or "").strip()
    if not code:
        return ""
    r = find_reason(company, code)
    if r is None:
        raise ApiError("reason_invalid", "Нет такой причины возврата.", fields={"reason_code": ["Нет такой причины."]})
    note = str((payload or {}).get("reason") or (payload or {}).get("note") or "").strip()
    if r.requires_note and not note:
        raise ApiError("note_required", "Для этой причины нужен комментарий (reason).", fields={"reason": ["Обязательно."]})
    return r.code


class ReturnReasonsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response([reason_payload(r) for r in reasons_qs(_company(request.user))])

    def post(self, request):
        from apps.main.models import ReturnReason

        company = _company(request.user)
        if not _owner_like(request.user):
            return _err("permission_denied", "Свои причины добавляет владелец или администратор.", 403, permission="owner")
        d = request.data if hasattr(request.data, "get") else {}
        names = d.get("names") if isinstance(d.get("names"), dict) else {}
        ru = str(names.get("ru") or d.get("name") or d.get("name_ru") or "").strip()
        if not ru or len(ru) > 64:
            return _err("validation_error", "Название: от 1 до 64 символов.", fields={"name": ["От 1 до 64."]})
        clean = {lang: str(names.get(lang) or d.get(f"name_{lang}") or "").strip()[:64] for lang in LANGS}
        clean["ru"] = ru
        base = "c_" + (re.sub(r"[^a-z0-9]+", "_", ru.lower()).strip("_")[:20] or "reason")
        code, n = base, 1
        while ReturnReason.objects.filter(company=company, code=code).exists() or ReturnReason.objects.filter(company__isnull=True, code=code).exists():
            n += 1
            code = f"{base}_{n}"
        if ReturnReason.objects.filter(company=company, is_active=True).count() >= 30:
            return _err("limit", "Своих причин не больше 30.", 400)
        r = ReturnReason.objects.create(company=company, code=code, names=clean, is_defect=bool(d.get("is_defect")),
                                        requires_note=bool(d.get("requires_note")), sort_order=500)
        return Response(reason_payload(r), status=201)


class ReturnReasonDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, code):
        from apps.main.models import ReturnReason

        company = _company(request.user)
        if not _owner_like(request.user):
            return _err("permission_denied", "Свои причины убирает владелец или администратор.", 403, permission="owner")
        r = ReturnReason.objects.filter(company=company, code=code, is_active=True).first()
        if r is None:
            if ReturnReason.objects.filter(company__isnull=True, code=code).exists():
                return _err("reason_system", "Общую причину убрать нельзя.", 409)
            return _err("not_found", "Нет такой причины.", 404)
        r.is_active = False
        r.save(update_fields=["is_active"])
        return Response(status=204)


def _dec(v, q):
    try:
        d = Decimal(str(v).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d.quantize(q, rounding=ROUND_HALF_UP)


def rws_payload(r):
    return {"id": str(r.id), "number": r.number, "without_sale": True, "sale": None, "reason_code": r.reason_code,
            "note": r.note, "items": r.items, "total": format(r.total, "f"), "amount": format(r.total, "f"),
            "refund_method": r.refund_method, "refund_cash": format(r.refund_cash, "f"), "refund_noncash": format(r.refund_noncash, "f"),
            "restocked": r.restocked, "client": str(r.client_id) if r.client_id else None, "shift": str(r.shift_id) if r.shift_id else None,
            "branch": str(r.branch_id) if r.branch_id else None, "cashier": str(r.user_id) if r.user_id else None,
            "approved_by_pin": r.approved_by_pin, "created_at": r.created_at.isoformat() if r.created_at else None}


def _day_range(qp):
    f, t = parse_date(qp.get("from") or qp.get("date_from") or ""), parse_date(qp.get("to") or qp.get("date_to") or "")
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(f, time.min), tz) if f else None,
            timezone.make_aware(datetime.combine(t, time.max), tz) if t else None)


class ReturnWithoutSaleAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import ReturnWithoutSale
        from apps.users.permissions_flags import sees_all_sales

        company = _company(request.user)
        qs = ReturnWithoutSale.objects.filter(company=company)
        if not sees_all_sales(request.user):
            qs = qs.filter(user=request.user)
        a, b = _day_range(request.query_params)
        if a:
            qs = qs.filter(created_at__gte=a)
        if b:
            qs = qs.filter(created_at__lte=b)
        for p, f in (("reason_code", "reason_code"), ("shift", "shift_id"), ("branch", "branch_id")):
            if request.query_params.get(p):
                qs = qs.filter(**{f: request.query_params[p]})
        return Response([rws_payload(r) for r in qs[:500]])

    def post(self, request):
        from apps.construction.auto_cashflow import create_auto_cashflow
        from apps.main.models import Client, Product, ReturnWithoutSale
        from apps.main.pos_views import _find_open_shift_for_cashier
        from apps.users.permissions_flags import has_any_flag
        from apps.users.pin import HEADER as PIN_HEADER, token_is_valid

        user, company = request.user, _company(request.user)
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if not key or len(key) > 128:
            return _err("idempotency_key_required", "Нужен заголовок Idempotency-Key.", 400)
        old = ReturnWithoutSale.objects.filter(company=company, idempotency_key=key).first()
        if old:
            return Response({**rws_payload(old), "replayed": True}, status=200)
        pin_ok = False
        if not _owner_like(user):
            if not has_any_flag(user, ("can_view_market_employee_return",)):
                return _err("permission_denied", "Нет права «Возврат».", 403, permission="can_view_market_employee_return",
                            required="can_view_market_employee_return")
            tok = request.META.get(PIN_HEADER, "")
            if not (tok and token_is_valid(tok, user)):
                return _err("confirm_required", "Возврат без чека подтверждает старший: нужен PIN (заголовок X-Confirm-Token).", 403)
            pin_ok = True
        d = request.data if hasattr(request.data, "get") else {}
        reason = find_reason(company, d.get("reason_code"))
        if not str(d.get("reason_code") or "").strip():
            return _err("reason_required", "Укажите причину возврата (reason_code).", 400, fields={"reason_code": ["Обязательно."]})
        if reason is None:
            return _err("reason_invalid", "Нет такой причины возврата.", 400, fields={"reason_code": ["Нет такой причины."]})
        note = str(d.get("note") or d.get("reason") or "").strip()[:255]
        if reason.requires_note and not note:
            return _err("note_required", "Для этой причины нужен комментарий (note).", 400, fields={"note": ["Обязательно."]})
        method = str(d.get("refund_method") or "cash").strip().lower()
        if method not in ("cash", "noncash"):
            return _err("validation_error", "refund_method: cash или noncash.", 400, fields={"refund_method": ["cash или noncash."]})
        raw = d.get("items") if isinstance(d.get("items"), list) else []
        if not raw:
            return _err("items_empty", "Добавьте товары.", 400)
        if len(raw) > 200:
            return _err("too_many_items", "Не больше 200 строк.", 400)
        lines = []
        for i, it in enumerate(raw):
            it = it if isinstance(it, dict) else {}
            qty = _dec(it.get("quantity", it.get("qty")), Q3)
            if qty is None or qty <= 0:
                return _err("validation_error", "Количество больше нуля.", 400, index=i, fields={"quantity": ["Больше нуля."]})
            if it.get("price") in (None, ""):
                return _err("price_required", "Укажите цену, по которой вернули товар.", 422, index=i)
            price = _dec(it.get("price"), Q2)
            if price is None or price < 0:
                return _err("validation_error", "Цена не меньше нуля.", 400, index=i, fields={"price": ["Не меньше нуля."]})
            lines.append((i, str(it.get("product") or ""), qty, price))
        client = None
        if d.get("client"):
            client = Client.objects.filter(company=company, pk=d.get("client")).first()
            if client is None:
                return _err("client_not_found", "Клиент не найден.", 404)
        with transaction.atomic():
            shift = None
            if d.get("shift"):
                from apps.construction.models import CashShift

                shift = CashShift.objects.filter(company=company, pk=d.get("shift"), status=CashShift.Status.OPEN).first()
                if shift is None:
                    return _err("shift_not_open", "Смена не найдена или закрыта.", 400)
                if shift.cashier_id != user.id and not _owner_like(user):
                    return _err("shift_not_owned", "Это чужая смена.", 403)
            else:
                shift = _find_open_shift_for_cashier(company=company, cashier=user)
            if shift is None and method == "cash":
                return _err("shift_closed", "Откройте смену: деньги за возврат выдаются из кассы смены.", 400)
            prods = {str(p.pk): p for p in Product.objects.select_for_update().filter(company=company, pk__in=[l[1] for l in lines if l[1]])}
            items, total = [], Decimal("0.00")
            for i, pid, qty, price in lines:
                p = prods.get(pid)
                if p is None:
                    transaction.set_rollback(True)
                    return _err("product_not_found", "Товар не найден.", 404, index=i)
                lt = (qty * price).quantize(Q2, rounding=ROUND_HALF_UP)
                total += lt
                items.append({"product": pid, "name": p.name, "quantity": format(qty, "f"), "price": format(price, "f"),
                              "line_total": format(lt, "f")})
            restock = not reason.is_defect
            num = (ReturnWithoutSale.objects.select_for_update().filter(company=company).aggregate(m=Max("number"))["m"] or 0) + 1
            r = ReturnWithoutSale.objects.create(
                company=company, branch=getattr(shift, "branch", None),
                shift=shift, user=user, approved_by_pin=pin_ok, number=num, reason_code=reason.code, note=note, client=client,
                items=items, total=total, refund_method=method,
                refund_cash=total if method == "cash" else Decimal("0.00"),
                refund_noncash=total if method == "noncash" else Decimal("0.00"),
                restocked=restock, idempotency_key=key)
            if restock:
                for i, pid, qty, price in lines:
                    p = prods[pid]
                    p.quantity = (Decimal(str(p.quantity or 0)) + qty)
                    p.save(update_fields=["quantity"])
            cf = create_auto_cashflow(
                company=company, branch=getattr(shift, "branch", None), cashbox=getattr(shift, "cashbox", None),
                user=user, shift=shift, type="expense", amount=total, source_kind="product_return", source_id=str(r.id),
                name=f"Возврат без чека №{num}", affects_shift_drawer=(method == "cash" and shift is not None),
                idempotency_key=f"rws:{r.id}", payment_method="cash" if method == "cash" else "transfer",
            ) if total > 0 else None
            if cf is not None:
                r.cashflow_id = cf.pk
                r.save(update_fields=["cashflow_id"])
        try:
            from apps.integrations.events import emit_event

            emit_event(company.pk, "sale.returned", {"return": str(r.id), "sale": None, "without_sale": True, "amount": format(total, "f"),
                                                     "reason_code": r.reason_code, "shift": str(shift.pk) if shift else None,
                                                     "cashier": user.pk})
        except Exception:  # noqa: BLE001
            pass
        try:  # П2-2 p2214: владельцу — «Требует внимания»
            from apps.main.staff_push import notify

            rr = find_reason(company, r.reason_code)
            who = (f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip() or getattr(user, "email", "") or "")
            notify(company, "return_without_sale", {"who": who, "amount": format(total, "f"),
                   "reason": (rr.names or {}).get("ru", r.reason_code) if rr else r.reason_code}, {"return": str(r.id)})
        except Exception:  # noqa: BLE001
            pass
        return Response({**rws_payload(r), "replayed": False}, status=201)


class ReturnsByReasonAPIView(APIView):
    """GET reports/returns-by-reason/?from=&to= → {rows:[{code, names, is_defect, count, amount, with_sale, without_sale}], total}"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import ReturnWithoutSale, SaleReturn
        from apps.users.permissions_flags import sees_all_sales

        company = _company(request.user)
        a, b = _day_range(request.query_params)
        sr = SaleReturn.objects.filter(company=company).exclude(status="voided")
        rw = ReturnWithoutSale.objects.filter(company=company)
        if not sees_all_sales(request.user):
            sr, rw = sr.filter(user=request.user), rw.filter(user=request.user)
        if a:
            sr, rw = sr.filter(created_at__gte=a), rw.filter(created_at__gte=a)
        if b:
            sr, rw = sr.filter(created_at__lte=b), rw.filter(created_at__lte=b)
        acc = {}
        for row in sr.values("reason_code", "is_defect").annotate(c=Count("id"), s=Sum("returned_amount")):
            code = row["reason_code"] or ("defect" if row["is_defect"] else "unspecified")
            e = acc.setdefault(code, {"count": 0, "amount": Decimal(0), "with_sale": 0, "without_sale": 0})
            e["count"] += row["c"]; e["with_sale"] += row["c"]; e["amount"] += row["s"] or 0
        for row in rw.values("reason_code").annotate(c=Count("id"), s=Sum("total")):
            e = acc.setdefault(row["reason_code"], {"count": 0, "amount": Decimal(0), "with_sale": 0, "without_sale": 0})
            e["count"] += row["c"]; e["without_sale"] += row["c"]; e["amount"] += row["s"] or 0
        known = {r.code: r for r in reasons_qs(company)}
        from apps.main.models import ReturnReason

        for r in ReturnReason.objects.filter(company=company, code__in=list(acc)):
            known.setdefault(r.code, r)
        rows = []
        for code, e in sorted(acc.items(), key=lambda kv: -kv[1]["amount"]):
            r = known.get(code)
            names = reason_payload(r)["names"] if r else {lang: ("Без причины" if lang == "ru" else "—") for lang in LANGS}
            rows.append({"code": code, "names": names, "is_defect": bool(r and r.is_defect), "count": e["count"],
                         "amount": format(Decimal(e["amount"]).quantize(Q2), "f"), "with_sale": e["with_sale"], "without_sale": e["without_sale"]})
        tot = sum((Decimal(x["amount"]) for x in rows), Decimal("0.00"))
        return Response({"rows": rows, "total": {"count": sum(x["count"] for x in rows), "amount": format(tot, "f")}})
