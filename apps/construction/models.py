from decimal import Decimal, ROUND_HALF_UP
import uuid

from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Sum, Count, Q, DecimalField, Case, When, Value
from django.utils import timezone

from apps.users.models import Company, Branch


class CashFlowCategory(models.Model):
    """Пользовательские категории движений по кассе (компания / филиал)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="cashflow_categories",
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="cashflow_categories",
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Филиал",
    )
    title = models.CharField(max_length=128, verbose_name="Название")
    # ТЗ «Бухгалтер», п. 3: статья движения денег.
    class Kind(models.TextChoices):
        INCOME = "income", "Доход"
        EXPENSE = "expense", "Расход"
        BOTH = "both", "Любой"

    type = models.CharField(max_length=10, choices=Kind.choices, default=Kind.BOTH, db_default=Kind.BOTH, verbose_name="Тип статьи")
    archived = models.BooleanField(default=False, db_default=False, verbose_name="В архиве")
    code = models.CharField(max_length=32, blank=True, default="", db_default="", verbose_name="Код статьи по умолчанию")
    # ТЗ Н-19: системный slug (salaries/rent/taxes/utilities/purchases) и признак «системная — не удаляется».
    slug = models.CharField(max_length=32, blank=True, default="", db_default="", db_index=True, verbose_name="Системный slug")
    is_system = models.BooleanField(default=False, db_default=False, verbose_name="Системная статья")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")

    class Meta:
        verbose_name = "Категория движения по кассе"
        verbose_name_plural = "Категории движений по кассе"
        ordering = ["title"]
        constraints = [
            models.UniqueConstraint(
                fields=("company", "branch", "title"),
                name="uq_cashflow_category_title_per_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["company", "branch", "created_at"]),
        ]

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class Cashbox(models.Model):
    class CashboxRole(models.TextChoices):
        POS_MAIN = "pos_main", "Основная касса (POS)"
        POS_BRANCH = "pos_branch", "Касса филиала (POS)"
        EXPENSE_VARIABLE = "expense_variable", "Переменные расходы"
        EXPENSE_FIXED = "expense_fixed", "Постоянные расходы"
        DEBT = "debt", "Для долгов"
        SAFE = "safe", "Сейф"  # ТЗ Н-33: не ящик кассы, наличные в смене не считает
        BANK = "bank", "Расчётный счёт"  # ТЗ Н-33

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="cash_cashboxes",
        verbose_name="Компания",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="cash_cashboxes",
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Филиал",
    )

    name = models.CharField(max_length=255, blank=True, null=True, verbose_name="Название кассы")
    role = models.CharField(
        max_length=32,
        choices=CashboxRole.choices,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Роль кассы",
    )
    # ⛔ лучше без null=True на boolean, но я оставлю как есть, чтобы не ломать миграции
    is_consumption = models.BooleanField(verbose_name="Расход", default=False, blank=True, null=True)

    is_active = models.BooleanField(verbose_name="Активна", default=True, db_index=True)
    archived_at = models.DateTimeField(verbose_name="Дата архивации", null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archived_cashboxes",
        verbose_name="Кто архивировал",
    )
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merged_from",
        verbose_name="Объединена с кассой",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True, null=True, blank=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True, verbose_name="Дата обновления")

    class Meta:
        verbose_name = "Касса"
        verbose_name_plural = "Кассы"
        indexes = [
            models.Index(fields=["company"]),
            models.Index(fields=["company", "branch"]),
            models.Index(fields=["company", "role"]),
            models.Index(fields=["company", "role", "branch"]),
            models.Index(fields=["company", "is_active"]),
            models.Index(fields=["company", "role", "is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("branch", "name"),
                name="uq_cashbox_name_per_branch",
                condition=Q(branch__isnull=False) & Q(name__isnull=False),
            ),
            models.UniqueConstraint(
                fields=("company", "name"),
                name="uq_cashbox_name_global_per_company",
                condition=Q(branch__isnull=True) & Q(name__isnull=False),
            ),
        ]

    def get_inferred_role(self) -> str:
        if self.role:
            return self.role
        n = (self.name or "").lower().strip()
        if "филиал" in n or self.branch_id:
            return self.CashboxRole.POS_BRANCH
        if "переменн" in n:
            return self.CashboxRole.EXPENSE_VARIABLE
        if "постоянн" in n:
            return self.CashboxRole.EXPENSE_FIXED
        if "основн" in n:
            return self.CashboxRole.POS_MAIN
        return self.CashboxRole.POS_MAIN

    def drawer_cash_info(self):
        """
        ТЗ Н-06: наличные в ящике кассы сейчас. Только для pos_branch/pos_main, иначе None.
        Открыта смена — её expected_cash; иначе пересчитанная сумма (closing_cash) последней
        закрытой смены + наличные приходы − расходы вне смены (shift пуст) после её закрытия.
        Возвращает dict {drawer_cash, drawer_cash_at, last_closed_shift} или None.
        """
        if self.get_inferred_role() not in (self.CashboxRole.POS_BRANCH, self.CashboxRole.POS_MAIN):
            return None
        z = Decimal("0.00")
        q = lambda v: (v or z).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # noqa: E731
        shifts = CashShift.objects.filter(cashbox=self)
        last_closed = shifts.filter(status=CashShift.Status.CLOSED).order_by("-closed_at", "-opened_at").first()
        last_id = str(last_closed.id) if last_closed else None
        open_shift = shifts.filter(status=CashShift.Status.OPEN).order_by("-opened_at").first()
        now = timezone.now()
        if open_shift is not None:
            cash = open_shift.calc_live_totals(refresh=True).get("expected_cash", z)
        elif last_closed is not None:
            base = last_closed.closing_cash
            if base is None:
                base = last_closed.calc_live_totals().get("expected_cash", z)
            out = (
                CashFlow.objects.money()
                .filter(cashbox=self, shift__isnull=True, created_at__gt=last_closed.closed_at)
                .filter(Q(payment_method__isnull=True) | Q(payment_method="") | Q(payment_method="cash"))
            )
            agg = out.aggregate(
                inc=Sum("amount", filter=Q(type=CashFlow.Type.INCOME)),
                exp=Sum("amount", filter=Q(type=CashFlow.Type.EXPENSE)),
            )
            cash = base + (agg["inc"] or z) - (agg["exp"] or z)
        else:
            return {"drawer_cash": None, "drawer_cash_at": now, "last_closed_shift": None}
        return {"drawer_cash": q(cash), "drawer_cash_at": now, "last_closed_shift": last_id}

    def clean(self):
        if self.branch_id and self.branch.company_id != self.company_id:
            raise ValidationError({"branch": "Филиал принадлежит другой компании."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def get_summary(self) -> dict:
        z = Decimal("0.00")

        # flows (approved, без строк заявок на правку/отмену)
        flows_qs = self.flows.money()
        fa = flows_qs.aggregate(
            income=Sum("amount", filter=Q(type=CashFlow.Type.INCOME)),
            expense=Sum("amount", filter=Q(type=CashFlow.Type.EXPENSE)),
        )
        income_total = fa["income"] or z
        expense_total = fa["expense"] or z

        # sales (paid)
        Sale = self.sales.model
        sales_qs = self.sales.filter(status=Sale.Status.PAID)

        sa = sales_qs.aggregate(
            cnt=Count("id"),
            total_sum=Sum("total"),
            cash_sum=Sum(
                Case(
                    When(payment_method=Sale.PaymentMethod.CASH, then="total"),
                    default=Value(0),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            ),
            noncash_sum=Sum(
                Case(
                    When(~Q(payment_method=Sale.PaymentMethod.CASH), then="total"),
                    default=Value(0),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            ),
        )

        sales_count = sa["cnt"] or 0
        sales_total = sa["total_sum"] or z
        cash_sales_total = sa["cash_sum"] or z
        noncash_sales_total = sa["noncash_sum"] or z

        # ✅ open shifts (их может быть несколько!)
        open_shifts = (
            self.shifts
            .filter(status=CashShift.Status.OPEN)
            .select_related("cashier")
            .only("id", "opening_cash", "cashier_id", "status", "opened_at")
            .order_by("-opened_at")
        )

        open_shifts_info = []
        open_shifts_expected_cash_total = None

        if open_shifts.exists():
            total_expected = Decimal("0.00")
            for sh in open_shifts:
                expected = None
                try:
                    expected = sh.calc_live_totals().get("expected_cash")
                    if expected is not None:
                        total_expected += expected
                except Exception:
                    expected = None

                open_shifts_info.append({
                    "id": str(sh.id),
                    "cashier_id": str(sh.cashier_id) if sh.cashier_id else None,
                    "opened_at": sh.opened_at.isoformat() if sh.opened_at else None,
                    "expected_cash": str(expected) if expected is not None else None,
                })

            open_shifts_expected_cash_total = str(total_expected)

        return {
            "income_total": income_total,
            "expense_total": expense_total,
            "sales_count": sales_count,
            "sales_total": sales_total,
            "cash_sales_total": cash_sales_total,
            "noncash_sales_total": noncash_sales_total,

            # было: open_shift_expected_cash (одна смена)
            # стало: список смен + сумма expected_cash
            "open_shifts": open_shifts_info,
            "open_shifts_expected_cash_total": open_shifts_expected_cash_total,
        }

    def __str__(self):
        if self.branch_id:
            base = f"Касса филиала {self.branch.name}"
            return f"{base}{f' ({self.name})' if self.name else ''}"
        return self.name or f"Касса компании {self.company.name}"


class CashShift(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Открыта"
        CLOSED = "closed", "Закрыта"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="shifts", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shifts",
        db_index=True,
        verbose_name="Филиал",
    )

    cashbox = models.ForeignKey("construction.Cashbox", on_delete=models.PROTECT, related_name="shifts", verbose_name="Касса")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="shifts", verbose_name="Кассир")

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True, verbose_name="Статус")
    opened_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата открытия")
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата закрытия")
    close_reason = models.CharField(
        max_length=64, blank=True, null=True, default="",
        verbose_name="Причина закрытия",
        help_text="Пусто — обычное закрытие; например employee_deleted — автозакрытие при удалении кассира.",
    )

    opening_cash = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Начальная сумма")
    closing_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Конечная сумма")

    income_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Итого приходов")
    expense_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Итого расходов")
    sales_count = models.PositiveIntegerField(default=0, verbose_name="Количество продаж")
    sales_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Итого продаж")
    cash_sales_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Наличные продажи")
    noncash_sales_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), verbose_name="Безналичные продажи")

    class Meta:
        verbose_name = "Кассовая смена"
        verbose_name_plural = "Кассовые смены"
        constraints = [
            # ✅ теперь можно много OPEN на 1 cashbox,
            # но нельзя 2 OPEN смены одному кассиру на одной кассе
            models.UniqueConstraint(
                fields=("cashbox", "cashier"),
                condition=Q(status="open"),
                name="uq_open_shift_per_cashbox_cashier",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "branch", "opened_at"]),
            models.Index(fields=["cashbox", "opened_at"]),
            models.Index(fields=["cashier", "opened_at"]),
            models.Index(fields=["status", "opened_at"]),
        ]

    def clean(self):
        if self.cashbox_id:
            if self.company_id != self.cashbox.company_id:
                raise ValidationError({"company": "Компания смены должна совпадать с компанией кассы."})
            if (self.branch_id or None) != (self.cashbox.branch_id or None):
                raise ValidationError({"branch": "Филиал смены должен совпадать с филиалом кассы (или оба None)."})

        if self.cashier_id:
            cashier_company_id = getattr(self.cashier, "company_id", None)
            if cashier_company_id and cashier_company_id != self.company_id:
                raise ValidationError({"cashier": "Кассир другой компании."})

    def save(self, *args, **kwargs):
        """
        Делаем смену самосогласованной:
        - company/branch всегда берём из кассы
        - поддерживаем save(update_fields=...), добавляя нужные поля, если мы их поправили
        """
        update_fields = kwargs.get("update_fields")
        touched = set()

        if self.cashbox_id:
            cb_company_id = getattr(self.cashbox, "company_id", None)
            cb_branch_id = getattr(self.cashbox, "branch_id", None)

            if cb_company_id and self.company_id != cb_company_id:
                self.company_id = cb_company_id
                touched.add("company")
            if (self.branch_id or None) != (cb_branch_id or None):
                self.branch_id = cb_branch_id
                touched.add("branch")

        if update_fields is not None and touched:
            kwargs["update_fields"] = list(set(update_fields) | touched)

        is_new = self._state.adding
        self.full_clean()
        result = super().save(*args, **kwargs)
        if is_new and self.status == self.Status.OPEN:
            from apps.integrations.events import emit_event

            emit_event(
                self.company_id,
                "shift.opened",
                {
                    "shift": self.pk,
                    "cashbox": self.cashbox_id,
                    "cashier": self.cashier_id,
                    "opened_at": self.opened_at,
                    "opening_cash": self.opening_cash,
                },
            )
        return result

    def calc_live_totals(self, *, refresh: bool = False) -> dict:
        # Свойства expected_cash / drawer_expected_cash / ledger_expected_cash /
        # non_drawer_expenses_total / cash_diff вызывают этот метод каждое по разу,
        # а сериализатор списка читает их все — без мемоизации это ~60 SQL-запросов
        # на одну смену. Кэшируем на время жизни экземпляра.
        if not refresh:
            cached = getattr(self, "_live_totals_cache", None)
            if cached is not None:
                return cached

        z = Decimal("0.00")

        flows = self.shift_flows.filter(status=CashFlow.Status.APPROVED, request_kind__isnull=True)
        fa = flows.aggregate(
            income=Sum(
                "amount",
                filter=Q(type=CashFlow.Type.INCOME)
                & Q(affects_shift_drawer=True)
                & ~Q(
                    source_kind__in=[
                        CashFlow.SourceKind.POS_SALE,
                        CashFlow.SourceKind.POS_PREPAYMENT,
                    ]
                ),
            ),
            expense=Sum(
                "amount",
                filter=Q(type=CashFlow.Type.EXPENSE) & Q(affects_shift_drawer=True),
            ),
        )

        Sale = self.sales.model
        # Продажи смены — оплаченные, частично возвращённые и продажи в долг (Rule 2.2)
        sold_statuses = [Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED, Sale.Status.DEBT]
        revenue_statuses = [Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED]
        sales_qs = Sale.objects.filter(shift_id=self.id, status__in=sold_statuses)

        from apps.main.models import SaleItem, Product, SalePayment
        from django.db.models import ExpressionWrapper, F

        service_sales = (
            SaleItem.objects.filter(
                sale__shift_id=self.id,
                sale__status__in=revenue_statuses,
                product__kind=Product.Kind.SERVICE,
            ).aggregate(
                total=Sum(
                    ExpressionWrapper(
                        F("unit_price") * F("quantity") - F("line_discount"),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    )
                )
            )["total"]
            or z
        )

        sa = sales_qs.aggregate(
            cnt=Count("id"),
            total_sum=Sum("total"),
        )

        pay_agg = SalePayment.objects.filter(
            sale__shift_id=self.id,
            sale__status__in=revenue_statuses,
        ).aggregate(
            cash_sum=Sum(
                "amount",
                filter=Q(method=Sale.PaymentMethod.CASH),
            ),
            noncash_sum=Sum(
                "amount",
                filter=~Q(method__in=[Sale.PaymentMethod.CASH, Sale.PaymentMethod.DEBT, Sale.PaymentMethod.OFFSET]),
            ),
        )

        legacy_qs = Sale.objects.filter(shift_id=self.id, status__in=revenue_statuses).annotate(pay_cnt=Count("payments")).filter(pay_cnt=0)
        legacy_agg = legacy_qs.aggregate(
            cash_sum=Sum(
                Case(
                    When(payment_method=Sale.PaymentMethod.CASH, then="total"),
                    default=Value(0),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            ),
            noncash_sum=Sum(
                Case(
                    When(
                        ~Q(payment_method__in=[Sale.PaymentMethod.CASH, Sale.PaymentMethod.DEBT]),
                        then="total",
                    ),
                    default=Value(0),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            ),
        )

        non_sale_income = fa["income"] or z
        income_total = non_sale_income + service_sales
        expense_total = fa["expense"] or z
        sales_count = sa["cnt"] or 0
        sales_total = sa["total_sum"] or z
        cash_sales_total = (pay_agg["cash_sum"] or z) + (legacy_agg["cash_sum"] or z)
        noncash_sales_total = (pay_agg["noncash_sum"] or z) + (legacy_agg["noncash_sum"] or z)

        # Наличные, реально осевшие в ящике. Отличается от cash_sales_total
        # (витрина, только status=paid) двумя вещами:
        #   * частично возвращённые чеки учитываются — их строки оплат уже
        #     уменьшены до остатка, значит там именно та наличность, что осталась;
        #   * полностью отменённые не учитываются — деньги вернули покупателю.
        # Компенсирующее движение pos_sale_return из ящика НЕ вычитается
        # (affects_shift_drawer=False), иначе сумма снималась бы дважды.
        drawer_statuses = [Sale.Status.PAID, Sale.Status.PARTIALLY_RETURNED]
        drawer_cash_in = (
            SalePayment.objects.filter(
                sale__shift_id=self.id,
                sale__status__in=drawer_statuses,
                method=Sale.PaymentMethod.CASH,
            ).aggregate(s=Sum("amount"))["s"]
            or z
        )
        drawer_legacy_cash = (
            Sale.objects.filter(
                shift_id=self.id,
                status__in=drawer_statuses,
                payment_method=Sale.PaymentMethod.CASH,
            )
            .annotate(pay_cnt=Count("payments"))
            .filter(pay_cnt=0)
            .aggregate(s=Sum("total"))["s"]
            or z
        )

        # Предоплата наличными по продажам в долг (AN-01, AN-02)
        debt_prepayments_cash = (
            SalePayment.objects.filter(
                sale__shift_id=self.id,
                sale__status=Sale.Status.DEBT,
                method=Sale.PaymentMethod.CASH,
            ).aggregate(s=Sum("amount"))["s"]
            or z
        )
        debt_prepayments_legacy = (
            Sale.objects.filter(
                shift_id=self.id,
                status=Sale.Status.DEBT,
            )
            .annotate(pay_cnt=Count("payments"))
            .filter(pay_cnt=0)
            .aggregate(
                s=Sum(
                    Case(
                        When(cash_amount__gt=0, then="cash_amount"),
                        When(cash_received__gt=0, then="cash_received"),
                        default=Value(0),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    )
                )
            )["s"]
            or z
        )
        debt_prepayments_cash += debt_prepayments_legacy

        # Оплата долга наличными в рамках смены (AN-02)
        debt_payments_cash = (
            flows.filter(
                type=CashFlow.Type.INCOME,
                source_kind=CashFlow.SourceKind.DEBT_REPAYMENT,
                affects_shift_drawer=True,
            ).aggregate(s=Sum("amount"))["s"]
            or z
        )

        # Предоплата безналом по продажам в долг (debt-prepayment-method-backend.md)
        debt_prepayments_noncash = (
            SalePayment.objects.filter(
                sale__shift_id=self.id,
                sale__status=Sale.Status.DEBT,
            ).exclude(
                method__in=[Sale.PaymentMethod.CASH, Sale.PaymentMethod.DEBT, Sale.PaymentMethod.OFFSET]
            ).aggregate(s=Sum("amount"))["s"]
            or z
        )
        debt_prepayments_legacy_noncash = (
            Sale.objects.filter(
                shift_id=self.id,
                status=Sale.Status.DEBT,
            )
            .annotate(pay_cnt=Count("payments"))
            .filter(pay_cnt=0)
            .aggregate(
                s=Sum(
                    Case(
                        When(card_amount__gt=0, then="card_amount"),
                        default=Value(0),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    )
                )
            )["s"]
            or z
        )
        debt_prepayments_noncash += debt_prepayments_legacy_noncash

        drawer_expected_cash = (
            (self.opening_cash or z) + drawer_cash_in + drawer_legacy_cash + debt_prepayments_cash + non_sale_income - expense_total
        )
        expected_cash = drawer_expected_cash

        # non_drawer_expenses_total: закупки/расходы за период смены, не влияющие на ящик
        non_drawer_expenses_total = (
            CashFlow.objects.filter(
                company_id=self.company_id,
                created_at__gte=self.opened_at,
                created_at__lte=self.closed_at or timezone.now(),
                type=CashFlow.Type.EXPENSE,
                affects_shift_drawer=False,
                request_kind__isnull=True,
                source_kind__in=[
                    CashFlow.SourceKind.WAREHOUSE_PURCHASE,
                    CashFlow.SourceKind.PROCUREMENT_RECEIPT,
                    CashFlow.SourceKind.DEFECT_WRITEOFF,
                    CashFlow.SourceKind.SUPPLIER_DEBT_PAYMENT,
                ],
            ).aggregate(total=Sum("amount"))["total"]
            or z
        )

        totals = {
            "income_total": income_total + debt_prepayments_cash + debt_prepayments_noncash,
            "expense_total": expense_total,
            "sales_count": sales_count,
            "sales_total": sales_total,
            "cash_sales_total": cash_sales_total + debt_prepayments_cash,
            "noncash_sales_total": noncash_sales_total + debt_prepayments_noncash,
            "expected_cash": expected_cash,
            "drawer_expected_cash": drawer_expected_cash,
            "ledger_expected_cash": drawer_expected_cash,
            "non_drawer_expenses_total": non_drawer_expenses_total,
            "debt_prepayments_cash": debt_prepayments_cash,
            "debt_prepayments_noncash": debt_prepayments_noncash,
            "debt_payments_cash": debt_payments_cash,
            # ТЗ Н-05: слагаемые expected_cash для раскладки «в ящике» (та же формула, без второй)
            "drawer_cash_sales_net": drawer_cash_in + drawer_legacy_cash,
            "non_sale_income": non_sale_income,
        }

        # Приводим деньги к 2 знакам. Произведение numeric(12,2) * numeric(…,3)
        # приходит из БД с 5 знаками после запятой (service_sales), а денежные
        # поля смены — DecimalField(decimal_places=2): full_clean() при закрытии
        # смены отклонял такое значение, и касса получала 400.
        totals = {
            key: (value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                  if isinstance(value, Decimal) else value)
            for key, value in totals.items()
        }
        self._live_totals_cache = totals
        return totals

    @property
    def drawer_expected_cash(self) -> Decimal:
        return self.calc_live_totals().get("expected_cash", Decimal("0.00"))

    @property
    def expected_cash(self) -> Decimal:
        return self.drawer_expected_cash

    @property
    def ledger_expected_cash(self) -> Decimal:
        return self.drawer_expected_cash

    @property
    def non_drawer_expenses_total(self) -> Decimal:
        return self.calc_live_totals().get("non_drawer_expenses_total", Decimal("0.00"))

    @property
    def cash_diff(self) -> Decimal:
        if self.closing_cash is None:
            return Decimal("0.00")
        return (self.closing_cash or Decimal("0.00")) - (self.expected_cash or Decimal("0.00"))

    def calc_payment_breakdown(self, *, refresh: bool = False) -> list:
        # Сериализатор списка обращается к разбивке дважды на объект — кэшируем.
        if not refresh:
            cached = getattr(self, "_payment_breakdown_cache", None)
            if cached is not None:
                return cached

        Sale = self.sales.model
        from apps.main.models import SalePayment

        excluded_statuses = ["cancelled", "canceled", "refunded", "returned"]
        sales_qs = (
            Sale.objects.filter(shift_id=self.id)
            .exclude(status__in=excluded_statuses)
            .prefetch_related("payments")
        )

        METHOD_LABELS = {
            "cash": "Наличные",
            "mbank": "МБанк",
            "optima": "Оптима Банк",
            "obank": "О!Деньги",
            "bakai": "Бакай Банк",
            "demir": "Демир Банк",
            "transfer": "Перевод",
            "card": "Карта",
            "split": "Смешанная",
            "mixed": "Смешанная",
            "debt": "Отсрочка",
            "deferred": "Отсрочка",
            "other": "Другое",
        }

        breakdown_data = {}

        for sale in sales_qs:
            payments = [p for p in sale.payments.all() if (p.amount or Decimal("0")) > 0]
            methods = {str(p.method).lower().strip() for p in payments}

            if len(methods) >= 2:
                m_code = "split"
            elif sale.payment_method in ("deferred", "debt"):
                m_code = "debt"
            elif len(methods) == 1:
                m_code = list(methods)[0]
            else:
                m_code = str(sale.payment_method or "cash").lower().strip()

            if m_code in ("deferred", "debt"):
                m_code = "debt"
            elif m_code in ("mixed", "split"):
                m_code = "split"

            label = METHOD_LABELS.get(m_code, m_code.title() if m_code else "Другое")
            tot = sale.total or Decimal("0.00")

            if m_code not in breakdown_data:
                breakdown_data[m_code] = {"method": m_code, "label": label, "count": 0, "amount": Decimal("0.00")}

            breakdown_data[m_code]["count"] += 1
            breakdown_data[m_code]["amount"] += tot

        # Погашения долгов в рамках смены
        debt_flows = self.shift_flows.filter(
            status=CashFlow.Status.APPROVED,
            type=CashFlow.Type.INCOME,
            source_kind=CashFlow.SourceKind.DEBT_REPAYMENT,
            request_kind__isnull=True,
        )
        for cf in debt_flows:
            m_code = str(getattr(cf, "payment_method", None) or "cash").lower().strip()
            if m_code in ("mixed", "split"):
                m_code = "split"
            label = METHOD_LABELS.get(m_code, m_code.title() if m_code else "Другое")
            tot = cf.amount or Decimal("0.00")
            if m_code not in breakdown_data:
                breakdown_data[m_code] = {"method": m_code, "label": label, "count": 0, "amount": Decimal("0.00")}
            breakdown_data[m_code]["count"] += 1
            breakdown_data[m_code]["amount"] += tot

        sorted_items = sorted(breakdown_data.values(), key=lambda x: (x["amount"], x["count"]), reverse=True)

        res = []
        for item in sorted_items:
            res.append({
                "method": item["method"],
                "label": item["label"],
                "count": item["count"],
                "amount": str(item["amount"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            })

        self._payment_breakdown_cache = res
        return res

    def other_open_shifts_on_cashbox(self) -> list:
        """
        Чужие открытые смены на той же кассе.

        Касса — это один физический денежный ящик, но открытых смен на ней может
        быть несколько (по одной на кассира), и у каждой свой независимый
        opening_cash. Из-за этого один и тот же ящик считается дважды, и сверка
        при закрытии даёт расхождение на чужие деньги. Отдаём данные, чтобы
        предупредить кассира до того, как он введёт свой остаток.
        """
        if not self.cashbox_id:
            return []

        qs = (
            CashShift.objects
            .filter(cashbox_id=self.cashbox_id, status=CashShift.Status.OPEN)
            .exclude(pk=self.pk)
            .select_related("cashier")
            .order_by("opened_at")
        )

        res = []
        for sh in qs:
            u = sh.cashier
            display = ""
            if u is not None:
                display = (
                    f"{(u.first_name or '').strip()} {(u.last_name or '').strip()}".strip()
                    or getattr(u, "email", "") or ""
                )
            res.append({
                "shift_id": str(sh.id),
                "cashier_id": str(sh.cashier_id) if sh.cashier_id else None,
                "cashier_display": display,
                "opened_at": sh.opened_at.isoformat() if sh.opened_at else None,
                "opening_cash": str(sh.opening_cash or Decimal("0.00")),
                "drawer_expected_cash": str(sh.drawer_expected_cash),
            })
        return res

    def cashbox_shared_warning(self) -> dict | None:
        """Готовое предупреждение для UI или None, если касса занята только этой сменой."""
        others = self.other_open_shifts_on_cashbox()
        if not others:
            return None

        cashbox_name = getattr(self.cashbox, "name", None) or "касса"
        first = others[0]
        who = first["cashier_display"] or "другой кассир"
        expected = first["drawer_expected_cash"]
        tail = "" if len(others) == 1 else f" (всего открытых смен на кассе: {len(others) + 1})"
        return {
            "code": "cashbox_has_other_open_shift",
            "message": (
                f"На кассе «{cashbox_name}» уже открыта смена: {who}. "
                f"По её данным в ящике ожидается {expected} сом. "
                f"Проверьте, что вы открыли смену на своей кассе — иначе при закрытии "
                f"будет расхождение на чужие деньги.{tail}"
            ),
            "cashbox_id": str(self.cashbox_id),
            "cashbox_name": cashbox_name,
            "open_shifts": others,
        }

    def calc_cashier_breakdown(self) -> list:
        """
        Разбивка продаж смены по продавцам чеков (shift-sales-cashier-filter-backend.md §4.2).

        Группируем по `Sale.user` — это тот, кто пробил чек, а не владелец смены
        (`shift.cashier`): в общей смене на одну кассу это разные люди. Отменённые
        и возвращённые чеки исключаем — как в calc_payment_breakdown.
        """
        Sale = self.sales.model

        excluded_statuses = ["cancelled", "canceled", "refunded", "returned"]
        rows = (
            Sale.objects
            .filter(shift_id=self.id)
            .exclude(status__in=excluded_statuses)
            .values("user_id", "user__first_name", "user__last_name", "user__email")
            .annotate(sales_count=models.Count("id"), sales_total=models.Sum("total"))
        )

        res = []
        for r in rows:
            display = " ".join(
                part for part in [
                    (r.get("user__first_name") or "").strip(),
                    (r.get("user__last_name") or "").strip(),
                ] if part
            ).strip() or (r.get("user__email") or "") or "Без кассира"
            total = r.get("sales_total") or Decimal("0.00")
            res.append({
                "cashier_id": str(r["user_id"]) if r["user_id"] else None,
                "cashier_display": display,
                "sales_count": int(r.get("sales_count") or 0),
                "sales_total": str(Decimal(total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            })

        res.sort(key=lambda x: (Decimal(x["sales_total"]), x["sales_count"]), reverse=True)
        return res

    def returns_summary(self) -> dict:
        """
        11: возвраты, оформленные в этой смене (сама смена возврата, не смена продажи).
        returns_total — сумма возвращённых товаров; returns_cash/returns_noncash —
        сколько денег фактически отдали и каким способом. По чеку в долг деньги
        могут не выдаваться: returns_cash + returns_noncash бывает меньше returns_total.
        """
        from apps.main.models import SaleReturn

        z = Decimal("0.00")
        agg = SaleReturn.objects.filter(shift_id=self.id).aggregate(
            c=Count("id"), t=Sum("returned_amount"), cash=Sum("refund_cash"), noncash=Sum("refund_noncash"),
        )
        q = lambda v: (v or z).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # noqa: E731
        return {
            "returns_count": agg["c"] or 0,
            "returns_total": str(q(agg["t"])),
            "returns_cash": str(q(agg["cash"])),
            "returns_noncash": str(q(agg["noncash"])),
        }

    def recalc_totals_for_close(self):
        t = self.calc_live_totals(refresh=True)
        self.income_total = t["income_total"]
        self.expense_total = t["expense_total"]
        self.sales_count = t["sales_count"]
        self.sales_total = t["sales_total"]
        self.cash_sales_total = t["cash_sales_total"]
        self.noncash_sales_total = t["noncash_sales_total"]

    def close(self, closing_cash: Decimal, close_reason: str = ""):
        if self.status != self.Status.OPEN:
            raise ValidationError({"status": "Смена уже закрыта."})

        self.closing_cash = closing_cash
        self.closed_at = timezone.now()
        if close_reason:
            self.close_reason = close_reason
        self.recalc_totals_for_close()

        self.status = self.Status.CLOSED
        self.save(
            update_fields=[
                "closing_cash",
                "closed_at",
                "close_reason",
                "income_total",
                "expense_total",
                "sales_count",
                "sales_total",
                "cash_sales_total",
                "noncash_sales_total",
                "status",
            ]
        )

        from apps.integrations.events import emit_event

        emit_event(
            self.company_id,
            "shift.closed",
            {
                "shift": self.pk,
                "cashbox": self.cashbox_id,
                "cashier": self.cashier_id,
                "opened_at": self.opened_at,
                "closed_at": self.closed_at,
                "sales_count": self.sales_count,
                "sales_total": self.sales_total,
                "cash_sales_total": self.cash_sales_total,
                "noncash_sales_total": self.noncash_sales_total,
                "expected_cash": self.expected_cash,
                "closing_cash": self.closing_cash,
            },
        )

    def __str__(self):
        return f"Смена {self.cashier} / {self.cashbox} ({self.status})"


class CashFlowQuerySet(models.QuerySet):
    # Канонические source_kind, у которых бывают «сырые» двойники
    # (source_kind=NULL, source_cashbox_flow_id = source_id канонической строки).
    RAW_TWIN_KINDS = ("pos_sale", "pos_prepayment", "debt_repayment", "warehouse_purchase")
    # Старые source_kind, вышедшие из choices: дубль, если есть каноническая строка с тем же source_id.
    LEGACY_KINDS = ("sale", "debt_payment")

    NON_MONEY_METHODS = ("offset", "debt")

    def money(self):
        """
        Движения, которые считаются деньгами: одобренные и не строки заявок
        на правку/отмену (у тех request_kind заполнен, а исходное движение
        правится или отклоняется само), без неденежных способов оплаты (offset, debt)
        и без технических поправок наличного ящика при возврате/обмене
        (source_kind=pos_sale_return + income).
        """
        return (
            self.filter(status=CashFlow.Status.APPROVED, request_kind__isnull=True)
            .exclude(payment_method__in=self.NON_MONEY_METHODS)
            .exclude(source_kind="pos_sale_return", type=CashFlow.Type.INCOME)
        )

    def without_raw_twins(self):
        """
        Исключает «сырые» дубли: строку без source_kind со ссылкой
        source_cashbox_flow_id (или legacy source_kind с тем же source_id), если
        есть одобренная каноническая строка того же типа с source_id = этой ссылке. Строки с одним source_id (части смешанной
        оплаты, несколько погашений, продажа и возврат) не склеиваются.
        """
        def twin(ref_field):
            return models.Exists(CashFlow.objects.filter(
                company_id=models.OuterRef("company_id"),
                source_id=models.OuterRef(ref_field),
                type=models.OuterRef("type"),
                status=CashFlow.Status.APPROVED,
                source_kind__in=self.RAW_TWIN_KINDS,
            ))

        return self.exclude(
            (Q(source_kind__isnull=True) & Q(source_cashbox_flow_id__isnull=False) & twin("source_cashbox_flow_id"))
            | (Q(source_kind__in=self.LEGACY_KINDS) & twin("source_id"))
        )


class CashFlow(models.Model):
    class Type(models.TextChoices):
        INCOME = "income", "Приход"
        EXPENSE = "expense", "Расход"

    class Status(models.TextChoices):
        PENDING = "pending", "В ожидании"
        APPROVED = "approved", "Успешно"
        REJECTED = "rejected", "Отклонено"

    class SourceKind(models.TextChoices):
        POS_SALE = "pos_sale", "POS продажа"
        POS_PREPAYMENT = "pos_prepayment", "POS предоплата"
        DEBT_REPAYMENT = "debt_repayment", "Погашение долга"
        SUPPLIER_DEBT_PAYMENT = "supplier_debt_payment", "Оплата долга поставщику"
        WAREHOUSE_PURCHASE = "warehouse_purchase", "Закупка товара"
        PROCUREMENT_RECEIPT = "procurement_receipt", "Приход поставщика"
        SUPPLIER_RETURN = "supplier_return", "Возврат поставщику"
        DEFECT_WRITEOFF = "defect_writeoff", "Списание брака"
        PRODUCT_RETURN = "product_return", "Возврат товара"
        SHIFT_DRAWER_OUTFLOW = "shift_drawer_outflow", "Расход из ящика смены"
        SHIFT_DRAWER_INFLOW = "shift_drawer_inflow", "Внесение в ящик смены"
        WORK_ORDER_PREPAYMENT = "work_order_prepayment", "Предоплата заказ-наряда"
        RENTAL_DEPOSIT = "rental_deposit", "Залог за прокат"
        POS_SALE_RETURN = "pos_sale_return", "Возврат продажи"
        CASHFLOW_CANCEL = "cashflow_cancel", "Отмена движения"
        MANUAL = "manual", "Ручная операция"
        # ТЗ Н-19: типы расходов, которые ставит касса вместе со статьёй и сменой.
        SHIFT_DRAWER_COLLECTION = "shift_drawer_collection", "Инкассация из ящика смены"
        SALARY_PAYOUT = "salary_payout", "Выплата зарплаты"
        RENT_PAYMENT = "rent_payment", "Оплата аренды"
        SUPPLIER_PAYMENT = "supplier_payment", "Оплата поставщику"

    class RequestKind(models.TextChoices):
        EDIT = "edit", "Редактирование"
        CANCEL = "cancel", "Отмена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="cash_cashflows", verbose_name="Компания")
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="cash_cashflows",
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Филиал",
    )

    cashbox = models.ForeignKey(Cashbox, on_delete=models.CASCADE, related_name="flows", verbose_name="Касса")
    type = models.CharField(max_length=10, choices=Type.choices, verbose_name="Тип")
    name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Наименование")
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Сумма")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True, verbose_name="Статус")

    request_kind = models.CharField(
        max_length=16,
        choices=RequestKind.choices,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Тип заявки",
    )
    target_flow = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="change_requests",
        db_index=True,
        verbose_name="Целевое движение",
    )
    proposed = models.JSONField(
        default=dict,
        blank=True,
        null=True,
        verbose_name="Предлагаемые изменения",
    )
    reason = models.TextField(
        default="",
        blank=True,
        verbose_name="Причина заявки",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cashflow_change_requests",
        db_index=True,
        verbose_name="Кем запрошено",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cashflow_resolved_requests",
        db_index=True,
        verbose_name="Кем разрешено",
    )
    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Когда разрешено",
    )

    affects_shift_drawer = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Влияет на ящик смены",
    )

    source_cashbox_flow_id = models.CharField(max_length=36, null=True, blank=True, verbose_name="ID исходного движения кассы")
    source_business_operation_id = models.CharField(max_length=36, null=True, blank=True, verbose_name="ID бизнес-операции")

    source_kind = models.CharField(
        max_length=50,
        choices=SourceKind.choices,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Тип источника",
    )
    source_id = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="ID источника",
    )
    idempotency_key = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Ключ идемпотентности",
    )
    payment_method = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Способ оплаты",
    )
    # ТЗ 29.10 Б-100: "startup" — вложение при открытии, не входит в opex/operating_profit отчёта pnl
    kind = models.CharField(max_length=16, blank=True, default="", db_default="", verbose_name="Вид расхода")

    shift = models.ForeignKey(
        "construction.CashShift",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="shift_flows",
        db_index=True,
        verbose_name="Смена",
    )
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_flows",
        verbose_name="Кассир",
    )
    category = models.ForeignKey(
        "CashFlowCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cashflows",
        verbose_name="Категория",
    )

    # ТЗ «Бухгалтер», п. 6: отметка «проверено» (деньги не меняет)
    checked_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name="Проверено")
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="checked_cash_flows", verbose_name="Кто проверил",
    )
    accountant_note = models.TextField(blank=True, default="", db_default="", verbose_name="Комментарий бухгалтера")

    objects = CashFlowQuerySet.as_manager()

    class Meta:
        verbose_name = "Движение по кассе"
        verbose_name_plural = "Движения по кассе"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "created_at"]),
            models.Index(fields=["company", "branch", "created_at"]),
            models.Index(fields=["cashbox", "created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["cashbox", "status", "type", "created_at"], name="ix_flow_cb_stat_type_created"),
            # Аналитика Производства → Расходы: company + type=expense за период.
            models.Index(fields=["company", "type", "created_at"], name="ix_flow_company_type_created"),
            models.Index(fields=["shift", "created_at"]),
            models.Index(fields=["cashier", "created_at"]),
            models.Index(fields=["company", "source_kind", "source_id"], name="ix_flow_comp_src_kind_id"),
            models.Index(fields=["target_flow", "status"], name="ix_flow_target_status"),
            models.Index(fields=["request_kind"], name="ix_flow_request_kind"),
        ]
        constraints = [
            models.CheckConstraint(check=Q(amount__gt=0), name="ck_cashflow_amount_positive"),
        ]

    def clean(self):
        if self.cashbox_id:
            if self.company_id != self.cashbox.company_id:
                raise ValidationError({"company": "Компания движения должна совпадать с компанией кассы."})
            if (self.branch_id or None) != (self.cashbox.branch_id or None):
                raise ValidationError({"branch": "Филиал движения должен совпадать с филиалом кассы (или оба None)."})

        if self.shift_id:
            if self.shift.cashbox_id != self.cashbox_id:
                raise ValidationError({"shift": "Смена относится к другой кассе."})
            if self.cashier_id and self.cashier_id != self.shift.cashier_id:
                raise ValidationError({"cashier": "Кассир не совпадает с кассиром смены."})

        if self.category_id:
            if self.category.company_id != self.company_id:
                raise ValidationError({"category": "Категория другой компании."})
            cb_br = self.cashbox.branch_id if self.cashbox_id else None
            cat_br = self.category.branch_id
            if cat_br is not None and cat_br != cb_br:
                raise ValidationError({"category": "Категория другого филиала (или укажите общую категорию без филиала)."})

    def save(self, *args, **kwargs):
        if self.source_kind in (
            self.SourceKind.SHIFT_DRAWER_OUTFLOW,
            self.SourceKind.SHIFT_DRAWER_INFLOW,
            self.SourceKind.SHIFT_DRAWER_COLLECTION,  # ТЗ Н-19: инкассация уменьшает ящик
        ):
            self.affects_shift_drawer = True

        if self.cashbox_id:
            self.company_id = self.cashbox.company_id
            self.branch_id = self.cashbox.branch_id

        if self.shift_id:
            self.cashier_id = self.shift.cashier_id

        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_type_display()} {self.amount} ({self.status})"


class AuditLog(models.Model):
    """Журнал изменений денежных операций (ТЗ «Бухгалтер», п. 7). Только дописывается."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="audit_logs")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=32)  # create/update/delete/edit_request/cancel_request/approve/reject/closed_until
    object_type = models.CharField(max_length=32)  # cashflow, period
    object_id = models.CharField(max_length=64, blank=True, default="")
    object_name = models.CharField(max_length=255, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "object_type", "object_id", "-created_at"], name="ix_audit_obj"),
            models.Index(fields=["company", "-created_at"], name="ix_audit_company_date"),
        ]


class TrashEntry(models.Model):
    """
    Корзина удалённого (ТЗ п.9). В apps/main нельзя добавить deleted_at, поэтому при удалении
    товара/клиента/категории/бренда сюда кладётся снимок строки (и всего, что удалится каскадом);
    «вернуть» пересоздаёт строки из снимка. Хранится 30 дней.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="trash_entries")
    type = models.CharField(max_length=16)  # product / client / category / brand
    object_id = models.CharField(max_length=64)
    object_name = models.CharField(max_length=255, blank=True, default="")
    payload = models.JSONField(default=dict)
    deleted_at = models.DateTimeField(auto_now_add=True)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-deleted_at"]
        indexes = [
            models.Index(fields=["company", "type", "-deleted_at"], name="ix_trash_company_type"),
            models.Index(fields=["company", "type", "object_id"], name="ix_trash_obj"),
        ]
