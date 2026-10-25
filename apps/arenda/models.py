"""
Модель данных «Аренда помещений» (ТЗ §4).

Правила:
- все суммы — целые тыйыны (BigIntegerField); площадь — Decimal (м²);
- у каждой записи company, created_at, created_by;
- деньги и документы не удаляются физически: archived_at / статусы / сторно;
- долг и статус арендатора не хранятся, считаются из Charge и Payment.
"""
import uuid

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q

from apps.users.models import Branch, Company

User = settings.AUTH_USER_MODEL


class Base(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        abstract = True


class Archivable(models.Model):
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------- настройки / номера


class ArendaSettings(Base):
    """Настройки модуля на компанию: реквизиты (ТЗ §3.11), ставки, параметры."""

    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="arenda_settings")
    org_name = models.CharField(max_length=255, blank=True, default="")
    inn = models.CharField(max_length=14, blank=True, default="")
    okpo = models.CharField(max_length=16, blank=True, default="")
    legal_address = models.CharField(max_length=255, blank=True, default="")
    director = models.CharField(max_length=255, blank=True, default="")
    chief_accountant = models.CharField(max_length=255, blank=True, default="")
    bank = models.CharField(max_length=255, blank=True, default="")
    bik = models.CharField(max_length=6, blank=True, default="")
    account = models.CharField(max_length=16, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    email = models.CharField(max_length=128, blank=True, default="")

    penalty_rate = models.DecimalField(max_digits=6, decimal_places=3, default="0.100",
                                       help_text="% в день по умолчанию")
    payroll_social_employee = models.DecimalField(max_digits=5, decimal_places=2, default="10.00")
    payroll_income_tax = models.DecimalField(max_digits=5, decimal_places=2, default="10.00")
    payroll_social_employer = models.DecimalField(max_digits=5, decimal_places=2, default="17.25")
    advance_percent = models.PositiveSmallIntegerField(default=40)
    advance_day = models.PositiveSmallIntegerField(default=10)
    settle_day = models.PositiveSmallIntegerField(default=0, help_text="0 = последний день месяца")
    soon_days = models.PositiveSmallIntegerField(default=7)
    lease_expiring_days = models.PositiveSmallIntegerField(default=45)
    indexation_notice_days = models.PositiveSmallIntegerField(default=30)


class Counter(models.Model):
    """Сквозной счётчик документов без пропусков (select_for_update)."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=24)  # lease / employment / receipt / act
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "key"], name="uq_arenda_counter")]

    @classmethod
    def next(cls, company, key):
        """Следующий номер. Вызывать внутри transaction.atomic() вместе с созданием документа."""
        with transaction.atomic():
            cls.objects.get_or_create(company=company, key=key)
            c = cls.objects.select_for_update().get(company=company, key=key)
            c.last_value += 1
            c.save(update_fields=["last_value"])
            return c.last_value


# ---------------------------------------------------------------- филиалы, помещения


class ArendaBranchInfo(Base):
    branch = models.OneToOneField(Branch, on_delete=models.CASCADE, related_name="arenda_info")
    city = models.CharField(max_length=128, blank=True, default="")
    object_type = models.CharField(max_length=64, blank=True, default="")  # ТЦ, здание...
    floors = models.PositiveSmallIntegerField(default=1)
    manager_name = models.CharField(max_length=255, blank=True, default="")
    manager_user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")


class UnitType(Base, Archivable):
    name = models.CharField(max_length=64)
    is_system = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "name"], name="uq_arenda_unittype_name")]
        ordering = ["created_at"]


class Unit(Base, Archivable):
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="arenda_units")
    floor = models.SmallIntegerField(default=1)
    number = models.CharField(max_length=32)
    unit_type = models.ForeignKey(UnitType, null=True, blank=True, on_delete=models.SET_NULL, related_name="units")
    area = models.DecimalField(max_digits=10, decimal_places=2)  # м²
    rate = models.BigIntegerField(default=0)  # тыйын за м² в месяц
    reserved_until = models.DateField(null=True, blank=True)
    reserved_for = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["branch", "number"], condition=Q(archived_at__isnull=True),
                                    name="uq_arenda_unit_number_branch"),
        ]
        indexes = [models.Index(fields=["company", "branch", "floor"])]
        ordering = ["floor", "number"]


# ---------------------------------------------------------------- арендаторы, договоры


class Tenant(Base, Archivable):
    FORMS = [("ip", "ИП"), ("ooo", "ОсОО"), ("person", "Физлицо")]
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    name = models.CharField(max_length=255)
    form = models.CharField(max_length=8, choices=FORMS, default="ip")
    inn = models.CharField(max_length=32, blank=True, default="")
    contact_person = models.CharField(max_length=255, blank=True, default="")
    passport = models.CharField(max_length=128, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    email = models.CharField(max_length=128, blank=True, default="")
    activity = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["company", "name"])]
        ordering = ["name"]


class Template(Base, Archivable):
    KINDS = [("lease", "Аренда"), ("employment", "Трудовой")]
    kind = models.CharField(max_length=16, choices=KINDS)
    name = models.CharField(max_length=255)
    text = models.TextField(blank=True, default="")
    source_file = models.FileField(upload_to="arenda/templates/", null=True, blank=True)


class Lease(Base):
    STATUSES = [("draft", "Черновик"), ("active", "Действует"), ("terminated", "Расторгнут")]
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="arenda_leases")
    number = models.PositiveIntegerField(null=True, blank=True)  # сквозной номер
    label = models.CharField(max_length=32, blank=True, default="")  # А-2026/031
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="leases")
    units = models.ManyToManyField(Unit, related_name="leases")
    template = models.ForeignKey(Template, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    rent_monthly = models.BigIntegerField(null=True, blank=True, help_text="Если пусто: площадь × ставка")
    pay_day = models.PositiveSmallIntegerField(default=10)
    deposit_months = models.PositiveSmallIntegerField(default=0)
    deposit_amount = models.BigIntegerField(default=0)
    penalty_rate = models.DecimalField(max_digits=6, decimal_places=3, default="0.100")
    indexation_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    utilities = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUSES, default="active")
    text_snapshot = models.TextField(blank=True, default="")
    signed_at = models.DateTimeField(null=True, blank=True)
    terminated_at = models.DateField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "number"], condition=Q(number__isnull=False),
                                    name="uq_arenda_lease_number"),
        ]
        indexes = [models.Index(fields=["company", "branch", "status"])]
        ordering = ["-start_date"]


# ---------------------------------------------------------------- деньги


class Charge(Base):
    KINDS = [("rent", "Аренда"), ("penalty", "Пеня"), ("fine", "Штраф"), ("utilities", "Коммунальные")]
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="charges")
    lease = models.ForeignKey(Lease, null=True, blank=True, on_delete=models.PROTECT, related_name="charges")
    kind = models.CharField(max_length=12, choices=KINDS)
    period = models.DateField(help_text="Первое число месяца начисления")
    amount = models.BigIntegerField()
    due_date = models.DateField()
    description = models.CharField(max_length=255, blank=True, default="")
    voided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["lease", "kind", "period"],
                                    condition=Q(kind__in=["rent", "utilities"], lease__isnull=False),
                                    name="uq_arenda_charge_lease_kind_period"),
        ]
        indexes = [models.Index(fields=["company", "tenant", "due_date"]),
                   models.Index(fields=["company", "branch", "period"])]
        ordering = ["due_date", "created_at"]


class Payment(Base):
    METHODS = [("cash", "Наличные"), ("bank", "Банк"), ("qr", "QR / Элкарт"), ("wallet", "Кошелёк")]
    STATUSES = [("posted", "Проведена"), ("reversed", "Сторнирована")]
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    amount = models.BigIntegerField()
    method = models.CharField(max_length=8, choices=METHODS, default="cash")
    paid_at = models.DateTimeField()
    cashier = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    description = models.CharField(max_length=255, blank=True, default="")  # для прочего прихода
    add_penalty = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUSES, default="posted")
    reversal_reason = models.CharField(max_length=255, blank=True, default="")
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        indexes = [models.Index(fields=["company", "branch", "paid_at"]),
                   models.Index(fields=["company", "tenant", "status"])]
        ordering = ["-paid_at"]


class PaymentAllocation(Base):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="allocations")
    charge = models.ForeignKey(Charge, on_delete=models.PROTECT, related_name="allocations")
    amount = models.BigIntegerField()


class Receipt(Base):
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, related_name="receipt")
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=32, blank=True, default="")
    snapshot = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="uq_arenda_receipt_number")]


class ReconciliationAct(Base):
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="acts")
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=32, blank=True, default="")
    period_from = models.DateField()
    period_to = models.DateField()
    opening_balance = models.BigIntegerField(default=0)  # >0 = долг арендатора
    closing_balance = models.BigIntegerField(default=0)
    snapshot = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="uq_arenda_act_number")]


class Remark(Base):
    TYPES = [("remark", "Замечание"), ("warning", "Предупреждение"),
             ("reprimand", "Выговор"), ("fine", "Штраф")]
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="remarks")
    type = models.CharField(max_length=12, choices=TYPES)
    date = models.DateField()
    text = models.TextField(blank=True, default="")
    issued_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    fine_amount = models.BigIntegerField(null=True, blank=True)
    notify_tenant = models.BooleanField(default=False)
    charge = models.OneToOneField(Charge, null=True, blank=True, on_delete=models.PROTECT, related_name="remark")
    archived_at = models.DateTimeField(null=True, blank=True)


# ---------------------------------------------------------------- уведомления


class Reminder(Base):
    CHANNELS = [("sms", "SMS"), ("whatsapp", "WhatsApp"), ("telegram", "Telegram")]
    STATUSES = [("not_sent", "Не отправлено"), ("sent", "Отправлено"),
                ("delivered", "Доставлено"), ("failed", "Ошибка")]
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name="reminders")
    lease = models.ForeignKey(Lease, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    channel = models.CharField(max_length=10, choices=CHANNELS, default="sms")
    text = models.TextField(blank=True, default="")
    is_auto = models.BooleanField(default=False)
    rule_kind = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUSES, default="not_sent")
    error = models.CharField(max_length=255, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=128, blank=True, default="")  # для идемпотентных автонапоминаний

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "dedupe_key"], condition=~Q(dedupe_key=""),
                                    name="uq_arenda_reminder_dedupe"),
        ]
        ordering = ["-created_at"]


class ReminderRule(Base):
    kind = models.CharField(max_length=32)
    enabled = models.BooleanField(default=False)
    params = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "kind"], name="uq_arenda_rule_kind")]


# ---------------------------------------------------------------- расходы, налоги, активы


class ExpenseCategory(Base, Archivable):
    name = models.CharField(max_length=64)
    is_system = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "name"], name="uq_arenda_expcat_name")]
        ordering = ["created_at"]


class Expense(Base):
    METHODS = Payment.METHODS
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="expenses")
    date = models.DateField()
    amount = models.BigIntegerField()
    description = models.CharField(max_length=255, blank=True, default="")
    method = models.CharField(max_length=8, choices=METHODS, default="cash")
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["company", "branch", "date"])]
        ordering = ["-date", "-created_at"]


class TaxPayment(Base):
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    tax = models.CharField(max_length=128)
    due_date = models.DateField()
    amount = models.BigIntegerField(default=0)
    paid = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    expense = models.OneToOneField(Expense, null=True, blank=True, on_delete=models.SET_NULL, related_name="tax_payment")
    source = models.CharField(max_length=16, blank=True, default="")  # payroll / manual
    dedupe_key = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["due_date"]


class Asset(Base, Archivable):
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    name = models.CharField(max_length=255)
    cost = models.BigIntegerField()
    purchase_date = models.DateField()
    life_years = models.PositiveSmallIntegerField(default=1)


# ---------------------------------------------------------------- сотрудники, зарплата


class Employee(Base, Archivable):
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    full_name = models.CharField(max_length=255)
    position = models.CharField(max_length=128, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    hired_at = models.DateField(null=True, blank=True)
    fired_at = models.DateField(null=True, blank=True)
    schedule = models.CharField(max_length=64, blank=True, default="")
    salary = models.BigIntegerField(default=0)
    bonus = models.BigIntegerField(default=0)


class EmploymentContract(Base):
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="contracts")
    template = models.ForeignKey(Template, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=32, blank=True, default="")
    date = models.DateField()
    text_snapshot = models.TextField(blank=True, default="")
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "number"], name="uq_arenda_empcontract_number")]


class Payroll(Base):
    STATUSES = [("draft", "Черновик"), ("advance_paid", "Аванс выплачен"), ("closed", "Закрыта")]
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="+")
    month = models.DateField(help_text="Первое число месяца")
    advance_date = models.DateField(null=True, blank=True)
    settle_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=14, choices=STATUSES, default="draft")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "branch", "month"], name="uq_arenda_payroll_month")]


class PayrollLine(Base):
    payroll = models.ForeignKey(Payroll, on_delete=models.CASCADE, related_name="lines")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="payroll_lines")
    salary = models.BigIntegerField(default=0)
    bonus = models.BigIntegerField(default=0)
    accrued = models.BigIntegerField(default=0)
    social = models.BigIntegerField(default=0)
    income_tax = models.BigIntegerField(default=0)
    net = models.BigIntegerField(default=0)
    advance = models.BigIntegerField(default=0)
    to_pay = models.BigIntegerField(default=0)
    employer_contribution = models.BigIntegerField(default=0)
    advance_paid = models.BigIntegerField(default=0)
    paid = models.BigIntegerField(default=0)
    advance_expense = models.ForeignKey(Expense, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    settle_expense = models.ForeignKey(Expense, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["payroll", "employee"], name="uq_arenda_payrollline")]


# ---------------------------------------------------------------- доступы, журнал


class ArendaAccess(Base):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="arenda_access")
    preset = models.CharField(max_length=24, blank=True, default="")
    sections = models.JSONField(default=list, blank=True)
    all_branches = models.BooleanField(default=True)
    branches = models.ManyToManyField(Branch, blank=True, related_name="+")
    is_active = models.BooleanField(default=True)
    employee = models.ForeignKey(Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")


class AuditLog(models.Model):
    """Журнал действий: кто, что, когда, старое и новое значение. Только дописывается."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=32)
    object_type = models.CharField(max_length=32)
    object_id = models.CharField(max_length=64, blank=True, default="")
    object_name = models.CharField(max_length=255, blank=True, default="")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["company", "-created_at"], name="ix_arenda_audit_company"),
                   models.Index(fields=["company", "object_type", "object_id"], name="ix_arenda_audit_obj")]
