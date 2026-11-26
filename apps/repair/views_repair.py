"""П2-1 (15.11.2026, p2115): ремонт. Адреса — api/main/repair-orders/… (см. urls.py)."""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.repair.models import RepairOrder, RepairPayment, RepairSettings
from apps.repair.perms import company_of, err, guard, perms_of, pin_ok, privileged
from apps.repair.services import Z, cash_move, hist, lock_company, money, next_number, norm_serial, open_shift

FIELDS = ("customer_name", "customer_phone", "device", "serial", "problem", "condition_note", "accessories", "warranty_ref")
NEXT = {"accepted": ("in_work", "ready", "cancelled"), "in_work": ("ready", "accepted", "cancelled"),
        "ready": ("in_work", "cancelled"), "issued": (), "cancelled": ()}


def warranty_mode(company):
    s = RepairSettings.objects.filter(company=company).first()
    return s.warranty_mode if s else "hint"


def payload(o, warranty=None):
    price = o.final_price if o.final_price is not None else o.estimated_price
    if o.is_warranty and warranty == "zero":
        price = Z
    return {
        "id": str(o.id), "number": o.number, "number_display": f"Р-{o.number}", "local_number": o.local_number or None,
        "status": o.status, "client": str(o.client_id) if o.client_id else None, "customer_name": o.customer_name,
        "customer_phone": o.customer_phone, "device": o.device, "serial": o.serial, "problem": o.problem,
        "condition_note": o.condition_note, "accessories": o.accessories, "estimated_price": format(o.estimated_price, "f"),
        "final_price": format(o.final_price, "f") if o.final_price is not None else None,
        "is_warranty": o.is_warranty, "warranty_ref": o.warranty_ref, "warranty_mode": warranty,
        "price_to_pay": format(price, "f"), "prepaid": format(o.prepaid, "f"), "refunded": format(o.refunded, "f"),
        "net_prepaid": format(o.net_prepaid, "f"), "due": format(price - o.net_prepaid, "f"),
        "due_date": o.due_date.isoformat() if o.due_date else None, "master": str(o.master_id) if o.master_id else None,
        "sale": str(o.sale_id) if o.sale_id else None, "branch": str(o.branch_id) if o.branch_id else None,
        "notified_at": o.notified_at.isoformat() if o.notified_at else None,
        "issued_at": o.issued_at.isoformat() if o.issued_at else None,
        "created_at": o.created_at.isoformat() if o.created_at else None, "history": o.history,
    }


def _apply_fields(o, d, create=False):
    for f in FIELDS:
        if f in d:
            setattr(o, f, str(d.get(f) or "").strip()[:255 if f != "problem" else 4000])
    if create and not (o.device or "").strip():
        return err("validation_error", "Укажите устройство (device).", fields={"device": ["Обязательно."]})
    o.serial_norm = norm_serial(o.serial)
    for f in ("estimated_price", "final_price"):
        if f in d:
            if d.get(f) in (None, "") and f == "final_price":
                o.final_price = None
                continue
            v = money(d.get(f), allow_zero=True)
            if v is None:
                return err("validation_error", f"{f}: сумма ≥ 0.", fields={f: ["Сумма ≥ 0."]})
            setattr(o, f, v)
    if "is_warranty" in d:
        o.is_warranty = d.get("is_warranty") in (True, "true", "1", 1)
    if "due_date" in d:
        o.due_date = parse_date(str(d.get("due_date"))) if d.get("due_date") else None
    if "client" in d:
        from apps.main.models import Client

        o.client = Client.objects.filter(company_id=o.company_id, pk=d.get("client")).first() if d.get("client") else None
    if "master" in d:
        from apps.users.models import User

        o.master = User.objects.filter(pk=d.get("master")).filter(Q(company_id=o.company_id) | Q(pk=getattr(o.company, "owner_id", None))).first() if d.get("master") else None
    return None


class RepairOrdersAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        bad = guard(request, "repair", "repair.view")
        if bad:
            return bad
        company = company_of(request.user)
        qp = request.query_params
        qs = RepairOrder.objects.filter(company=company)
        if qp.get("status"):
            qs = qs.filter(status__in=[s for s in qp["status"].split(",") if s])
        q = (qp.get("q") or "").strip()
        if q:
            ns = norm_serial(q)
            cond = Q(customer_name__icontains=q) | Q(customer_phone__icontains=q) | Q(device__icontains=q)
            if q.isdigit():
                cond |= Q(number=int(q))
            if len(ns) >= 4:
                cond |= Q(serial_norm__endswith=ns)
            qs = qs.filter(cond)
        d1, d2 = parse_date(qp.get("from") or ""), parse_date(qp.get("to") or "")
        if d1:
            qs = qs.filter(created_at__date__gte=d1)
        if d2:
            qs = qs.filter(created_at__date__lte=d2)
        try:
            page, size = max(1, int(qp.get("page") or 1)), min(200, max(1, int(qp.get("page_size") or 50)))
        except ValueError:
            page, size = 1, 50
        mode = warranty_mode(company)
        total = qs.count()
        rows = [payload(o, mode) for o in qs[(page - 1) * size: page * size]]
        return Response({"count": total, "page": page, "results": rows})

    def post(self, request):
        bad = guard(request, "repair", "repair.manage")
        if bad:
            return bad
        company = company_of(request.user)
        key = (request.headers.get("Idempotency-Key") or "").strip()[:128]
        if not key:
            return err("idempotency_key_required", "Нужен заголовок Idempotency-Key.")
        old = RepairOrder.objects.filter(company=company, idempotency_key=key).first()
        if old:
            return Response({**payload(old, warranty_mode(company)), "replayed": True})
        d = request.data if hasattr(request.data, "get") else {}
        with transaction.atomic():
            lock_company(company)
            o = RepairOrder(company=company, created_by=request.user, idempotency_key=key)
            sh = open_shift(request.user, company)
            o.branch = getattr(sh, "branch", None)
            bad = _apply_fields(o, d, create=True)
            if bad:
                return bad
            o.number = next_number(RepairOrder, company)
            hist(o, request.user, "accepted")
            o.save()
        _feed(o, request.user)
        return Response({**payload(o, warranty_mode(company)), "replayed": False}, status=201)


def _get(request, pk):
    return get_object_or_404(RepairOrder, pk=pk, company=company_of(request.user))


def _feed(o, user):
    try:
        from apps.main.change_feed import record_change

        record_change(o.company_id, "repair_changed", actor_id=getattr(user, "pk", None), entity="repair_order", entity_id=o.pk,
                      data={"repair_order": str(o.pk), "number": o.number, "status": o.status})
    except Exception:  # noqa: BLE001
        pass


class RepairOrderDetailAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        bad = guard(request, "repair", "repair.view")
        if bad:
            return bad
        o = _get(request, pk)
        out = payload(o, warranty_mode(o.company))
        out["payments"] = [{"id": str(p.id), "kind": p.kind, "amount": format(p.amount, "f"), "method": p.method,
                            "shift": str(p.shift_id) if p.shift_id else None, "approved_by_pin": p.approved_by_pin,
                            "created_at": p.created_at.isoformat()} for p in o.payments.order_by("created_at")]
        return Response(out)

    def patch(self, request, pk):
        bad = guard(request, "repair", "repair.manage")
        if bad:
            return bad
        with transaction.atomic():
            o = RepairOrder.objects.select_for_update().get(pk=_get(request, pk).pk)
            if o.status in ("issued", "cancelled"):
                return err("bad_transition", "Выданный или отменённый ремонт менять нельзя.", 409, status=o.status)
            bad = _apply_fields(o, request.data if hasattr(request.data, "get") else {})
            if bad:
                return bad
            hist(o, request.user, "edited")
            o.save()
        _feed(o, request.user)
        return Response(payload(o, warranty_mode(o.company)))


class RepairStatusAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        bad = guard(request, "repair", "repair.manage")
        if bad:
            return bad
        new = str((request.data or {}).get("status") or "")
        with transaction.atomic():
            o = RepairOrder.objects.select_for_update().get(pk=_get(request, pk).pk)
            if new == o.status:
                return Response({**payload(o, warranty_mode(o.company)), "replayed": True})
            if new not in NEXT.get(o.status, ()):
                return err("bad_transition", f"Нельзя из «{o.status}» в «{new}». Выдача — через issue/.", 409,
                           status=o.status, allowed=list(NEXT.get(o.status, ())))
            if new == "cancelled" and o.net_prepaid > 0:
                return err("prepay_not_refunded", "Сначала верните предоплату (refund/).", 409, amount=format(o.net_prepaid, "f"))
            o.status = new
            if new == "cancelled":
                o.cancelled_at = timezone.now()
            hist(o, request.user, new, note=str((request.data or {}).get("note") or "")[:255])
            o.save()
        _feed(o, request.user)
        return Response(payload(o, warranty_mode(o.company)))


class RepairMoneyAPIView(APIView):
    """POST repair-orders/<id>/prepay/ и …/refund/ {amount, method: cash|card|transfer|mbank…, shift?} + Idempotency-Key."""
    permission_classes = [permissions.IsAuthenticated]
    kind = "prepay"

    def post(self, request, pk):
        bad = guard(request, "repair", "repair.manage" if self.kind == "prepay" else "repair.refund")
        if bad:
            return bad
        pin = False
        if self.kind == "refund" and not privileged(request.user):
            if not pin_ok(request):
                return err("confirm_required", "Возврат предоплаты подтверждает старший: нужен PIN (X-Confirm-Token).", 403)
            pin = True
        key = (request.headers.get("Idempotency-Key") or "").strip()[:128]
        if not key:
            return err("idempotency_key_required", "Нужен заголовок Idempotency-Key.")
        d = request.data if hasattr(request.data, "get") else {}
        amount = money(d.get("amount"))
        if amount is None:
            return err("validation_error", "amount: больше 0.", fields={"amount": ["Больше 0."]})
        method = str(d.get("method") or "cash").strip().lower()[:16]
        company = company_of(request.user)
        with transaction.atomic():
            o = RepairOrder.objects.select_for_update().get(pk=_get(request, pk).pk)
            old = RepairPayment.objects.filter(order=o, idempotency_key=key).first()
            if old:
                return Response({**payload(o, warranty_mode(company)), "replayed": True})
            if o.status in ("issued", "cancelled") and self.kind == "prepay":
                return err("bad_transition", "Ремонт уже закрыт.", 409, status=o.status)
            if self.kind == "refund" and amount > o.net_prepaid:
                return err("refund_exceeds_paid", "Нельзя вернуть больше внесённого.", 409, max=format(o.net_prepaid, "f"))
            sh = open_shift(request.user, company, d.get("shift"))
            if sh is None and method == "cash":
                return err("shift_closed", "Откройте смену: наличные идут через кассу смены.", 400)
            name = f"{'Предоплата' if self.kind == 'prepay' else 'Возврат предоплаты'} ремонта Р-{o.number}"
            cf = cash_move(company=company, user=request.user, shift=sh, direction="in" if self.kind == "prepay" else "out",
                           amount=amount, method=method, source_id=o.pk, name=name, key=f"repair:{o.pk}:{key}")
            RepairPayment.objects.create(order=o, kind=self.kind, amount=amount, method=method, shift=sh,
                                         cashflow_id=getattr(cf, "pk", None), user=request.user, approved_by_pin=pin, idempotency_key=key)
            if self.kind == "prepay":
                o.prepaid += amount
            else:
                o.refunded += amount
            hist(o, request.user, self.kind, amount=format(amount, "f"), method=method)
            o.save()
        _feed(o, request.user)
        return Response({**payload(o, warranty_mode(company)), "replayed": False}, status=201)


class RepairPrepayAPIView(RepairMoneyAPIView):
    kind = "prepay"


class RepairRefundAPIView(RepairMoneyAPIView):
    kind = "refund"


