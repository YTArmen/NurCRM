from django.utils import timezone
import uuid
from decimal import Decimal

import django_filters
from rest_framework import status, filters
from rest_framework.response import Response
from rest_framework import generics
from rest_framework.exceptions import ValidationError, PermissionDenied
from django.db import transaction, IntegrityError
from django.db.models import Sum, Q
from django_filters.rest_framework import DjangoFilterBackend

from apps.utils import _is_owner_like

from .views import CompanyBranchRestrictedMixin, filter_qs_company_branch_or_global
from . import models, serializers_money, services_money
from .services import error_payload, error_status


def _flag(request, name) -> bool:
    """Булев флаг из тела запроса или query string (true/1/yes)."""
    data = getattr(request, "data", None) or {}
    raw = data.get(name) if hasattr(data, "get") else None
    if raw is None:
        raw = request.query_params.get(name)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _post_money(request, doc):
    services_money.post_money_document(
        doc,
        user=request.user,
        allow_negative_cash=_flag(request, "allow_negative_cash"),
        allow_advance=_flag(request, "allow_advance"),
    )


class ServiceError(ValidationError):
    """DRF-исключение с телом/статусом ошибки сервиса (400 или 403 для прав)."""

    def __init__(self, exc):
        super().__init__(error_payload(exc))
        self.status_code = error_status(exc)


def _cash_registers_visible_qs(view):
    """
    Кассы, доступные пользователю: своя компания/филиал (как обычно), а для
    владельца с ?include_partners=1 — ещё и кассы компаний-партнёров
    (активное складское/кассовое партнёрство).
    """
    base = models.CashRegister.objects.select_related("company", "branch")
    qs = view._filter_qs_company_branch(base.all())

    user = view._user()
    request = view._request()
    raw = ""
    if request is not None:
        params = getattr(request, "query_params", None) or getattr(request, "GET", None)
        if params is not None:
            raw = str(params.get("include_partners", "")).strip().lower()
    include_partners = raw in ("1", "true", "yes", "on")

    if include_partners and _is_owner_like(user):
        company = view._company()
        partner_ids = [p.id for p in models.list_active_stock_partner_companies(company)]
        if partner_ids:
            qs = (qs | base.filter(company_id__in=partner_ids)).distinct()
    return qs


def _hidden_partner_register_ids(view, register_ids):
    """
    stock-partnership §7.14 (D8): сальдо и обороты кассы партнёра видны, только если партнёр
    разрешил забирать у него без подтверждения. → id чужих касс, чьи суммы надо скрыть.
    """
    company = view._company()
    if company is None:
        return set()
    foreign = dict(
        models.CashRegister.objects.filter(id__in=register_ids).exclude(company=company).values_list("id", "company_id")
    )
    hidden = set()
    allowed_cache = {}
    for rid, cid in foreign.items():
        if cid not in allowed_cache:
            p = models.get_stock_partnership(company.id, cid)
            allowed_cache[cid] = bool(p and p.allows_direct_pull_from(cid))
        if not allowed_cache[cid]:
            hidden.add(rid)
    return hidden


class MoneyDocumentFilter(django_filters.FilterSet):
    """Параметр ?agent= — агент контрагента (у MoneyDocument нет поля agent)."""

    agent = django_filters.UUIDFilter(field_name="counterparty__agent")

    class Meta:
        model = models.MoneyDocument
        fields = {
            "doc_type": ["exact"],
            "status": ["exact"],
            "cash_register": ["exact"],
            "warehouse": ["exact"],
            "counterparty": ["exact"],
            "payment_category": ["exact"],
            "payment_method": ["exact"],
        }


