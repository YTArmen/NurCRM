"""Л-10 (14.11.2026, l1014): начальный долг клиента при переносе и пометка «перенесён из файла».

GET    clients/<id>/opening-debt/   — {imported, source, batch, opening_debt: {deal, amount, paid, remaining, due_date, note} | null}
POST   clients/<id>/opening-debt/   — Idempotency-Key обязателен; {amount, due_date?, note?, source?, batch?}
         amount = 0 (или не указан) — только пометка «перенесён», без долга.
         Создаёт долговую сделку «Начальный долг (перенос)» без чека и БЕЗ движения денег: долг виден в clients/debtors/,
         гасится как обычный (clients/<id>/pay-debt/).
DELETE clients/<id>/opening-debt/   — откат переноса: снять долг (если по нему ещё не платили) и пометку.
GET    clients/imported/?batch=     — клиенты, перенесённые из файла (для отчёта и отката пачки).
Права: владелец/админ или «Клиенты» + «Долги» (can_view_clients и can_view_debts).
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

TITLE = "Начальный долг (перенос)"
MAX_AMOUNT = Decimal("100000000")


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def _allowed(user):
    from apps.users.permissions_flags import is_privileged

    if is_privileged(user):
        return True
    return getattr(user, "can_view_clients", None) is True and getattr(user, "can_view_debts", None) is True


def _deny():
    return _err("permission_denied", "Начальный долг вносит владелец или сотрудник с правами «Клиенты» и «Долги».", 403,
                permission="can_view_debts", required="can_view_debts", required_any=["can_view_debts", "can_view_clients"])


def _deal_info(deal):
    if deal is None:
        return None
    from django.db.models import Sum

    paid = deal.installments.aggregate(s=Sum("paid_amount"))["s"] or Decimal("0")
    first = deal.installments.order_by("due_date").first()
    remaining = (deal.amount or 0) - (deal.prepayment or 0) - paid
    return {"deal": str(deal.pk), "amount": format(deal.amount, "f"), "paid": format(Decimal(paid).quantize(Decimal("0.01")), "f"),
            "remaining": format(Decimal(max(remaining, 0)).quantize(Decimal("0.01")), "f"),
            "due_date": first.due_date.isoformat() if first and first.due_date else None, "note": deal.note or ""}


def mark_payload(mark):
    if mark is None:
        return {"imported": False, "source": None, "batch": None, "opening_debt": None}
    deal = mark.opening_debt_deal
    return {"imported": True, "source": mark.source or None, "batch": mark.batch or None,
            "opening_debt": _deal_info(deal), "created_at": mark.created_at.isoformat() if mark.created_at else None}


class ClientOpeningDebtAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _client(self, request, pk):
        from apps.main.models import Client

        return get_object_or_404(Client, pk=pk, company=_company(request.user))

    def get(self, request, pk):
        from apps.main.models import ClientImportMark

        client = self._client(request, pk)
        return Response(mark_payload(ClientImportMark.objects.filter(client=client).select_related("opening_debt_deal").first()))

    def post(self, request, pk):
        from apps.main.models import ClientDeal, ClientImportMark

        if not _allowed(request.user):
            return _deny()
        key = (request.headers.get("Idempotency-Key") or "").strip()
        if not key or len(key) > 128:
            return _err("idempotency_key_required", "Нужен заголовок Idempotency-Key.")
        company = _company(request.user)
        d = request.data if hasattr(request.data, "get") else {}
        try:
            amount = Decimal(str(d.get("amount") or "0").replace(",", ".")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            return _err("validation_error", "amount: число.", fields={"amount": ["Число."]})
        if amount < 0 or amount > MAX_AMOUNT:
            return _err("validation_error", "amount: от 0 до 100 000 000.", fields={"amount": ["От 0 до 100 000 000."]})
        due = None
        if d.get("due_date"):
            due = parse_date(str(d.get("due_date")))
            if due is None:
                return _err("validation_error", "due_date: YYYY-MM-DD.", fields={"due_date": ["YYYY-MM-DD."]})
        note = str(d.get("note") or "").strip()[:500]
        source = str(d.get("source") or "").strip()[:64]
        batch = str(d.get("batch") or "").strip()[:64]
        with transaction.atomic():
            client = self._client(request, pk)
            from apps.main.models import Client

            Client.objects.select_for_update().filter(pk=client.pk).first()
            mark = ClientImportMark.objects.filter(client=client).select_related("opening_debt_deal").first()
            if mark is not None:
                if mark.idempotency_key == key:
                    return Response({**mark_payload(mark), "replayed": True}, status=200)
                if mark.opening_debt_deal_id and amount > 0:
                    return _err("opening_debt_exists", "У клиента уже есть начальный долг. Чтобы изменить, сначала снимите его (DELETE).",
                                409, current=mark_payload(mark))
            deal = None
            if amount > 0:
                deal = ClientDeal(
                    company=company, branch=getattr(client, "branch", None), client=client, title=TITLE,
                    kind=ClientDeal.Kind.DEBT, amount=amount, prepayment=Decimal("0"),
                    schedule_version="v1", debt_days=max(1, ((due - timezone.localdate()).days if due else 30)),
                    first_due_date=due or (timezone.localdate() + timedelta(days=30)),
                    note=("Перенос из " + source + ". " if source else "Перенос. ") + note,
                )
                try:
                    deal.save()
                except DjangoValidationError as e:
                    transaction.set_rollback(True)
                    return _err("validation_error", "; ".join(e.messages)[:300] or "Долг не сохранён.",
                                fields=getattr(e, "message_dict", None) or {})
            if mark is None:
                mark = ClientImportMark(company=company, client=client)
            mark.source = source or mark.source
            mark.batch = batch or mark.batch
            if deal is not None:
                mark.opening_debt_deal = deal
            mark.idempotency_key = key
            mark.created_by = request.user
            mark.save()
        try:
            from apps.main.change_feed import _emit

            _emit(company.pk, "client_changed", actor_id=request.user.pk, entity="client", entity_id=client.pk,
                  data={"client": str(client.pk), "opening_debt": format(amount, "f")})
        except Exception:  # noqa: BLE001
            pass
        return Response({**mark_payload(mark), "replayed": False}, status=201)

    def delete(self, request, pk):
        from apps.main.models import ClientImportMark

        if not _allowed(request.user):
            return _deny()
        with transaction.atomic():
            client = self._client(request, pk)
            mark = ClientImportMark.objects.select_for_update().filter(client=client).first()
            if mark is None:
                return _err("not_found", "Клиент не помечен как перенесённый.", 404)
            deal = mark.opening_debt_deal
            if deal is not None:
                if deal.payments.exists() or deal.installments.filter(paid_amount__gt=0).exists():
                    return _err("opening_debt_paid", "По начальному долгу уже были оплаты — снять нельзя.", 409, current=mark_payload(mark))
                mark.opening_debt_deal = None
                mark.save(update_fields=["opening_debt_deal"])
                deal.delete()
            mark.delete()
        return Response(status=204)


class ImportedClientsAPIView(APIView):
    """GET clients/imported/?batch= → [{client, name, phone, source, batch, opening_debt{…}|null, created_at}]"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import ClientImportMark

        if not _allowed(request.user):
            return _deny()
        qs = ClientImportMark.objects.filter(company=_company(request.user)).select_related("client", "opening_debt_deal")
        if request.query_params.get("batch"):
            qs = qs.filter(batch=request.query_params["batch"])
        out = []
        for m in qs.order_by("-created_at")[:2000]:
            c = m.client
            out.append({"client": str(c.pk), "name": getattr(c, "full_name", None) or getattr(c, "name", None),
                        "phone": getattr(c, "phone", None), **mark_payload(m)})
        return Response(out)