class RepairIssueAPIView(APIView):
    """POST repair-orders/<id>/issue/ {final_price?, sale?} — выдача. due = цена − (предоплата − возвраты).
    due > 0 — нужен чек продажи на эту сумму (sale), иначе 409 payment_missing; due < 0 — 409 overpay_not_refunded."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        bad = guard(request, "repair", "repair.manage")
        if bad:
            return bad
        d = request.data if hasattr(request.data, "get") else {}
        with transaction.atomic():
            o = RepairOrder.objects.select_for_update().get(pk=_get(request, pk).pk)
            if o.status == "issued":
                return Response({**payload(o, warranty_mode(o.company)), "replayed": True})
            if o.status == "cancelled":
                return err("bad_transition", "Ремонт отменён.", 409, status=o.status)
            if "final_price" in d and d.get("final_price") not in (None, ""):
                v = money(d.get("final_price"), allow_zero=True)
                if v is None:
                    return err("validation_error", "final_price: сумма ≥ 0.", fields={"final_price": ["Сумма ≥ 0."]})
                o.final_price = v
            mode = warranty_mode(o.company)
            price = o.final_price if o.final_price is not None else o.estimated_price
            if o.is_warranty and mode == "zero":
                price = Z
            due = price - o.net_prepaid
            if due < 0:
                return err("overpay_not_refunded", "Предоплата больше цены — верните разницу (refund/).", 409, amount=format(-due, "f"))
            if due > 0:
                from apps.main.models import Sale

                sale = Sale.objects.filter(company=o.company, pk=d.get("sale")).first() if d.get("sale") else None
                if sale is None:
                    return err("payment_missing", "Доплату нужно провести чеком на кассе и передать его id (sale).", 409, due=format(due, "f"))
                if sale.status not in ("paid", "debt", "partially_returned") or (sale.total or Z) < due:
                    return err("payment_missing", "Чек не проведён или его сумма меньше доплаты.", 409, due=format(due, "f"),
                               sale_total=format(sale.total or Z, "f"))
                o.sale = sale
            o.status, o.issued_at = "issued", timezone.now()
            hist(o, request.user, "issued", price=format(price, "f"), sale=str(o.sale_id) if o.sale_id else None)
            o.save()
        _feed(o, request.user)
        return Response(payload(o, mode))


class RepairNotifyAPIView(APIView):
    """POST repair-orders/<id>/notify/ — отметка «клиенту сообщили» + готовая ссылка WhatsApp с текстом."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        bad = guard(request, "repair", "repair.manage")
        if bad:
            return bad
        import re
        from urllib.parse import quote

        o = _get(request, pk)
        due = (o.final_price if o.final_price is not None else o.estimated_price) - o.net_prepaid
        shop = getattr(o.company, "name", "") or ""
        text = str((request.data or {}).get("text") or "").strip() or (
            f"Здравствуйте! {shop}: ремонт №{o.number} ({o.device}) " + ("готов, можно забирать." if o.status == "ready" else f"— статус: {o.status}.")
            + (f" К оплате: {due:.2f} сом." if due > 0 and o.status == "ready" else ""))
        phone = re.sub(r"\D", "", o.customer_phone or "")
        o.notified_at = timezone.now()
        hist(o, request.user, "notified")
        o.save(update_fields=["notified_at", "history", "updated_at"])
        return Response({"notified_at": o.notified_at.isoformat(), "text": text,
                         "whatsapp_url": f"https://wa.me/{phone}?text={quote(text)}" if phone else None})


class RepairBySerialAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        bad = guard(request, "repair", "repair.view")
        if bad:
            return bad
        s = norm_serial(request.query_params.get("serial"))
        if len(s) < 4:
            return err("validation_error", "serial: от 4 символов.", fields={"serial": ["От 4 символов."]})
        company = company_of(request.user)
        qs = RepairOrder.objects.filter(company=company).filter(Q(serial_norm=s) | Q(serial_norm__endswith=s))[:20]
        return Response([payload(o, warranty_mode(company)) for o in qs])