class CashRegisterListCreateView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = serializers_money.CashRegisterSerializer
    queryset = models.CashRegister.objects.select_related("company", "branch").order_by("name")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["company", "branch"]
    search_fields = ["name", "location"]

    def get_queryset(self):
        qs = _cash_registers_visible_qs(self).order_by("name")
        wh_raw = (self.request.query_params.get("for_warehouse") or "").strip()
        if wh_raw:
            qs = self._for_warehouse(qs, wh_raw)
        return qs

    def _for_warehouse(self, qs, wh_raw):
        """
        ?for_warehouse=<uuid> — кассы, которые подойдут для денег документа этого склада
        (как при проведении: кассы филиала склада, иначе кассы компании без филиала).
        Пустой список — фронт заранее предупреждает «Создайте кассу».
        """
        import uuid as _uuid

        try:
            wh_id = _uuid.UUID(wh_raw)
        except ValueError:
            raise ValidationError({"for_warehouse": "Неверный UUID."})
        wh = self._filter_qs_company_branch(models.Warehouse.objects.all()).filter(id=wh_id).first()
        if wh is None:
            raise ValidationError({"for_warehouse": "Склад не найден или недоступен."})
        qs = qs.filter(company_id=wh.company_id)
        if wh.branch_id is not None:
            branch_qs = qs.filter(branch_id=wh.branch_id)
            if branch_qs.exists():
                return branch_qs
        return qs.filter(branch__isnull=True)

    def perform_create(self, serializer):
        company = self._company()
        branch = self._auto_branch()
        if not company:
            # на всякий случай (не должно происходить при IsAuthenticated)
            raise ValidationError({"company": "Обязательное поле."})
        serializer.save(company=company, branch=branch)

    def _balances(self, register_ids, at=None):
        """{register_id: остаток} по проведённым документам (без миграционных); at — на конец даты."""
        qs = services_money.cash_balance_qs().filter(cash_register_id__in=register_ids)
        if at is not None:
            qs = qs.filter(date__date__lte=at)
        out = {}
        for row in qs.values("cash_register_id").annotate(
            receipts=Sum("amount", filter=Q(doc_type=models.MoneyDocument.DocType.MONEY_RECEIPT), default=Decimal("0.00")),
            expenses=Sum("amount", filter=Q(doc_type=models.MoneyDocument.DocType.MONEY_EXPENSE), default=Decimal("0.00")),
        ):
            out[row["cash_register_id"]] = Decimal(row["receipts"] or 0) - Decimal(row["expenses"] or 0)
        return out

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        qs = self.filter_queryset(self.get_queryset())
        register_ids = list(qs.values_list("pk", flat=True))
        hidden = _hidden_partner_register_ids(self, register_ids)
        register_ids = [rid for rid in register_ids if rid not in hidden]

        # QA B06: остаток каждой кассы (и на дату ?date=YYYY-MM-DD) — колонка «Остаток».
        from django.utils.dateparse import parse_date

        at = parse_date(request.query_params.get("date") or "") if request.query_params.get("date") else None
        balances = self._balances(register_ids) if register_ids else {}
        balances_at = self._balances(register_ids, at=at) if (register_ids and at) else {}
        rows = response.data.get("results") if isinstance(response.data, dict) else response.data
        for row in rows or []:
            try:
                rid = uuid.UUID(str(row.get("id")))
            except (ValueError, TypeError):
                continue
            if rid in hidden:
                row["balance"] = None
                if at:
                    row["balance_at"] = None
                continue
            row["balance"] = str(balances.get(rid, Decimal("0.00")).quantize(Decimal("0.01")))
            if at:
                row["balance_at"] = str(balances_at.get(rid, Decimal("0.00")).quantize(Decimal("0.01")))

        if register_ids:
            agg = services_money.cash_balance_qs().filter(
                cash_register_id__in=register_ids,
            ).aggregate(
                receipts_total=Sum(
                    "amount",
                    filter=Q(doc_type=models.MoneyDocument.DocType.MONEY_RECEIPT),
                    default=Decimal("0.00"),
                ),
                expenses_total=Sum(
                    "amount",
                    filter=Q(doc_type=models.MoneyDocument.DocType.MONEY_EXPENSE),
                    default=Decimal("0.00"),
                ),
            )
            response.data["receipts_total"] = str(agg["receipts_total"] or Decimal("0.00"))
            response.data["expenses_total"] = str(agg["expenses_total"] or Decimal("0.00"))
        else:
            response.data["receipts_total"] = "0.00"
            response.data["expenses_total"] = "0.00"
        return response


