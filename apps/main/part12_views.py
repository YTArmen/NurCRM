"""
ТЗ для бэкенда NurCRM — часть 12 (итог проверки 05.10.2026): новые адреса.
"""
from collections import defaultdict

from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.kassa_views import _company
from apps.main.models import Product, ProductAlternateBarcode
from core.exceptions import ApiError, api_exception_handler
from apps.users.permissions_flags import PosAccess


class BarcodeDuplicatesAPIView(APIView):
    """
    GET /api/main/products/barcode-duplicates/   (ТЗ ч.12, 2.6)

    Старые дубли: один штрихкод у нескольких товаров компании (основной или доп. код).
    Новые сервер уже не даёт создать; этот список — чтобы владелец удалил или объединил старые.
    → [{"barcode": "…", "products": [{"id", "name", "quantity", "created_at", "branch", "is_alternate"}]}]
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # Расширено для «Калькуляции» (calculator-after-stress-test/03): у товара есть code,
        # status, branch_name; у группы — same_scope (true — дубль в одном филиале/основном
        # каталоге, нужно объединить; false — копии в разных филиалах после перемещения).
        # ?same_scope_only=true — только настоящие дубли.
        from apps.main.duplicate_barcodes import find_duplicate_barcodes

        company = _company(request)
        same_scope_only = str(request.query_params.get("same_scope_only") or "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        return Response(find_duplicate_barcodes(
            Product.objects.filter(company=company), same_scope_only=same_scope_only,
        ))


class _BatchItemRequest:
    """Запрос одной продажи из пакета: свои тело и ключ, остальное — от исходного запроса."""

    def __init__(self, request, data, key):
        self._request_obj = request
        self.data = data
        self.headers = {**dict(request.headers), "Idempotency-Key": key}

    def __getattr__(self, name):
        return getattr(self._request_obj, name)


class PosCheckoutBatchAPIView(APIView):
    """
    POST /api/main/pos/checkout/batch/   (ТЗ ч.2, BE2-12; ч.12, 3.6)
    { "items": [ { "idempotency_key": "…", "body": { …как pos/checkout… } }, … ] }   — до 50
    200 → { "results": [ { "idempotency_key": "…", "status": 201, "sale": {…} },
                         { "idempotency_key": "…", "status": 400, "detail": … } ] }
    Каждая продажа проводится отдельно (своя транзакция): ошибка одной не отменяет остальные.
    Повтор с тем же ключом — status 200 и уже созданная продажа.
    """

    permission_classes = [permissions.IsAuthenticated, PosAccess]  # ТЗ Н-16
    MAX_ITEMS = 50

    def post(self, request):
        from django.http import Http404
        from rest_framework.exceptions import APIException, ValidationError

        # ТЗ Н-15: тот же класс, что у pos/checkout/ (упаковки sale_package_id, лимиты долга, график, ответ с остатками)
        from apps.possync.views import PosQuickCheckoutGuardedAPIView as PosQuickCheckoutAPIView

        items = request.data.get("items") if isinstance(request.data, dict) else None
        if not isinstance(items, list) or not items:
            raise ValidationError({"items": "Передайте непустой список продаж."})
        if len(items) > self.MAX_ITEMS:
            raise ValidationError({"items": f"Не больше {self.MAX_ITEMS} продаж за запрос."})

        view = PosQuickCheckoutAPIView()
        view.request = request  # ТЗ 26.10: крючки строк (шаг Б-52, цены Б-55) видят заголовок клиента
        results = []
        for raw in items:
            key = str((raw or {}).get("idempotency_key") or "").strip() if isinstance(raw, dict) else ""
            body = raw.get("body") if isinstance(raw, dict) else None
            if not key or not isinstance(body, dict):
                results.append({"idempotency_key": key or None, "status": 400,
                                "detail": {"detail": "Нужны idempotency_key и body."}})
                continue
            try:
                resp = view.post(_BatchItemRequest(request, body, key))
                row = {"idempotency_key": key, "status": resp.status_code}
                if resp.status_code < 400:
                    row["sale"] = resp.data
                else:
                    row["detail"] = resp.data
            except ValidationError as e:
                row = {"idempotency_key": key, "status": 400, "detail": e.detail}
            except Http404:
                row = {"idempotency_key": key, "status": 404, "detail": {"detail": "Не найдено."}}
            except APIException as e:
                detail = e.detail
                if isinstance(e, ApiError):
                    # ТЗ Н-15: code/errors[]/total_mismatch и т.п. — как в обычном ответе
                    detail = api_exception_handler(e, {}).data
                row = {"idempotency_key": key, "status": e.status_code, "detail": detail}
            results.append(row)
        return Response({"results": results})


def _finance_view_base():
    from apps.main.analytics_market import AnalyticsView
    return AnalyticsView


class AnalyticsFinanceAPIView(_finance_view_base()):
    """
    GET /api/main/analytics/finance/?date_from=&date_to=&branch=&operations=1&limit=1000   (ч.4 AN-05; ч.12, 3.1)

    Доход без задвоения: одна продажа — один доход на её сумму (те же продажи и та же сумма,
    что «выручка» в analytics/market/?tab=sales); способ оплаты — разбивка этого дохода.
    Оплата долга — приход денег (debt_repayments), а не новая выручка.
    → { "income_total", "income_by_method": {"cash": …, "mixed": …}, "sales_count",
        "debt_repayments": {"total", "by_method"},
        "operations": [ {"transaction_id", "source_type": "SALE", "source_id", "operation_type": "income",
                         "payment_method", "amount", "date"} ] }
    """

    def get(self, request):
        from django.db.models import Count, Q, Sum

        from apps.construction.models import CashFlow
        from apps.main.analytics_market import (
            Z_MONEY, _get_active_branch, _get_company, _get_period, _money,
        )
        from apps.main.models import Sale

        company = _get_company(request.user)
        if not company:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("У пользователя не настроена компания.")
        branch = _get_active_branch(request)
        period = _get_period(request, strict=True)

        qs = Sale.objects.filter(
            company=company,
            status__in=(Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED),
            paid_at__gte=period.start,
            paid_at__lt=period.end,
        )
        if branch is not None:
            qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True)) if self._include_global(request) else qs.filter(branch=branch)
        qs = self._apply_sale_filters(request, qs, Sale)

        by_method = {}
        income_total = Z_MONEY
        sales_count = 0
        for row in qs.values("payment_method").annotate(s=Sum("total"), n=Count("id")).order_by("payment_method"):
            amt = _money(row["s"] or Z_MONEY)
            by_method[row["payment_method"] or "other"] = str(amt)
            income_total += amt
            sales_count += row["n"]

        cf = CashFlow.objects.filter(
            company=company,
            status=CashFlow.Status.APPROVED,
            type=CashFlow.Type.INCOME,
            source_kind=CashFlow.SourceKind.DEBT_REPAYMENT,
            created_at__gte=period.start,
            created_at__lt=period.end,
        )
        if branch is not None:
            cf = cf.filter(Q(branch=branch) | Q(branch__isnull=True))
        debt_by_method = {}
        debt_total = Z_MONEY
        for row in cf.values("payment_method").annotate(s=Sum("amount")).order_by("payment_method"):
            amt = _money(row["s"] or Z_MONEY)
            debt_by_method[row["payment_method"] or "cash"] = str(amt)
            debt_total += amt

        data = {
            "period": {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
            },
            "income_total": str(_money(income_total)),
            "income_by_method": by_method,
            "sales_count": sales_count,
            "debt_repayments": {"total": str(_money(debt_total)), "by_method": debt_by_method},
        }
        if (request.query_params.get("operations") or "1") not in ("0", "false", "no"):
            try:
                limit = max(1, min(int(request.query_params.get("limit") or 1000), 5000))
            except ValueError:
                limit = 1000
            ops = qs.order_by("-paid_at").values("id", "payment_method", "total", "paid_at")[:limit]
            data["operations"] = [
                {
                    "transaction_id": str(r["id"]),
                    "source_type": "SALE",
                    "source_id": str(r["id"]),
                    "operation_type": "income",
                    "payment_method": r["payment_method"],
                    "amount": str(_money(r["total"] or Z_MONEY)),
                    "date": r["paid_at"].isoformat() if r["paid_at"] else None,
                }
                for r in ops
            ]
            data["operations_truncated"] = sales_count > limit
        return Response(data)


class AnalyticsMarketSummaryAPIView(_finance_view_base()):
    """
    GET /api/main/analytics/market/summary/?periods=today,yesterday,week,prev_week,month   (ч.2 BE2-18; ч.12, 3.7)

    Карточки продаж (как analytics/market/?tab=sales) сразу для нескольких периодов.
    Периоды: today, yesterday, week (с понедельника по сегодня), prev_week, month (с 1-го числа),
    prev_month, last7, last30, или явный «YYYY-MM-DD..YYYY-MM-DD». Фильтры branch/cashbox/... — как у tab=sales.
    → { "periods": { "today": { "date_from": "…", "date_to": "…", "cards": {…} }, … } }
    """

    MAX_PERIODS = 12

    @staticmethod
    def _bounds(name, today):
        from datetime import timedelta

        from django.utils.dateparse import parse_date

        monday = today - timedelta(days=today.weekday())
        first = today.replace(day=1)
        simple = {
            "today": (today, today),
            "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
            "week": (monday, today),
            "prev_week": (monday - timedelta(days=7), monday - timedelta(days=1)),
            "month": (first, today),
            "prev_month": ((first - timedelta(days=1)).replace(day=1), first - timedelta(days=1)),
            "last7": (today - timedelta(days=6), today),
            "last30": (today - timedelta(days=29), today),
        }
        if name in simple:
            return simple[name]
        if ".." in name:
            a, b = name.split("..", 1)
            d1, d2 = parse_date(a.strip()), parse_date(b.strip())
            if d1 and d2 and d1 <= d2:
                return d1, d2
        return None

    def get(self, request):
        from datetime import datetime, time, timedelta

        from django.utils import timezone
        from rest_framework.exceptions import PermissionDenied, ValidationError

        from apps.main.analytics_market import Period, _get_active_branch, _get_company

        company = _get_company(request.user)
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")
        branch = _get_active_branch(request)

        names = [p.strip() for p in (request.query_params.get("periods") or "").split(",") if p.strip()]
        if not names:
            raise ValidationError({"periods": "Укажите периоды, например periods=today,yesterday,week,prev_week,month."})
        if len(names) > self.MAX_PERIODS:
            raise ValidationError({"periods": f"Не больше {self.MAX_PERIODS} периодов."})

        tz = timezone.get_current_timezone()
        today = timezone.localdate()
        result = {}
        for name in names:
            b = self._bounds(name, today)
            if b is None:
                raise ValidationError({"periods": f"Неизвестный период: {name}."})
            d1, d2 = b
            start = timezone.make_aware(datetime.combine(d1, time.min), tz)
            end = timezone.make_aware(datetime.combine(d2 + timedelta(days=1), time.min), tz)
            data = self._sales(request, company, branch, Period(start=start, end=end))
            result[name] = {"date_from": d1.isoformat(), "date_to": d2.isoformat(), "cards": data.get("cards", {})}
        return Response({"periods": result})