class RepairSettingsAPIView(APIView):
    """GET/PUT repair/settings/ {warranty_mode: zero|hint} (п.4 решения 15.11: оба варианта, выбирает владелец)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = company_of(request.user)
        return Response({"warranty_mode": warranty_mode(company), "perms": sorted(perms_of(request.user))})

    def put(self, request):
        if not privileged(request.user):
            return err("permission_denied", "Настройки ремонта меняет владелец.", 403, permission="owner")
        mode = str((request.data or {}).get("warranty_mode") or "")
        if mode not in ("zero", "hint"):
            return err("validation_error", "warranty_mode: zero или hint.", fields={"warranty_mode": ["zero, hint"]})
        RepairSettings.objects.update_or_create(company=company_of(request.user), defaults={"warranty_mode": mode})
        return self.get(request)

    patch = put


class RepairStaffPermsAPIView(APIView):
    """GET/PUT repair/permissions/<user_id>/ {perms: [...]} — владелец/админ."""
    permission_classes = [permissions.IsAuthenticated]

    def _user(self, request, user_id):
        from apps.users.models import User

        company = company_of(request.user)
        return get_object_or_404(User.objects.filter(Q(company=company) | Q(pk=getattr(company, "owner_id", None))), pk=user_id)

    def get(self, request, user_id):
        u = self._user(request, user_id)
        return Response({"user": str(u.pk), "perms": sorted(perms_of(u))})

    def put(self, request, user_id):
        from apps.repair.models import RepairStaffPerms
        from apps.repair.perms import ALL

        if not privileged(request.user):
            return err("permission_denied", "Права выдаёт владелец или админ.", 403, permission="owner")
        u = self._user(request, user_id)
        perms = [p for p in ((request.data or {}).get("perms") or []) if p in ALL]
        RepairStaffPerms.objects.update_or_create(user=u, defaults={"company": company_of(request.user), "perms": perms})
        try:
            from apps.main.change_feed import _emit

            _emit(company_of(request.user).pk, "staff_changed", entity="user", entity_id=u.pk, data={"user": str(u.pk)})
        except Exception:  # noqa: BLE001
            pass
        return Response({"user": str(u.pk), "perms": sorted(perms_of(u))})


class RepairImportAPIView(APIView):
    """POST repair-orders/import/ {items:[{device_id, local_id, local_number, status, prepaid, created_at, …поля}]}
    Перенос локальных записей кассы: по (device_id, local_id) — один раз. Предоплата — только цифрой
    (деньги уже прошли через старые смены), движений денег нет."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        bad = guard(request, "repair", "repair.manage")
        if bad:
            return bad
        company = company_of(request.user)
        items = (request.data or {}).get("items") or []
        if not isinstance(items, list) or len(items) > 500:
            return err("validation_error", "items: список до 500.", fields={"items": ["До 500."]})
        out = []
        with transaction.atomic():
            lock_company(company)
            for i, it in enumerate(items):
                it = it if isinstance(it, dict) else {}
                dev, lid = str(it.get("device_id") or "")[:64], str(it.get("local_id") or "")[:64]
                if not dev or not lid:
                    out.append({"index": i, "error": "device_id_local_id_required"})
                    continue
                o = RepairOrder.objects.filter(company=company, device_id=dev, local_id=lid).first()
                if o:
                    out.append({"index": i, "id": str(o.id), "number": o.number, "replayed": True})
                    continue
                o = RepairOrder(company=company, created_by=request.user, device_id=dev, local_id=lid,
                                local_number=str(it.get("local_number") or "")[:32])
                bad = _apply_fields(o, it, create=True)
                if bad:
                    out.append({"index": i, "error": bad.data.get("code"), "detail": bad.data.get("detail")})
                    continue
                st = str(it.get("status") or "accepted")
                o.status = st if st in RepairOrder.STATUSES else "accepted"
                o.prepaid = money(it.get("prepaid") or 0, allow_zero=True) or Z
                o.number = next_number(RepairOrder, company)
                hist(o, request.user, "imported", local_number=o.local_number)
                o.save()
                out.append({"index": i, "id": str(o.id), "number": o.number, "replayed": False})
        return Response({"results": out})