class CashRegisterDetailView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = serializers_money.CashRegisterSerializer
    queryset = models.CashRegister.objects.select_related("company", "branch")

    def get_queryset(self):
        return _cash_registers_visible_qs(self)

    def _ensure_own_company(self, instance):
        company = self._company()
        company_id = getattr(company, "id", None)
        if company_id and instance.company_id != company_id:
            raise PermissionDenied("Кассу компании-партнёра можно только просматривать.")

    def perform_update(self, serializer):
        self._ensure_own_company(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._ensure_own_company(instance)
        instance.delete()


class CashRegisterOperationsView(CompanyBranchRestrictedMixin, generics.RetrieveAPIView):
    """
    Детали кассы с балансом, приходами и расходами.
    GET /api/warehouse/cash-registers/{id}/operations/
    """

    queryset = models.CashRegister.objects.select_related("company", "branch")

    def get_queryset(self):
        return _cash_registers_visible_qs(self)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.id in _hidden_partner_register_ids(self, [instance.id]):
            data = serializers_money.CashRegisterSerializer(instance).data
            data.update({"balance": None, "receipts": [], "expenses": [], "receipts_total": None, "expenses_total": None})
            return Response(data)
        docs = list(
            services_money.cash_balance_qs().filter(
                cash_register=instance,
            ).select_related("cash_register", "counterparty", "payment_category").order_by("-date")
        )
        receipts = []
        expenses = []
        receipts_sum = Decimal("0.00")
        expenses_sum = Decimal("0.00")
        for d in docs:
            data = serializers_money.MoneyDocumentSerializer(d).data
            if d.doc_type == models.MoneyDocument.DocType.MONEY_RECEIPT:
                receipts.append(data)
                receipts_sum += Decimal(d.amount or 0)
            else:
                expenses.append(data)
                expenses_sum += Decimal(d.amount or 0)
        balance = receipts_sum - expenses_sum

        data = serializers_money.CashRegisterSerializer(instance).data
        data["balance"] = str(balance)
        data["receipts"] = receipts
        data["expenses"] = expenses
        data["receipts_total"] = str(receipts_sum)
        data["expenses_total"] = str(expenses_sum)

        return Response(data)


class PaymentCategoryListCreateView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = serializers_money.PaymentCategorySerializer
    queryset = models.PaymentCategory.objects.all().order_by("title")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["company", "branch"]
    search_fields = ["title"]

    def get_queryset(self):
        # QA B13: системные категории — общие на компанию (branch=NULL), их видят и филиалы;
        # старые копии системных категорий по филиалам скрыты (сливаются командой
        # merge_system_payment_categories).
        qs = filter_qs_company_branch_or_global(self, self.queryset.all())
        return qs.exclude(system_code__isnull=False, branch__isnull=False)

    def perform_create(self, serializer):
        try:
            self._save_with_company_branch(serializer)
        except IntegrityError as e:
            msg = str(e)
            if (
                "uq_wh_payment_category_title_per_branch" in msg
                or "uq_wh_payment_category_title_global_per_company" in msg
            ):
                raise ValidationError({"title": "Категория платежа с таким названием уже существует."})
            if "uq_wh_payment_category_system_code_per_scope" in msg:
                raise ValidationError({"detail": "Системная категория для этой области уже существует."})
            raise


class PaymentCategoryDetailView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = serializers_money.PaymentCategorySerializer
    queryset = models.PaymentCategory.objects.all()

    def get_queryset(self):
        return filter_qs_company_branch_or_global(self, self.queryset.all())

    def perform_update(self, serializer):
        if serializer.instance.system_code:
            raise ValidationError({"detail": "Системную категорию платежа нельзя изменять.", "code": "category_system"})
        try:
            self._save_with_company_branch(serializer)
        except IntegrityError as e:
            msg = str(e)
            if (
                "uq_wh_payment_category_title_per_branch" in msg
                or "uq_wh_payment_category_title_global_per_company" in msg
            ):
                raise ValidationError({"title": "Категория платежа с таким названием уже существует."})
            raise

    def perform_destroy(self, instance):
        if instance.system_code:
            raise ValidationError({"detail": "Системную категорию платежа нельзя удалить.", "code": "category_system"})
        super().perform_destroy(instance)


class MoneyDocumentListCreateView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = serializers_money.MoneyDocumentSerializer
    queryset = models.MoneyDocument.objects.select_related(
        "cash_register", "warehouse", "counterparty", "payment_category", "company", "branch"
    ).order_by("-date")
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_class = MoneyDocumentFilter
    search_fields = ["number", "comment", "counterparty__name"]

    def get_queryset(self):
        return filter_qs_company_branch_or_global(self, self.queryset.all())

    @staticmethod
    def _wants_post(request) -> bool:
        """
        Backward compatible:
          - if client sends {"status": "POSTED"} on create/update, auto-post it
          - or {"post": true}
          - or ?post=1
        """
        data = getattr(request, "data", {}) or {}
        raw_status = data.get("status")
        raw_post = data.get("post")
        qp = getattr(request, "query_params", None)
        raw_qp = qp.get("post") if qp is not None else None

        if isinstance(raw_post, bool):
            return raw_post
        if isinstance(raw_post, str) and raw_post.strip().lower() in ("1", "true", "yes", "y"):
            return True
        if isinstance(raw_qp, str) and raw_qp.strip().lower() in ("1", "true", "yes", "y"):
            return True
        return str(raw_status).upper() == models.MoneyDocument.Status.POSTED

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        wants_post = self._wants_post(request)
        with transaction.atomic():
            # save with company/branch from mixin
            self._save_with_company_branch(serializer)
            doc = serializer.instance
            if wants_post:
                try:
                    _post_money(request, doc)
                except Exception as e:
                    raise ServiceError(e)

        # refetch for consistent response
        doc = (
            models.MoneyDocument.objects
            .select_related("cash_register", "warehouse", "counterparty", "payment_category", "company", "branch")
            .get(pk=serializer.instance.pk)
        )
        out = self.get_serializer(doc)
        headers = self.get_success_headers(out.data)
        return Response(out.data, status=status.HTTP_201_CREATED, headers=headers)


class MoneyDocumentDetailView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = serializers_money.MoneyDocumentSerializer
    queryset = models.MoneyDocument.objects.select_related(
        "cash_register", "warehouse", "counterparty", "payment_category", "company", "branch"
    )

    def get_queryset(self):
        return filter_qs_company_branch_or_global(self, self.queryset.all())

    def perform_destroy(self, instance):
        # Удалять можно только черновики и отказанные; проведённый — сначала unpost/reject.
        if instance.status == models.MoneyDocument.Status.POSTED:
            raise ValidationError(
                {"detail": "Нельзя удалить проведённый документ. Сначала отмените проведение или откажите."}
            )
        instance.delete()

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        wants_post = MoneyDocumentListCreateView._wants_post(request)
        with transaction.atomic():
            self.perform_update(serializer)
            if wants_post and serializer.instance.status != models.MoneyDocument.Status.POSTED:
                try:
                    _post_money(request, serializer.instance)
                except Exception as e:
                    raise ServiceError(e)

        instance = (
            models.MoneyDocument.objects
            .select_related("cash_register", "warehouse", "counterparty", "payment_category", "company", "branch")
            .get(pk=serializer.instance.pk)
        )
        return Response(self.get_serializer(instance).data, status=status.HTTP_200_OK)


class MoneyDocumentPostView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    serializer_class = serializers_money.MoneyDocumentSerializer

    def get_queryset(self):
        qs = models.MoneyDocument.objects.select_related(
            "cash_register", "warehouse", "counterparty", "payment_category", "company", "branch"
        )
        return filter_qs_company_branch_or_global(self, qs)

    def post(self, request, pk=None):
        doc = self.get_object()
        try:
            _post_money(request, doc)
        except Exception as e:
            return Response(error_payload(e), status=error_status(e))
        return Response(self.get_serializer(doc).data)


class MoneyDocumentUnpostView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    serializer_class = serializers_money.MoneyDocumentSerializer

    def get_queryset(self):
        qs = models.MoneyDocument.objects.select_related(
            "cash_register", "warehouse", "counterparty", "payment_category", "company", "branch"
        )
        return filter_qs_company_branch_or_global(self, qs)

    def post(self, request, pk=None):
        doc = self.get_object()
        try:
            services_money.unpost_money_document(doc, user=request.user)
        except Exception as e:
            return Response(error_payload(e), status=error_status(e))
        return Response(self.get_serializer(doc).data)


class MoneyDocumentRejectView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    """POST {id}/reject/ — отказ проведённого документа (POSTED → REJECTED), откат кассы."""

    serializer_class = serializers_money.MoneyDocumentSerializer

    def get_queryset(self):
        qs = models.MoneyDocument.objects.select_related(
            "cash_register", "warehouse", "counterparty", "payment_category", "company", "branch"
        )
        return filter_qs_company_branch_or_global(self, qs)

    def post(self, request, pk=None):
        doc = self.get_object()
        try:
            services_money.reject_money_document(doc)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(doc).data)


