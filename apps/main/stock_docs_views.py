"""П0-4 К-2 (14.11.2026, k2_1411): документы движения склада.

POST stock/documents/  {kind, reason_code?, occurred_at?, comment?, lines:[{product, qty, unit_cost?, note?}]}
     Idempotency-Key (заголовок или idempotency_key): тот же ключ и тело -> 200 replayed; другое тело -> 409 idempotency_conflict.
     kind: receive (+qty, «приход без поставщика») | write_off (−qty, причина обязательна) | count_fix (qty со знаком).
GET  stock/documents/?kind=&reason_code=&product=&from=&to=&status=&page=&page_size=
GET  stock/documents/<id>/
POST stock/documents/<id>/void/ {reason} — аннулирование (обратные движения), владелец/админ, не позже 7 суток.
GET  stock/writeoff-reasons/ — справочник причин списания.
Ошибки: 400 validation_error | reason_required | kind_not_supported, 409 insufficient_stock (lines[]),
422 qty_suspicious (confirm_large=true — провести), 423 period_closed, 403 permission_denied.
"""
import hashlib
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F, Max
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.views import CompanyBranchRestrictedMixin

Q3 = Decimal("0.001")
MAX_QTY = Decimal("100000")
OFFLINE_MAX = timedelta(days=7)

REASONS = [
    ("defect", "Брак", "Брак", "Defect", "Kusurlu", "Brak"),
    ("expired", "Просрочка", "Мөөнөтү өткөн", "Expired", "Süresi geçmiş", "Muddati o'tgan"),
    ("spoiled", "Порча", "Бузулган", "Spoiled", "Bozulmuş", "Buzilgan"),
    ("broken", "Бой", "Сынган", "Broken", "Kırık", "Singan"),
    ("theft", "Кража", "Уурдалган", "Theft", "Hırsızlık", "O'g'irlik"),
    ("own_use", "Для себя", "Өзү үчүн", "Own use", "Kendi kullanımı", "O'zi uchun"),
    ("sample", "Пробник", "Үлгү", "Sample", "Numune", "Namuna"),
    ("regrade", "Пересорт", "Пересорт", "Regrade", "Yanlış çeşit", "Qayta navlash"),
    ("other", "Другое", "Башка", "Other", "Diğer", "Boshqa"),
]
REASON_CODES = {r[0] for r in REASONS}
KINDS = ("receive", "write_off", "count_fix")


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def _may_edit_stock(user):
    from apps.main.pos_serializers import _is_owner_like

    return _is_owner_like(user) or bool(getattr(user, "can_view_products", False)) or bool(getattr(user, "can_edit_products", False))


def _q3(v):
    return format(Decimal(v or 0).quantize(Q3), "f")


def doc_payload(d, *, show_cost=True):
    return {
        "id": str(d.id), "number": d.number, "kind": d.kind, "status": d.status, "reason_code": d.reason_code or None,
        "comment": d.comment, "occurred_at": d.occurred_at.isoformat(), "created_at": d.created_at.isoformat(),
        "created_by": str(d.created_by_id) if d.created_by_id else None,
        "branch": str(d.branch_id) if d.branch_id else None,
        "total_cost": format(d.total_cost, "f") if show_cost else None,
        "voided_at": d.voided_at.isoformat() if d.voided_at else None, "void_reason": d.void_reason or None,
        "lines": [{
            "id": ln.id, "product": str(ln.product_id), "name": ln.name_snapshot, "qty_delta": _q3(ln.qty_delta),
            "qty_before": _q3(ln.qty_before), "qty_after": _q3(ln.qty_after), "note": ln.note or None,
            "unit_cost": (format(ln.unit_cost, "f") if ln.unit_cost is not None else None) if show_cost else None,
        } for ln in d.lines.all()],
    }


def _show_cost(user):
    from apps.main.pos_serializers import _is_owner_like

    return _is_owner_like(user) or getattr(user, "can_see_purchase_price", None) is not False


def _mark_ledger(request, doc, occurred_at):
    """Контекст журнала остатков (К-1): номер документа и настоящее время движения."""
    try:
        request._ledger_doc = f"stockdoc:{doc.id}"
        request._ledger_at = occurred_at.isoformat()
    except AttributeError:
        pass


class StockWriteoffReasonsAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response([{"code": c, "name_ru": ru, "name_ky": ky, "name_en": en, "name_tr": tr, "name_uz": uz,
                          "requires_note": c == "other"} for c, ru, ky, en, tr, uz in REASONS])


class StockDocumentListCreateAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import StockDocument

        qs = StockDocument.objects.filter(company=self._company()).prefetch_related("lines")
        qp = request.query_params
        for f in ("kind", "reason_code", "status"):
            if qp.get(f):
                qs = qs.filter(**{f: qp[f]})
        if qp.get("product"):
            qs = qs.filter(lines__product_id=qp["product"]).distinct()
        d1, d2 = parse_date(qp.get("from") or ""), parse_date(qp.get("to") or "")
        if d1:
            qs = qs.filter(occurred_at__date__gte=d1)
        if d2:
            qs = qs.filter(occurred_at__date__lte=d2)
        try:
            page = max(1, int(qp.get("page") or 1))
            size = min(200, max(1, int(qp.get("page_size") or 50)))
        except ValueError:
            return _err("validation_error", "page и page_size — целые числа.")
        total = qs.count()
        show = _show_cost(request.user)
        rows = qs.order_by("-created_at")[(page - 1) * size: page * size]
        return Response({"count": total, "page": page, "page_size": size, "results": [doc_payload(d, show_cost=show) for d in rows]})

    def post(self, request):
        from apps.main.models import Product, StockDocument, StockDocumentLine

        user = request.user
        company = self._company()
        if company is None:
            return _err("validation_error", "Компания не определена.")
        if not _may_edit_stock(user):
            return _err("permission_denied", "Нет права менять остатки.", 403, permission="can_view_products")
        data = request.data if hasattr(request.data, "get") else {}
        key = (request.headers.get("Idempotency-Key") or str(data.get("idempotency_key") or "")).strip()[:128]
        body_hash = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
        if key:
            prior = StockDocument.objects.filter(company=company, idempotency_key=key).first()
            if prior is not None:
                return self._replay(prior, body_hash, request)

        kind = str(data.get("kind") or "").strip()
        if kind not in KINDS:
            return _err("kind_not_supported", f"kind: {', '.join(KINDS)}.", supported=list(KINDS))
        reason = str(data.get("reason_code") or "").strip()
        comment = str(data.get("comment") or "").strip()[:2000]
        if kind == "write_off" and not reason:
            return _err("reason_required", "Для списания нужна причина (reason_code).", fields={"reason_code": ["Обязательно."]})
        if reason and reason not in REASON_CODES:
            return _err("validation_error", "Неизвестная причина.", fields={"reason_code": [f"Допустимо: {', '.join(sorted(REASON_CODES))}."]})
        if reason == "other" and not comment:
            return _err("validation_error", "Для причины «Другое» нужен комментарий.", fields={"comment": ["Обязательно."]})

        now = timezone.now()
        occurred = now
        if data.get("occurred_at"):
            occurred = parse_datetime(str(data["occurred_at"]))
            if occurred is None:
                return _err("validation_error", "occurred_at: ISO-8601 с часовым поясом.", fields={"occurred_at": ["ISO-8601."]})
            if timezone.is_naive(occurred):
                occurred = timezone.make_aware(occurred)
            if occurred > now + timedelta(minutes=5):
                return _err("validation_error", "occurred_at в будущем.", fields={"occurred_at": ["В будущем."]})
            if occurred < now - OFFLINE_MAX:
                return _err("offline_too_old", "Движение старше 7 суток — оформите его вручную.", max_days=7)
        from apps.warehouse.services import get_closed_until

        closed = get_closed_until(company)
        if closed is not None and timezone.localdate(occurred) <= closed:
            return _err("period_closed", f"Период до {closed:%d.%m.%Y} закрыт.", 423, closed_until=closed.isoformat())

        raw_lines = data.get("lines") or data.get("items") or []
        if not isinstance(raw_lines, list) or not raw_lines:
            return _err("validation_error", "Нет строк.", fields={"lines": ["Хотя бы одна строка."]})
        confirm_large = data.get("confirm_large") in (True, "true", "1", 1)
        lines, fields = [], {}
        for i, ln in enumerate(raw_lines):
            if not isinstance(ln, dict) or not ln.get("product"):
                fields[f"lines[{i}].product"] = ["Обязательно."]
                continue
            try:
                q = Decimal(str(ln.get("qty")).replace(",", ".")).quantize(Q3)
            except (InvalidOperation, ValueError):
                fields[f"lines[{i}].qty"] = ["Число."]
                continue
            if kind in ("receive", "write_off") and q <= 0:
                fields[f"lines[{i}].qty"] = ["Больше нуля."]
                continue
            if kind == "count_fix" and q == 0:
                fields[f"lines[{i}].qty"] = ["Не ноль (со знаком: + или −)."]
                continue
            cost = None
            if ln.get("unit_cost") not in (None, ""):
                try:
                    cost = Decimal(str(ln["unit_cost"]).replace(",", ".")).quantize(Decimal("0.0001"))
                    if cost < 0:
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    fields[f"lines[{i}].unit_cost"] = ["Неотрицательное число."]
                    continue
            delta = -q if kind == "write_off" else q
            lines.append({"i": i, "product": str(ln["product"]), "delta": delta, "cost": cost,
                          "note": str(ln.get("note") or "")[:255]})
        if fields:
            return _err("validation_error", "Проверьте строки.", fields=fields)
        big = [ln["i"] for ln in lines if abs(ln["delta"]) > MAX_QTY]
        if big and not confirm_large:
            return _err("qty_suspicious", f"Количество больше {MAX_QTY}: проверьте или передайте confirm_large=true.", 422, indexes=big)

        allow_negative = data.get("allow_negative") in (True, "true", "1", 1)
        from apps.main.pos_serializers import _is_owner_like

        if allow_negative and not _is_owner_like(user):
            return _err("permission_denied", "Уводить остаток в минус может владелец или админ.", 403, permission="owner")

        try:
            with transaction.atomic():
                return self._create(request, company, kind, reason, comment, occurred, key, body_hash, lines, allow_negative)
        except IntegrityError:
            prior = StockDocument.objects.filter(company=company, idempotency_key=key).first() if key else None
            if prior is not None:
                return self._replay(prior, body_hash, request)
            raise

    def _replay(self, prior, body_hash, request):
        if prior.body_hash and prior.body_hash != body_hash:
            return _err("idempotency_conflict", "Этот ключ уже использован с другим телом.", 409, document=str(prior.id))
        return Response({**doc_payload(prior, show_cost=_show_cost(request.user)), "replayed": True}, status=200)

    def _create(self, request, company, kind, reason, comment, occurred, key, body_hash, lines, allow_negative):
        from apps.main.models import Product, StockDocument, StockDocumentLine

        ids = [ln["product"] for ln in lines]
        try:
            prods = {str(p.id): p for p in self._filter_qs_company_branch(Product.objects.select_for_update()).filter(id__in=ids)}
        except Exception:  # noqa: BLE001 — неверный uuid
            prods = {}
        missing = [ln["i"] for ln in lines if ln["product"] not in prods]
        if missing:
            return _err("validation_error", "Товар не найден.", fields={f"lines[{i}].product": ["Не найден."] for i in missing})
        need = {}
        for ln in lines:
            need[ln["product"]] = need.get(ln["product"], Decimal("0")) + ln["delta"]
        short = [{"index": ln["i"], "product": ln["product"], "available": _q3(prods[ln["product"]].quantity),
                  "requested": _q3(-ln["delta"])}
                 for ln in lines if ln["delta"] < 0
                 and Decimal(prods[ln["product"]].quantity or 0) + need[ln["product"]] < 0]
        if short and not allow_negative:
            return _err("insufficient_stock", "Не хватает остатка.", 409, lines=short)

        num = (StockDocument.objects.filter(company=company).aggregate(m=Max("number"))["m"] or 0) + 1
        doc = StockDocument.objects.create(
            company=company, branch=self._auto_branch(), number=num, kind=kind, reason_code=reason, comment=comment,
            occurred_at=occurred, idempotency_key=key, body_hash=body_hash, created_by=request.user)
        _mark_ledger(request, doc, occurred)
        total = Decimal("0")
        for ln in lines:
            p = prods[ln["product"]]
            Product.objects.filter(pk=p.pk).update(quantity=F("quantity") + ln["delta"])
            before = Decimal(p.quantity or 0)
            p.quantity = before + ln["delta"]
            cost = ln["cost"] if ln["cost"] is not None else (Decimal(p.purchase_price) if p.purchase_price is not None else None)
            if cost is not None:
                total += abs(ln["delta"]) * cost
            StockDocumentLine.objects.create(document=doc, product=p, name_snapshot=(p.name or "")[:255],
                                             qty_delta=ln["delta"], qty_before=before, qty_after=p.quantity,
                                             unit_cost=cost, note=ln["note"])
        doc.total_cost = total.quantize(Decimal("0.01"))
        doc.save(update_fields=["total_cost"])
        from apps.main.cache_utils import invalidate_cache_pattern

        invalidate_cache_pattern(f"products:list:{company.id}:")
        invalidate_cache_pattern(f"analytics:market:{company.id}:")
        return Response({**doc_payload(doc, show_cost=_show_cost(request.user)), "replayed": False}, status=201)


class StockDocumentDetailAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        from apps.main.models import StockDocument

        d = get_object_or_404(StockDocument.objects.prefetch_related("lines"), pk=pk, company=self._company())
        return Response(doc_payload(d, show_cost=_show_cost(request.user)))


class StockDocumentVoidAPIView(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk):
        from apps.main.models import Product, StockDocument
        from apps.main.pos_serializers import _is_owner_like

        if not _is_owner_like(request.user):
            return _err("permission_denied", "Аннулировать может владелец или админ.", 403, permission="owner")
        d = get_object_or_404(StockDocument.objects.select_for_update(), pk=pk, company=self._company())
        if d.status == StockDocument.Status.VOIDED:
            return Response({**doc_payload(d), "replayed": True})
        if d.created_at < timezone.now() - OFFLINE_MAX:
            return _err("void_window_expired", "Аннулировать можно в течение 7 суток.", 409)
        reason = str((request.data or {}).get("reason") or "").strip()[:255]
        if not reason:
            return _err("validation_error", "Укажите причину.", fields={"reason": ["Обязательно."]})
        try:
            request._ledger_doc = f"stockdoc-void:{d.id}"
        except AttributeError:
            pass
        for ln in d.lines.all():
            Product.objects.filter(pk=ln.product_id).update(quantity=F("quantity") - ln.qty_delta)
        d.status, d.voided_at, d.voided_by, d.void_reason = StockDocument.Status.VOIDED, timezone.now(), request.user, reason
        d.save(update_fields=["status", "voided_at", "voided_by", "void_reason"])
        return Response({**doc_payload(d, show_cost=_show_cost(request.user)), "replayed": False})