class CounterpartyMoneyOperationsView(CompanyBranchRestrictedMixin, generics.ListAPIView):
    """
    Операции по контрагенту: только проведённые денежные документы (без черновиков);
    с ?include_debts=1 или ?include_documents=1 — ещё и складские продажи/покупки/возвраты
    (POSTED и CASH_PENDING: наличные и в долг).
    """

    serializer_class = serializers_money.MoneyDocumentSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["doc_type", "status", "cash_register", "warehouse", "payment_category", "payment_method"]
    search_fields = ["number", "comment"]

    def get_queryset(self):
        counterparty_id = self.kwargs.get("counterparty_id")
        qs = models.MoneyDocument.objects.select_related(
            "cash_register", "warehouse", "counterparty", "payment_category", "company", "branch"
        ).filter(
            counterparty_id=counterparty_id,
            status=models.MoneyDocument.Status.POSTED,
        ).order_by("-date")
        return filter_qs_company_branch_or_global(self, qs)

    @staticmethod
    def _truthy(v) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

    @staticmethod
    def _dec_q2(x) -> Decimal:
        v = x if x is not None else Decimal("0")
        return Decimal(v).quantize(Decimal("0.01"))

    def _counterparty_mini_analytics(self, counterparty_id):
        """
        Сводка по контрагенту в том же company/branch scope, что и списки:
        продажи, движение кассы (проведённые деньги), сальдо взаиморасчёта (как в акте сверки).
        """
        cpid = services_money.norm_counterparty_id(counterparty_id)
        d = services_money.bulk_counterparty_mini_analytics(self, [cpid])
        return d.get(cpid) or services_money.empty_counterparty_mini_analytics()

    def list(self, request, *args, **kwargs):
        """
        - default: список проведённых MoneyDocument по контрагенту (черновики не включаются).
        - if ?include_debts=1 or ?include_documents=1: те же деньги + складские товарные документы
          (POSTED и CASH_PENDING) + объединённая лента и analytics.
        """
        include_docs = self._truthy(request.query_params.get("include_debts")) or self._truthy(
            request.query_params.get("include_documents")
        )
        if not include_docs:
            return super().list(request, *args, **kwargs)

        # 1) money operations (respect existing filters/pagination/search)
        money_resp = super().list(request, *args, **kwargs)
        money_payload = money_resp.data

        # Flatten money results for merging
        if isinstance(money_payload, dict) and "results" in money_payload:
            money_items = list(money_payload.get("results") or [])
        else:
            money_items = list(money_payload or [])

        # 2) Складские товарные документы по контрагенту (как в акте сверки: все проведённые
        #    продажи/покупки/возвраты независимо от способа оплаты + ожидающие кассу).
        counterparty_id = self.kwargs.get("counterparty_id")
        trade_doc_types = (
            models.Document.DocType.SALE,
            models.Document.DocType.PURCHASE,
            models.Document.DocType.SALE_RETURN,
            models.Document.DocType.PURCHASE_RETURN,
        )
        doc_qs = (
            models.Document.objects.select_related(
                "warehouse_from", "counterparty", "payment_category", "cash_register"
            )
            .filter(
                counterparty_id=counterparty_id,
                status__in=(
                    models.Document.Status.POSTED,
                    models.Document.Status.CASH_PENDING,
                ),
                doc_type__in=trade_doc_types,
            )
            .order_by("-date")
        )
        doc_qs = self._filter_qs_company_branch(
            doc_qs,
            company_field="warehouse_from__company_id",
            branch_field="warehouse_from__branch",
        )

        def _doc_debt_delta(doc) -> Decimal:
            amt = Decimal(getattr(doc, "total", None) or 0).quantize(Decimal("0.01"))
            # Consistent with reconciliation: debit increases counterparty debt to company; credit decreases it.
            if doc.doc_type in (models.Document.DocType.SALE, models.Document.DocType.PURCHASE_RETURN):
                return amt
            return -amt

        debt_ops = []
        for d in doc_qs:
            amt = Decimal(getattr(d, "total", None) or 0).quantize(Decimal("0.01"))
            pc = getattr(d, "payment_category", None)
            cr = getattr(d, "cash_register", None)
            debt_ops.append(
                {
                    "source": "document",
                    "id": str(d.id),
                    "date": timezone.localtime(d.date).isoformat() if getattr(d, "date", None) else None,
                    "number": d.number,
                    "status": d.status,
                    "doc_type": d.doc_type,
                    "doc_type_display": d.get_doc_type_display(),
                    "payment_kind": d.payment_kind,
                    "payment_kind_display": d.get_payment_kind_display()
                    if getattr(d, "payment_kind", None)
                    else None,
                    "amount": str(amt),
                    "debt_delta": str(_doc_debt_delta(d)),
                    "comment": (d.comment or ""),
                    "cash_register": str(cr.id) if cr else None,
                    "cash_register_name": cr.name if cr else None,
                    "payment_category": str(pc.id) if pc else None,
                    "payment_category_title": pc.title if pc else None,
                }
            )

        def _money_debt_delta(item: dict) -> Decimal:
            amt = Decimal(str(item.get("amount") or "0")).quantize(Decimal("0.01"))
            if item.get("doc_type") == models.MoneyDocument.DocType.MONEY_EXPENSE:
                return amt
            return -amt

        money_type_labels = dict(models.MoneyDocument.DocType.choices)
        merged = []
        for it in money_items:
            md_t = it.get("doc_type")
            merged.append(
                {
                    "source": "money",
                    "id": str(it.get("id")),
                    "date": it.get("date"),
                    "number": it.get("number"),
                    "status": it.get("status"),
                    "doc_type": md_t,
                    "doc_type_display": money_type_labels.get(md_t, md_t),
                    "payment_kind": None,
                    "payment_kind_display": None,
                    "amount": str(it.get("amount")),
                    "debt_delta": str(_money_debt_delta(it)),
                    "comment": it.get("comment") or "",
                    "cash_register": it.get("cash_register"),
                    "cash_register_name": it.get("cash_register_name"),
                    "payment_category": it.get("payment_category"),
                    "payment_category_title": it.get("payment_category_title"),
                    "source_document": it.get("source_document"),
                }
            )
        merged.extend(debt_ops)
        merged.sort(key=lambda x: x.get("date") or "", reverse=True)

        analytics = self._counterparty_mini_analytics(counterparty_id)

        return Response(
            {
                "money": money_payload,
                "debt_operations": debt_ops,
                "operations": merged,
                "analytics": analytics,
            },
            status=money_resp.status_code,
            headers=getattr(money_resp, "headers", None),
        )


class WarehouseCashConfirmationSettingsView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    """
    Настройки подтверждения кассы склада:
    GET /warehouse/cash/confirmation-settings/ -> {"enabled": false}
    PATCH /warehouse/cash/confirmation-settings/ -> {"enabled": true|false}
    """
    serializer_class = serializers_money.WarehouseCashConfirmationSettingsSerializer

    def get_object(self):
        company = self._company()
        if not company:
            raise PermissionDenied("У пользователя не настроена компания.")
        settings, _ = models.WarehouseCashConfirmationSettings.objects.get_or_create(
            company=company,
            defaults={"enabled": False},
        )
        return settings

    def get(self, request, *args, **kwargs):
        settings = self.get_object()
        return Response(self.get_serializer(settings).data)

    def patch(self, request, *args, **kwargs):
        if not _is_owner_like(request.user):
            raise PermissionDenied("Изменять настройки кассы может только владелец или администратор.")
        settings = self.get_object()
        serializer = self.get_serializer(settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def put(self, request, *args, **kwargs):
        return self.patch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.patch(request, *args, **kwargs)




class WarehousePeriodCloseView(CompanyBranchRestrictedMixin, generics.GenericAPIView):
    """
    Закрытие периода (QA B07).
    GET   /api/warehouse/settings/period-close/ → {"closed_until": "2026-09-30" | null}
    PATCH /api/warehouse/settings/period-close/ {"closed_until": "2026-09-30" | null} — только владелец.

    Документы с датой <= closed_until нельзя проводить, отменять, редактировать и удалять
    без права can_post_closed_period.
    """

    serializer_class = serializers_money.WarehousePeriodCloseSerializer

    def _settings(self):
        company = self._company()
        if company is None:
            raise ValidationError({"company": "Компания не найдена."})
        obj, _ = models.WarehouseAccountingSettings.objects.get_or_create(company=company)
        return obj

    def get(self, request, *args, **kwargs):
        return Response(self.get_serializer(self._settings()).data)

    def patch(self, request, *args, **kwargs):
        from .op_permissions import is_company_owner

        if not is_company_owner(request.user):
            raise PermissionDenied({"detail": "Закрывать период может только владелец.", "code": "permission_period_close"})
        obj = self._settings()
        ser = self.get_serializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        old_closed = obj.closed_until
        ser.save(updated_by=request.user)
        if obj.closed_until != old_closed:
            from apps.construction.audit import log_change
            log_change(
                company=obj.company, user=request.user, action="closed_until", object_type="period",
                object_id=obj.company_id, object_name="Закрытие периода",
                before={"closed_until": str(old_closed) if old_closed else None},
                after={"closed_until": str(obj.closed_until) if obj.closed_until else None}, request=request,
            )
        return Response(ser.data)

    def put(self, request, *args, **kwargs):
        return self.patch(request, *args, **kwargs)
