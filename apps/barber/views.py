from rest_framework import generics, permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.filters import SearchFilter, OrderingFilter
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import time, datetime
from decimal import Decimal, ROUND_HALF_UP
from django.db.models.deletion import ProtectedError
from django.db import IntegrityError, models, transaction
from django.db.models import Q, Prefetch, Count, Sum, Avg, F, Value, DecimalField, ExpressionWrapper

from django_filters import rest_framework as filters
from django_filters.rest_framework import DjangoFilterBackend

from apps.users.models import Branch, Company
from .models import Service, Client, Appointment, AppointmentService, Document, Folder, ServiceCategory, Payout, PayoutSale, ProductSalePayout, OnlineBooking, ClientDocument

from .serializers import (
    ServiceSerializer,
    ClientSerializer,
    ClientDetailSerializer,
    ClientDocumentSerializer,
    AppointmentSerializer,
    AppointmentHistoryRowSerializer,
    FolderSerializer,
    DocumentSerializer,
    ServiceCategorySerializer,
    PayoutSerializer,
    PayoutSaleSerializer,
    ProductSalePayoutSerializer,
    OnlineBookingCreateSerializer,
    OnlineBookingMultiCreateSerializer,
    OnlineBookingSerializer,
    OnlineBookingStatusUpdateSerializer,
    PublicServiceSerializer,
    PublicServiceCategorySerializer,
    PublicMasterSerializer,
    PublicMasterScheduleSerializer,
    PublicMasterAvailabilitySerializer,
    BarberAnalyticsResponseSerializer
)


# ---- DocumentFilter ----
from apps.users.permissions import read_only_guard  # noqa: E402
class DocumentFilter(filters.FilterSet):
    name = filters.CharFilter(lookup_expr="icontains")
    folder = filters.UUIDFilter(field_name="folder__id")
    file_name = filters.CharFilter(field_name="file", lookup_expr="icontains")
    created_at = filters.DateTimeFromToRangeFilter()
    updated_at = filters.DateTimeFromToRangeFilter()

    class Meta:
        model = Document
        fields = ["name", "folder", "file_name", "created_at", "updated_at"]


# ---- Appointment / visits history filters ----
class _CharInFilter(filters.BaseInFilter, filters.CharFilter):
    """Поддержка query param вида: ?status__in=completed,canceled,no_show"""


class ClientVisitHistoryFilter(filters.FilterSet):
    status__in = _CharInFilter(field_name="status", lookup_expr="in")
    start_at_from = filters.DateTimeFilter(field_name="start_at", lookup_expr="gte")
    start_at_to = filters.DateTimeFilter(field_name="start_at", lookup_expr="lte")

    class Meta:
        model = Appointment
        fields = ["status", "barber", "start_at"]


class VisitHistoryFilter(filters.FilterSet):
    """
    Фильтры общего списка истории визитов.
    """

    status__in = _CharInFilter(field_name="status", lookup_expr="in")
    start_at_from = filters.DateTimeFilter(field_name="start_at", lookup_expr="gte")
    start_at_to = filters.DateTimeFilter(field_name="start_at", lookup_expr="lte")
    client = filters.UUIDFilter(field_name="client_id")
    barber = filters.UUIDFilter(field_name="barber_id")

    class Meta:
        model = Appointment
        fields = ["status", "client", "barber", "start_at"]


# ==== Company + Branch scoped mixin ====
class CompanyQuerysetMixin:
    """
    Видимость данных:

      - company берётся из request.user.company/owned_company
      - branch определяется:
          1) «жёсткий» филиал пользователя:
               - user.branch (если есть и принадлежит компании)
               - единственный id в user.branch_ids (если список есть и в нём ровно 1 элемент)
          2) если жёсткого филиала нет — пробуем ?branch=<uuid>, если филиал принадлежит компании
          3) иначе branch = None (режим по всей компании)

      Логика выборки:
        - если branch определён → показываем только записи этого филиала;
        - если branch = None → показываем все записи компании (без ограничения по branch).

    Создание:
      - company берётся из request.user.company/owned_company
      - если есть активный филиал → он проставляется в branch;
      - если активного филиала нет → branch оставляем как есть (решает сериализатор/валидатор).

    Обновление:
      - company фиксируем;
      - branch НЕ трогаем (не переносим запись между филиалами случайно).
    """

    def _user(self):
        return getattr(self.request, "user", None)

    def _user_company(self):
        user = self._user()
        if not user or not getattr(user, "is_authenticated", False):
            return None

        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        if company:
            return company

        # fallback: если вдруг company хранится через user.branch
        branch = getattr(user, "branch", None)
        if branch is not None:
            return getattr(branch, "company", None)

        return None

    def _model_has_field(self, field_name: str) -> bool:
        qs = getattr(self, "queryset", None)
        model = getattr(qs, "model", None)
        if not model:
            return False
        return field_name in {f.name for f in model._meta.get_fields()}

    def _fixed_branch_from_user(self, company):
        """
        «Жёстко» назначенный филиал сотрудника:
          - user.branch, если принадлежит компании
          - единственный id в user.branch_ids, если список есть и в нём ровно 1 элемент

        Такой филиал пользователь поменять не может через ?branch.
        """
        user = self._user()
        if not user or not company:
            return None

        company_id = getattr(company, "id", None)

        # 1) user.branch как объект
        if hasattr(user, "branch"):
            b = getattr(user, "branch")
            if b and getattr(b, "company_id", None) == company_id:
                return b

        # 2) branch_ids: если ровно один филиал — считаем его фиксированным
        branch_ids = getattr(user, "branch_ids", None)
        if isinstance(branch_ids, (list, tuple)) and len(branch_ids) == 1:
            try:
                br = Branch.objects.get(id=branch_ids[0], company_id=company_id)
                return br
            except Branch.DoesNotExist:
                pass

        return None

    def _active_branch(self):
        """
        Определяем активный филиал:

          1) «жёсткий» филиал пользователя (branch или один branch_id);
          2) если жёсткого нет — пробуем ?branch=<uuid>, если филиал принадлежит компании;
          3) иначе None (режим по всей компании).
        """
        request = self.request
        company = self._user_company()
        if not company:
            setattr(request, "branch", None)
            return None

        company_id = getattr(company, "id", None)

        # 1) жёстко назначенный филиал
        fixed = self._fixed_branch_from_user(company)
        if fixed is not None:
            setattr(request, "branch", fixed)
            return fixed

        # 2) если жёсткого филиала нет — позволяем выбирать через ?branch
        branch_id = None
        if hasattr(request, "query_params"):
            branch_id = request.query_params.get("branch")
        elif hasattr(request, "GET"):
            branch_id = request.GET.get("branch")

        if branch_id:
            try:
                br = Branch.objects.get(id=branch_id, company_id=company_id)
                setattr(request, "branch", br)
                return br
            except (Branch.DoesNotExist, ValueError):
                # чужой/битый UUID — игнорируем
                pass

        # 3) никакого филиала → None (работаем по всей компании)
        setattr(request, "branch", None)
        return None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset.none()

        qs = super().get_queryset()
        company = self._user_company()
        if not company:
            return qs.none()

        qs = qs.filter(company=company)

        if self._model_has_field("branch"):
            active_branch = self._active_branch()  # None или Branch
            if active_branch is not None:
                qs = qs.filter(branch=active_branch)

        return qs

    def perform_create(self, serializer):
        company = self._user_company()
        if not company:
            raise PermissionDenied("У пользователя не задана компания.")

        kwargs = {"company": company}

        if self._model_has_field("branch"):
            active_branch = self._active_branch()
            if active_branch is not None:
                kwargs["branch"] = active_branch

        serializer.save(**kwargs)

    def perform_update(self, serializer):
        company = self._user_company()
        if not company:
            raise PermissionDenied("У пользователя не задана компания.")
        serializer.save(company=company)


# ==== ServiceCategory ====
class ServiceCategoryListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    queryset = ServiceCategory.objects.all()
    serializer_class = ServiceCategorySerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        f.name for f in ServiceCategory._meta.get_fields() if not f.is_relation or f.many_to_one
    ]
    search_fields = ["name"]
    ordering_fields = ["name", "is_active"]
    ordering = ["name"]


class ServiceCategoryRetrieveUpdateDestroyView(
    CompanyQuerysetMixin, generics.RetrieveUpdateDestroyAPIView
):
    queryset = ServiceCategory.objects.all()
    serializer_class = ServiceCategorySerializer
    permission_classes = [permissions.IsAuthenticated]


# ==== Service ====
class ServiceListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    queryset = Service.objects.prefetch_related("barbers").all()
    serializer_class = ServiceSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        f.name for f in Service._meta.get_fields() if not f.is_relation or f.many_to_one
    ]
    # category → теперь FK → ищем по названию категории
    search_fields = ["name", "category__name"]
    ordering_fields = ["name", "price", "is_active"]
    ordering = ["name"]

    def get_queryset(self):
        qs = super().get_queryset()
        barber_id = self.request.query_params.get("barber")
        if barber_id:
            qs = qs.annotate(_barber_count=Count("barbers")).filter(
                Q(_barber_count=0) | Q(barbers__id=barber_id)
            ).distinct()
        return qs

    def create(self, request, *args, **kwargs):
        try:
            return super().create(request, *args, **kwargs)
        except IntegrityError as e:
            msg = "Услуга с таким названием уже существует."
            s = str(e)
            if "uniq_service_name_global_per_company" in s:
                msg = "Глобальная услуга с таким названием уже существует в компании."
            elif "uniq_service_name_per_branch" in s:
                msg = "Услуга с таким названием уже существует в этом филиале."
            raise ValidationError({"name": msg})


class ServiceRetrieveUpdateDestroyView(
    CompanyQuerysetMixin, generics.RetrieveUpdateDestroyAPIView
):
    queryset = Service.objects.prefetch_related("barbers").all()
    serializer_class = ServiceSerializer
    permission_classes = [permissions.IsAuthenticated]


# ==== Client ====
class BarberClientPagination(PageNumberPagination):
    page_size = 5000
    page_size_query_param = "page_size"
    max_page_size = 10000
    # 30.10.2026: «Записи» на nurcrm.kg берут клиентов одной страницей page_size=1000 и дальше
    # не листают — у компаний с 1000+ клиентами остальные не находились при записи.
    # page_size от 1000 считаем просьбой «все» (до max_page_size).
    all_from = 1000

    def get_page_size(self, request):
        size = super().get_page_size(request)
        if size and size >= self.all_from:
            return self.max_page_size
        return size


class ClientListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    queryset = Client.objects.all()
    serializer_class = ClientSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = BarberClientPagination
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = [
        f.name for f in Client._meta.get_fields() if not f.is_relation or f.many_to_one
    ]
    ordering_fields = ["full_name", "created_at", "status"]
    ordering = ["-created_at"]

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        search_val = self.request.query_params.get("search")
        if search_val:
            search_val = search_val.strip()
            digits = "".join(c for c in search_val if c.isdigit())
            from django.db.models import Q
            q_filter = (
                Q(full_name__icontains=search_val)
                | Q(phone__icontains=search_val)
                | Q(email__icontains=search_val)
                | Q(notes__icontains=search_val)
            )
            if len(digits) >= 6:
                last9 = digits[-9:]
                q_filter |= Q(phone__endswith=last9)
            queryset = queryset.filter(q_filter)
        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.request.method == "POST":
            context["active_branch"] = self._active_branch()
        return context

    def create(self, request, *args, **kwargs):
        try:
            return super().create(request, *args, **kwargs)
        except IntegrityError as e:
            s = str(e)
            if "uniq_client_phone_global_per_company" in s or "uniq_client_phone_per_branch" in s:
                raise ValidationError(
                    {"phone": "Клиент с таким номером телефона уже существует в этой компании (или в выбранном филиале)."}
                )
            raise


class ClientRetrieveUpdateDestroyView(
    CompanyQuerysetMixin, generics.RetrieveUpdateDestroyAPIView
):
    queryset = Client.objects.all()
    serializer_class = ClientSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        # в карточке клиента нужен раздел "документы"
        if self.request.method == "GET":
            return ClientDetailSerializer
        return ClientSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            qs = (
                Appointment.objects
                .filter(client=instance)
                .select_related("barber")
                .prefetch_related(
                    Prefetch("appointment_services", queryset=AppointmentService.objects.order_by("position").select_related("service"))
                )
                .order_by("-start_at")
            )
            examples = []
            for a in qs[:3]:
                barber_name = None
                if a.barber:
                    if a.barber.first_name or a.barber.last_name:
                        barber_name = f"{a.barber.first_name} {a.barber.last_name}".strip()
                    else:
                        barber_name = a.barber.email
                service_names = [item.service.name for item in a.appointment_services.order_by("position")]
                examples.append(
                    {
                        "start_at": a.start_at,
                        "services": service_names,
                        "barber": barber_name,
                        "status": a.status,
                    }
                )
            return Response(
                {
                    "detail": "Нельзя удалить клиента: есть связанные записи (appointments).",
                    "appointments_count": qs.count(),
                    "examples": examples,
                    "solutions": [
                        "Измените статус клиента на 'inactive' или 'blacklist' вместо удаления.",
                        "Либо удалите/переназначьте связанные записи.",
                    ],
                },
                status=status.HTTP_409_CONFLICT,
            )


class ClientDocumentListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    queryset = ClientDocument.objects.select_related("client").all()
    serializer_class = ClientDocumentSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    ordering_fields = ["file_create_date"]
    ordering = ["-file_create_date"]

    def get_queryset(self):
        qs = super().get_queryset()
        company = self._user_company()
        if not company:
            return qs.none()
        client = generics.get_object_or_404(Client, pk=self.kwargs["pk"], company=company)
        return qs.filter(client=client)

    def perform_create(self, serializer):
        company = self._user_company()
        if not company:
            raise PermissionDenied("У пользователя не задана компания.")
        client = generics.get_object_or_404(Client, pk=self.kwargs["pk"], company=company)
        serializer.save(company=company, branch=client.branch, client=client)


class ClientDocumentRetrieveUpdateDestroyView(
    CompanyQuerysetMixin, generics.RetrieveUpdateDestroyAPIView
):
    queryset = ClientDocument.objects.select_related("client").all()
    serializer_class = ClientDocumentSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        qs = super().get_queryset()
        company = self._user_company()
        if not company:
            return qs.none()
        client = generics.get_object_or_404(Client, pk=self.kwargs["client_pk"], company=company)
        return qs.filter(client=client)

    def perform_update(self, serializer):
        """
        Разрешаем обновлять file и file_comment.
        company/branch/client фиксируем, чтобы документ нельзя было "перенести" на другого клиента/филиал.
        """
        company = self._user_company()
        if not company:
            raise PermissionDenied("У пользователя не задана компания.")
        instance = self.get_object()
        serializer.save(company=company, branch=instance.client.branch, client=instance.client)


class ClientVisitHistoryListView(CompanyQuerysetMixin, generics.ListAPIView):
    """
    История визитов конкретного клиента.

    GET /api/barbershop/clients/<uuid:pk>/visits/history/
      - по умолчанию возвращает только "исторические" статусы: completed/canceled/no_show
      - можно переопределить через ?status=... или ?status__in=...
      - можно ограничить по дате: ?start_at_from=...&start_at_to=...
    """

    queryset = (
        Appointment.objects
        .select_related("client", "barber", "company", "branch")
        .prefetch_related(
            Prefetch(
                "appointment_services",
                queryset=AppointmentService.objects.order_by("position").select_related("service__category"),
            )
        )
        .all()
    )
    serializer_class = AppointmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = ClientVisitHistoryFilter
    ordering_fields = ["start_at", "end_at", "status", "created_at"]
    ordering = ["-start_at"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return self.queryset.none()

        client = generics.get_object_or_404(Client, pk=self.kwargs["pk"], company=company)

        qs = self.queryset.filter(company=company, client=client)

        # Если выбран активный филиал — показываем и глобальные записи (branch=NULL)
        active_branch = self._active_branch()
        if active_branch is not None:
            qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))

        # Дефолт: история = завершённые/отменённые/не пришёл
        qp = getattr(self.request, "query_params", {})
        if "status" not in qp and "status__in" not in qp:
            qs = qs.filter(status__in=[
                Appointment.Status.COMPLETED,
                Appointment.Status.CANCELED,
                Appointment.Status.NO_SHOW,
            ])

        user = self.request.user
        if user and user.is_authenticated:
            role = str(getattr(user, "role", "") or "").strip().lower()
            is_owner_or_admin = (
                role in ("admin", "owner")
                or user.is_superuser
                or (company and getattr(company, "owner_id", None) == user.id)
                or getattr(user, "is_owner_or_admin", False)
            )
            if not is_owner_or_admin:
                qs = qs.exclude(status=Appointment.Status.DELETED)

        return qs


class VisitHistoryListView(CompanyQuerysetMixin, generics.ListAPIView):
    """
    Общая история визитов (по всем клиентам).

    GET /api/barbershop/visits/history/
      - по умолчанию возвращает только "исторические" статусы: completed/canceled/no_show
      - можно переопределить через ?status=... или ?status__in=...
      - можно ограничить по дате: ?start_at_from=...&start_at_to=...
      - можно фильтровать по мастеру/клиенту: ?barber=<uuid>&client=<uuid>
    """

    queryset = (
        Appointment.objects
        .select_related("client", "barber", "company", "branch")
        .all()
    )
    serializer_class = AppointmentHistoryRowSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = VisitHistoryFilter
    ordering_fields = ["start_at", "end_at", "status", "created_at"]
    ordering = ["-start_at"]

    def get_queryset(self):
        company = self._user_company()
        if not company:
            return self.queryset.none()

        qs = self.queryset.filter(company=company)

        # Если выбран активный филиал — показываем и глобальные записи (branch=NULL)
        active_branch = self._active_branch()
        if active_branch is not None:
            qs = qs.filter(Q(branch=active_branch) | Q(branch__isnull=True))

        # Дефолт: история = завершённые/отменённые/не пришёл
        qp = getattr(self.request, "query_params", {})
        if "status" not in qp and "status__in" not in qp:
            qs = qs.filter(status__in=[
                Appointment.Status.COMPLETED,
                Appointment.Status.CANCELED,
                Appointment.Status.NO_SHOW,
            ])

        user = self.request.user
        if user and user.is_authenticated:
            role = str(getattr(user, "role", "") or "").strip().lower()
            is_owner_or_admin = (
                role in ("admin", "owner")
                or user.is_superuser
                or (company and getattr(company, "owner_id", None) == user.id)
                or getattr(user, "is_owner_or_admin", False)
            )
            if not is_owner_or_admin:
                qs = qs.exclude(status=Appointment.Status.DELETED)

        return qs


def compute_appointment_expected_price(appt: Appointment) -> Decimal:
    """
    Сумма одной записи согласно правилам (§3.2):
    1. Если appointment.price задан и > 0 -> берётся он (уже с учётом ручной правки и скидки на фронте).
    2. Иначе — сумма базовых цен услуг из appointment.services на момент расчёта,
       с учётом appointment.discount (%).
    3. Если итог <= 0 -> 0.00.
    """
    price = getattr(appt, "price", None)
    if price is not None and price > 0:
        return Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    base_sum = Decimal("0.00")
    rel = getattr(appt, "appointment_services", None)
    if rel is not None and hasattr(rel, "all"):
        for item in rel.all():
            svc = getattr(item, "service", None)
            if svc and getattr(svc, "price", None):
                base_sum += Decimal(str(svc.price))
    elif hasattr(appt, "services"):
        for svc in appt.services.all():
            if svc.price:
                base_sum += Decimal(str(svc.price))

    if base_sum <= 0:
        return Decimal("0.00")

    discount = getattr(appt, "discount", None) or Decimal("0")
    if discount > 0:
        disc_dec = Decimal(str(discount))
        total = base_sum * (Decimal("1") - (disc_dec / Decimal("100")))
    else:
        total = base_sum

    if total <= 0:
        return Decimal("0.00")

    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class AppointmentSummaryView(CompanyQuerysetMixin, APIView):
    """
    Эндпоинт сводки ожидаемой суммы по записям (appointment-day-summary.md §2):
    GET /api/barbershop/appointments/summary/

    1. Scope day (календарь):
       - date: YYYY-MM-DD (обязательный)
       - barber: UUID (опционально)
       - status: string (опционально; по умолчанию booked, confirmed, completed)
       - Response: { "date": "...", "scope": "day", "expected_count": N, "expected_total": "..." }

    2. Scope deleted (раздел «Удалённые»):
       - scope: deleted (обязательный)
       - barber: UUID (опционально)
       - Response: { "scope": "deleted", "records_count": N, "expected_total": "..." }
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        company = self._user_company()
        if not company:
            return Response(
                {"detail": "Компания не найдена."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        scope = (request.query_params.get("scope") or "day").strip().lower()
        barber_id = request.query_params.get("barber")
        status_param = request.query_params.get("status")

        # Базовый кверисет с учетом компании и филиала
        qs = Appointment.objects.filter(company=company)
        active_branch = self._active_branch()
        if active_branch is not None:
            qs = qs.filter(branch=active_branch)

        if barber_id:
            qs = qs.filter(barber_id=barber_id)

        # Режим 1: Сводка удаленных записей (scope=deleted)
        if scope == "deleted":
            role = str(getattr(request.user, "role", "") or "").strip().lower()
            is_owner_or_admin = (
                role in ("admin", "owner")
                or request.user.is_superuser
                or (getattr(company, "owner_id", None) == request.user.id)
                or getattr(request.user, "is_owner_or_admin", False)
            )
            if not is_owner_or_admin:
                raise PermissionDenied("Просмотр сводки удаленных записей доступен только администраторам и владельцам.")

            qs = qs.filter(status=Appointment.Status.DELETED)

            appts = qs.select_related("client").prefetch_related(
                Prefetch(
                    "appointment_services",
                    queryset=AppointmentService.objects.select_related("service"),
                )
            )

            records_count = qs.count()
            total = Decimal("0.00")
            client_map = {}
            for appt in appts:
                appt_total = compute_appointment_expected_price(appt)
                total += appt_total

                client = getattr(appt, "client", None)
                if client:
                    client_id = str(client.id)
                    client_name = client.full_name or client.phone or "Клиент"
                else:
                    client_id = None
                    client_name = "Без клиента"

                client_key = client_id or "__none__"
                if client_key not in client_map:
                    client_map[client_key] = {
                        "client_id": client_id,
                        "client_name": client_name,
                        "records_count": 0,
                        "expected_total": Decimal("0.00"),
                    }
                client_map[client_key]["records_count"] += 1
                client_map[client_key]["expected_total"] += appt_total

            by_client = [
                {
                    "client_id": c["client_id"],
                    "client_name": c["client_name"],
                    "records_count": c["records_count"],
                    "expected_total": f"{c['expected_total']:.2f}",
                }
                for c in client_map.values()
            ]

            return Response({
                "scope": "deleted",
                "records_count": records_count,
                "expected_total": f"{total:.2f}",
                "by_client": by_client,
            }, status=status.HTTP_200_OK)

        # Режим 2: Сводка за день (scope=day)
        date_param = request.query_params.get("date")
        if not date_param:
            return Response(
                {"detail": "Параметр 'date' обязателен при scope=day в формате YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            target_date = datetime.strptime(date_param.strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return Response(
                {"detail": "Неверный формат параметра 'date'. Ожидается YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = qs.filter(start_at__date=target_date)

        # Никогда не включаем deleted в day-сводку
        qs = qs.exclude(status=Appointment.Status.DELETED)

        if status_param:
            qs = qs.filter(status=status_param.strip().lower())
        else:
            qs = qs.filter(
                status__in=[
                    Appointment.Status.BOOKED,
                    Appointment.Status.CONFIRMED,
                    Appointment.Status.COMPLETED,
                ]
            )

        appts = qs.select_related("client").prefetch_related(
            Prefetch(
                "appointment_services",
                queryset=AppointmentService.objects.select_related("service"),
            )
        )

        expected_count = qs.count()
        total = Decimal("0.00")
        client_map = {}
        for appt in appts:
            appt_total = compute_appointment_expected_price(appt)
            total += appt_total

            client = getattr(appt, "client", None)
            if client:
                client_id = str(client.id)
                client_name = client.full_name or client.phone or "Клиент"
            else:
                client_id = None
                client_name = "Без клиента"

            client_key = client_id or "__none__"
            if client_key not in client_map:
                client_map[client_key] = {
                    "client_id": client_id,
                    "client_name": client_name,
                    "records_count": 0,
                    "expected_total": Decimal("0.00"),
                }
            client_map[client_key]["records_count"] += 1
            client_map[client_key]["expected_total"] += appt_total

        by_client = [
            {
                "client_id": c["client_id"],
                "client_name": c["client_name"],
                "records_count": c["records_count"],
                "expected_total": f"{c['expected_total']:.2f}",
            }
            for c in client_map.values()
        ]

        return Response({
            "date": date_param.strip(),
            "scope": "day",
            "expected_count": expected_count,
            "expected_total": f"{total:.2f}",
            "by_client": by_client,
        }, status=status.HTTP_200_OK)


class AppointmentPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 1000


# ==== Appointment ====
class AppointmentListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    queryset = (
        Appointment.objects
        .select_related("client", "barber", "company", "branch")
        .prefetch_related(
            Prefetch(
                "appointment_services",
                queryset=AppointmentService.objects.order_by("position").select_related("service__category"),
            )
        )
        .all()
    )
    serializer_class = AppointmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = AppointmentPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        f.name for f in Appointment._meta.get_fields() if not f.is_relation or f.many_to_one
    ]
    search_fields = [
        "client__full_name",
        "barber__first_name",
        "barber__last_name",
        "comment",
    ]
    ordering_fields = ["start_at", "end_at", "status", "created_at"]
    ordering = ["-start_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user and user.is_authenticated:
            role = str(getattr(user, "role", "") or "").strip().lower()
            company = self._user_company()
            is_owner_or_admin = (
                role in ("admin", "owner")
                or user.is_superuser
                or (company and getattr(company, "owner_id", None) == user.id)
                or getattr(user, "is_owner_or_admin", False)
            )
            status_filter = self.request.query_params.get("status")
            if not is_owner_or_admin and status_filter != "deleted":
                qs = qs.exclude(status=Appointment.Status.DELETED)

        qp = self.request.query_params
        date_param = qp.get("date")
        if date_param:
            try:
                d = datetime.strptime(date_param.strip(), "%Y-%m-%d").date()
                qs = qs.filter(start_at__date=d)
            except (ValueError, TypeError):
                pass

        date_from = qp.get("date_from") or qp.get("start_date")
        if date_from:
            try:
                df = datetime.strptime(date_from.strip(), "%Y-%m-%d").date()
                qs = qs.filter(start_at__date__gte=df)
            except (ValueError, TypeError):
                pass

        date_to = qp.get("date_to") or qp.get("end_date")
        if date_to:
            try:
                dt = datetime.strptime(date_to.strip(), "%Y-%m-%d").date()
                qs = qs.filter(start_at__date__lte=dt)
            except (ValueError, TypeError):
                pass

        return qs

    def create(self, request, *args, **kwargs):
        """После создания перезагружаем запись с prefetch appointment_services, чтобы в ответе были services и services_names."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        instance = serializer.instance
        qs = self.get_queryset().filter(pk=instance.pk)
        instance = qs.first()
        if instance is None:
            instance = serializer.instance
        output_serializer = AppointmentSerializer(instance, context=self.get_serializer_context())
        headers = self.get_success_headers(output_serializer.data)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED, headers=headers)


class AppointmentRetrieveUpdateDestroyView(
    CompanyQuerysetMixin, generics.RetrieveUpdateDestroyAPIView
):
    queryset = (
        Appointment.objects
        .select_related("client", "barber", "company", "branch")
        .prefetch_related(
            Prefetch(
                "appointment_services",
                queryset=AppointmentService.objects.order_by("position").select_related("service__category"),
            )
        )
        .all()
    )
    serializer_class = AppointmentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user and user.is_authenticated:
            role = str(getattr(user, "role", "") or "").strip().lower()
            company = self._user_company()
            is_owner_or_admin = (
                role in ("admin", "owner")
                or user.is_superuser
                or (company and getattr(company, "owner_id", None) == user.id)
                or getattr(user, "is_owner_or_admin", False)
            )
            if not is_owner_or_admin:
                qs = qs.exclude(status=Appointment.Status.DELETED)
        return qs

    def update(self, request, *args, **kwargs):
        """После PATCH перезагружаем запись с prefetch appointment_services для полного ответа."""
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        new_status = request.data.get("status")

        user = request.user
        role = str(getattr(user, "role", "") or "").strip().lower()
        company = self._user_company()
        is_owner_or_admin = (
            role in ("admin", "owner")
            or user.is_superuser
            or (company and getattr(company, "owner_id", None) == user.id)
            or getattr(user, "is_owner_or_admin", False)
        )

        # 1. Проверка прав на установку status=deleted (§4.2)
        if new_status == Appointment.Status.DELETED and not is_owner_or_admin:
            raise PermissionDenied("У вас нет прав на удаление записей.")

        # 2. Проверка начислений зарплаты при удалении (§5.2)
        if new_status == Appointment.Status.DELETED:
            from .models import MasterSalaryAccrual
            has_paid_accruals = MasterSalaryAccrual.objects.filter(
                appointment=instance,
                status=MasterSalaryAccrual.Status.PAID
            ).exists()
            if has_paid_accruals:
                return Response(
                    {"detail": "Нельзя удалить запись с выплаченным начислением. Измените статус вручную."},
                    status=status.HTTP_409_CONFLICT
                )

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        updated_instance = serializer.instance
        qs = self.get_queryset().filter(pk=updated_instance.pk)
        instance = qs.first() or updated_instance
        output_serializer = AppointmentSerializer(instance, context=self.get_serializer_context())
        return Response(output_serializer.data, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        user = request.user
        role = str(getattr(user, "role", "") or "").strip().lower()
        company = self._user_company()
        is_owner_or_admin = (
            role in ("admin", "owner")
            or user.is_superuser
            or (company and getattr(company, "owner_id", None) == user.id)
            or getattr(user, "is_owner_or_admin", False)
        )
        if not is_owner_or_admin:
            raise PermissionDenied("Физическое удаление записей запрещено. Используйте мягкое удаление (status=deleted).")
        return super().destroy(request, *args, **kwargs)


class MyAppointmentListView(CompanyQuerysetMixin, generics.ListAPIView):
    """
    Эндпоинт "мои записи" — возвращает только записи, где текущий пользователь = barber.
    """

    queryset = (
        Appointment.objects
        .select_related("client", "barber", "company", "branch")
        .prefetch_related(
            Prefetch(
                "appointment_services",
                queryset=AppointmentService.objects.order_by("position").select_related("service__category"),
            )
        )
        .all()
    )
    serializer_class = AppointmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        f.name for f in Appointment._meta.get_fields() if not f.is_relation or f.many_to_one
    ]
    search_fields = [
        "client__full_name",
        "comment",
    ]
    ordering_fields = ["start_at", "end_at", "status", "created_at"]
    ordering = ["-start_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = getattr(self.request, "user", None)
        return qs.filter(barber=user).exclude(status=Appointment.Status.DELETED)


class MyAppointmentDetailView(CompanyQuerysetMixin, generics.RetrieveAPIView):
    """
    Эндпоинт "моя запись" — детальный просмотр только своих записей.
    """

    queryset = (
        Appointment.objects
        .select_related("client", "barber", "company", "branch")
        .prefetch_related(
            Prefetch(
                "appointment_services",
                queryset=AppointmentService.objects.order_by("position").select_related("service__category"),
            )
        )
        .all()
    )
    serializer_class = AppointmentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        user = getattr(self.request, "user", None)
        return qs.filter(barber=user).exclude(status=Appointment.Status.DELETED)


# ==== Folder ====
class FolderListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    queryset = Folder.objects.select_related("parent").all()
    serializer_class = FolderSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        f.name for f in Folder._meta.get_fields() if not f.is_relation or f.many_to_one
    ]
    search_fields = ["name", "parent__name"]
    ordering_fields = ["name"]
    ordering = ["name"]


class FolderRetrieveUpdateDestroyView(
    CompanyQuerysetMixin, generics.RetrieveUpdateDestroyAPIView
):
    queryset = Folder.objects.select_related("parent").all()
    serializer_class = FolderSerializer
    permission_classes = [permissions.IsAuthenticated]


def _analytics_date_range(request):
    """
    date_from/date_to в формате YYYY-MM-DD.
    По умолчанию: текущий месяц (с 1-го числа по сегодня).
    """
    today = timezone.localdate()

    raw_from = request.query_params.get("date_from") if hasattr(request, "query_params") else request.GET.get("date_from")
    raw_to = request.query_params.get("date_to") if hasattr(request, "query_params") else request.GET.get("date_to")

    date_from = parse_date(raw_from) if raw_from else today.replace(day=1)
    date_to = parse_date(raw_to) if raw_to else today

    if not date_from:
        raise ValidationError({"date_from": "Неверный формат даты. Используйте YYYY-MM-DD."})
    if not date_to:
        raise ValidationError({"date_to": "Неверный формат даты. Используйте YYYY-MM-DD."})
    if date_to < date_from:
        raise ValidationError({"date_to": "date_to должен быть >= date_from."})

    return date_from, date_to


def _can_view_barber_analytics(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    role = str(getattr(user, "role", "") or "").strip().lower()
    if role in {"admin", "owner"}:
        return True
    return bool(getattr(user, "can_view_barber_history", False) or getattr(user, "can_view_barber_records", False))


def _build_barber_analytics(base_qs, date_from, date_to, include_masters: bool):
    """
    base_qs уже должен быть скоупнут по company/branch.
    """
    qs = base_qs.filter(start_at__date__gte=date_from, start_at__date__lte=date_to)

    effective_price = ExpressionWrapper(
        F("price"),
        output_field=DecimalField(max_digits=14, decimal_places=6),
    )

    totals = qs.aggregate(
        appointments_total=Count("id"),
        appointments_completed=Count("id", filter=Q(status=Appointment.Status.COMPLETED)),
        appointments_canceled=Count("id", filter=Q(status=Appointment.Status.CANCELED)),
        appointments_no_show=Count("id", filter=Q(status=Appointment.Status.NO_SHOW)),
        revenue=Sum(effective_price, filter=Q(status=Appointment.Status.COMPLETED)),
        avg_ticket=Avg(effective_price, filter=Q(status=Appointment.Status.COMPLETED)),
    )

    totals["revenue"] = totals["revenue"] or 0

    services_rows = (
        Service.objects
        .filter(appointments__in=qs.filter(status=Appointment.Status.COMPLETED))
        .values("id", "name")
        .annotate(
            count=Count("appointments", distinct=True),
            revenue=Sum("price"),
        )
        .order_by("-revenue", "-count", "name")
    )

    masters_rows = []
    if include_masters:
        masters_rows = list(
            qs.filter(status=Appointment.Status.COMPLETED)
            .values("barber_id", "barber__first_name", "barber__last_name", "barber__email")
            .annotate(
                count=Count("id"),
                revenue=Sum(effective_price),
            )
            .order_by("-revenue", "-count")
        )
        for r in masters_rows:
            first = r.pop("barber__first_name") or ""
            last = r.pop("barber__last_name") or ""
            email = r.pop("barber__email") or ""
            full = f"{first} {last}".strip()
            r["master_id"] = r.pop("barber_id")
            r["master_name"] = full or email
            r["revenue"] = r["revenue"] or 0

    data = {
        "date_from": date_from,
        "date_to": date_to,
        "totals": {
            "appointments_total": totals["appointments_total"] or 0,
            "appointments_completed": totals["appointments_completed"] or 0,
            "appointments_canceled": totals["appointments_canceled"] or 0,
            "appointments_no_show": totals["appointments_no_show"] or 0,
            "revenue": totals["revenue"],
            "avg_ticket": totals["avg_ticket"],
        },
        "services": [
            {
                "service_id": r["id"],
                "name": r["name"],
                "count": r["count"] or 0,
                "revenue": r["revenue"] or 0,
            }
            for r in services_rows
        ],
    }
    if include_masters:
        data["masters"] = masters_rows
    return data


class BarberAnalyticsView(CompanyQuerysetMixin, generics.GenericAPIView):
    """
    Общая аналитика по барбершопу (по компании/филиалу).

    Query params:
      - date_from: YYYY-MM-DD (опционально)
      - date_to: YYYY-MM-DD (опционально)
      - branch: UUID (опционально, учитывается CompanyQuerysetMixin)
    """

    queryset = (
        Appointment.objects
        .select_related("barber", "client")
        .prefetch_related(
            Prefetch(
                "appointment_services",
                queryset=AppointmentService.objects.order_by("position").select_related("service__category"),
            )
        )
        .all()
    )
    serializer_class = BarberAnalyticsResponseSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not _can_view_barber_analytics(getattr(request, "user", None)):
            raise PermissionDenied("Нет доступа к аналитике барбершопа.")
        date_from, date_to = _analytics_date_range(request)
        data = _build_barber_analytics(self.get_queryset(), date_from, date_to, include_masters=True)
        return Response(self.serializer_class(data).data)


class MyBarberAnalyticsView(CompanyQuerysetMixin, generics.GenericAPIView):
    """
    Аналитика мастера (только по своим записям).

    Query params:
      - date_from: YYYY-MM-DD (опционально)
      - date_to: YYYY-MM-DD (опционально)
      - branch: UUID (опционально, учитывается CompanyQuerysetMixin)
    """

    queryset = (
        Appointment.objects
        .select_related("barber", "client")
        .prefetch_related(
            Prefetch(
                "appointment_services",
                queryset=AppointmentService.objects.order_by("position").select_related("service__category"),
            )
        )
        .all()
    )
    serializer_class = BarberAnalyticsResponseSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        date_from, date_to = _analytics_date_range(request)
        user = getattr(request, "user", None)
        data = _build_barber_analytics(self.get_queryset().filter(barber=user), date_from, date_to, include_masters=False)
        return Response(self.serializer_class(data).data)


# ==== Document ====
class DocumentListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    queryset = Document.objects.select_related("folder").all()
    serializer_class = DocumentSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = DocumentFilter
    search_fields = ["name", "folder__name", "file"]
    ordering_fields = ["name", "created_at", "updated_at"]
    ordering = ["name"]


class DocumentRetrieveUpdateDestroyView(
    CompanyQuerysetMixin, generics.RetrieveUpdateDestroyAPIView
):
    queryset = Document.objects.select_related("folder").all()
    serializer_class = DocumentSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]



@read_only_guard("can_edit_salary")
class PayoutListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    """
    GET  /api/barber/payouts/        – список выплат (по компании/филиалу, с фильтрами)
    POST /api/barber/payouts/        – создать выплату + авторасчёт суммы
    """

    queryset = (
        Payout.objects
        .select_related("company", "branch", "barber")
        .all()
    )
    serializer_class = PayoutSerializer
    permission_classes = [permissions.IsAuthenticated]

    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        f.name for f in Payout._meta.get_fields() if not f.is_relation or f.many_to_one
    ]
    search_fields = [
        "period",
        "comment",
        "barber__first_name",
        "barber__last_name",
        "barber__email",
    ]
    ordering_fields = ["created_at", "period", "payout_amount"]
    ordering = ["-created_at"]


@read_only_guard("can_edit_salary")
class PayoutRetrieveUpdateDestroyView(
    CompanyQuerysetMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    """
    GET    /api/barber/payouts/<uuid:pk>/   – одна выплата
    PATCH  /api/barber/payouts/<uuid:pk>/   – изменить (например, комментарий/ставку)
    DELETE /api/barber/payouts/<uuid:pk>/   – удалить
    """

    queryset = (
        Payout.objects
        .select_related("company", "branch", "barber")
        .all()
    )
    serializer_class = PayoutSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    

class ServiceCategoryListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    """
    GET  /api/barber/service-categories/      – список категорий (по компании/филиалу)
    POST /api/barber/service-categories/      – создать категорию услуги
    """
    queryset = ServiceCategory.objects.all()
    serializer_class = ServiceCategorySerializer
    permission_classes = [permissions.IsAuthenticated]

    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        f.name for f in ServiceCategory._meta.get_fields() if not f.is_relation or f.many_to_one
    ]
    search_fields = ["name"]
    ordering_fields = ["name", "is_active", "created_at"] if hasattr(ServiceCategory, "created_at") else ["name", "is_active"]
    ordering = ["name"]


class ServiceCategoryRetrieveUpdateDestroyView(
    CompanyQuerysetMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    """
    GET    /api/barber/service-categories/<uuid:pk>/   – одна категория
    PATCH  /api/barber/service-categories/<uuid:pk>/   – обновить
    DELETE /api/barber/service-categories/<uuid:pk>/   – удалить
    """
    queryset = ServiceCategory.objects.all()
    serializer_class = ServiceCategorySerializer
    permission_classes = [permissions.IsAuthenticated]

class ProductSalePayoutListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    """
    GET  /api/barber/product-sale-payouts/  – список начислений с процента от товара
    POST /api/barber/product-sale-payouts/  – создать одно начисление (по форме модалки)
    """

    queryset = (
        ProductSalePayout.objects
        .select_related("company", "branch", "product", "employee")
        .all()
    )
    serializer_class = ProductSalePayoutSerializer
    permission_classes = [permissions.IsAuthenticated]

    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        f.name for f in ProductSalePayout._meta.get_fields()
        if not f.is_relation or f.many_to_one
    ]
    search_fields = [
        "product__name",
        "employee__first_name",
        "employee__last_name",
        "employee__email",
    ]
    ordering_fields = ["created_at", "price", "payout_amount", "percent"]
    ordering = ["-created_at"]


class ProductSalePayoutRetrieveUpdateDestroyView(
    CompanyQuerysetMixin,
    generics.RetrieveUpdateDestroyAPIView,
):
    """
    GET    /api/barber/product-sale-payouts/<uuid:pk>/   – одно начисление
    PATCH  /api/barber/product-sale-payouts/<uuid:pk>/   – можно править процент/цену/комментарий
    DELETE /api/barber/product-sale-payouts/<uuid:pk>/   – удалить начисление
    """

    queryset = (
        ProductSalePayout.objects
        .select_related("company", "branch", "product", "employee")
        .all()
    )
    serializer_class = ProductSalePayoutSerializer
    permission_classes = [permissions.IsAuthenticated]    
    
class PayoutSaleListCreateView(
    CompanyQuerysetMixin,
    generics.ListCreateAPIView
):

    queryset = PayoutSale.objects.all()
    serializer_class = PayoutSaleSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]

    filterset_fields = [
        f.name for f in PayoutSale._meta.get_fields() if not f.is_relation or f.many_to_one
    ]
    search_fields = ["period"]
    ordering_fields = ["period", "total", "old_total_fund", "new_total_fund"]
    ordering = ["-period"]

class PayoutSaleRetrieveUpdateDestroyView(
    CompanyQuerysetMixin,
    generics.RetrieveUpdateDestroyAPIView
):
    queryset = PayoutSale.objects.all()
    serializer_class = PayoutSaleSerializer
    permission_classes = [permissions.IsAuthenticated]


# ===========================
# OnlineBooking - Публичный эндпоинт для создания заявки
# ===========================
class OnlineBookingPublicCreateView(generics.CreateAPIView):
    """
    Публичный эндпоинт для создания заявки на онлайн запись.
    Доступен без авторизации, но требует slug компании в URL.
    URL: /api/barbershop/public/{company_slug}/bookings/

    Поддерживает два формата запроса:
    - одиночная бронь (старый формат): {services, master_id, master_name, date,
      time_start, time_end, client_*} → 201 с объектом заявки;
    - multi-master (новый формат): {client_*, date, assignments: [...]} → записи создаются
      атомарно, ответ 201 {"bookings": [{"id", "master_id"}, ...]}.
    """
    serializer_class = OnlineBookingCreateSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    # Рабочий день (согласовано с PublicMasterScheduleView)
    WORK_START = time(9, 0)
    WORK_END = time(21, 0)

    def get_company(self):
        """Получаем компанию по slug из URL"""
        slug = self.kwargs.get('company_slug')
        try:
            return Company.objects.get(slug=slug)
        except Company.DoesNotExist:
            raise ValidationError({'detail': 'Компания не найдена'})

    def perform_create(self, serializer):
        """Создаем заявку с автоматическим определением компании (одиночная бронь)"""
        company = self.get_company()
        serializer.save(
            company=company,
            status=OnlineBooking.Status.NEW
        )

    def create(self, request, *args, **kwargs):
        # Multi-master формат — массив назначений.
        if isinstance(request.data, dict) and request.data.get('assignments'):
            return self._create_multi(request)
        return super().create(request, *args, **kwargs)

    # ---- multi-master ----
    def _master_busy(self, company, master_id, booking_date, t_start, t_end):
        """Проверка занятости мастера: существующие онлайн-заявки и записи (appointments)."""
        ob_overlap = OnlineBooking.objects.filter(
            company=company,
            master_id=master_id,
            date=booking_date,
            status__in=[OnlineBooking.Status.NEW, OnlineBooking.Status.CONFIRMED],
            time_start__lt=t_end,
            time_end__gt=t_start,
        ).exists()
        if ob_overlap:
            return True

        appts = Appointment.objects.filter(
            company=company,
            barber_id=master_id,
            start_at__date=booking_date,
            status__in=[Appointment.Status.BOOKED, Appointment.Status.CONFIRMED],
        ).values_list('start_at', 'end_at')
        for start_at, end_at in appts:
            s_t = timezone.localtime(start_at).time() if timezone.is_aware(start_at) else start_at.time()
            e_t = timezone.localtime(end_at).time() if timezone.is_aware(end_at) else end_at.time()
            if s_t < t_end and e_t > t_start:
                return True
        return False

    def _create_multi(self, request):
        from apps.users.models import User

        company = self.get_company()
        serializer = OnlineBookingMultiCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        assignments = data['assignments']
        booking_date = data['date']

        errors = []
        prepared = []
        seen_master_slots = {}  # master_id -> list of (start, end) внутри одного запроса

        for idx, assignment in enumerate(assignments):
            master_id = assignment['master_id']
            t_start = assignment['time_start']
            t_end = assignment['time_end']

            # 1) Рабочий день
            if t_start < self.WORK_START or t_end > self.WORK_END:
                errors.append({'index': idx, 'detail': 'Время вне рабочего дня (09:00–21:00).'})
                continue

            # 2) Мастер существует и принадлежит компании
            master = User.objects.filter(id=master_id, company=company, is_active=True).first()
            if master is None:
                errors.append({'index': idx, 'detail': 'Мастер не найден.'})
                continue

            # 3) Мастер умеет назначенные услуги (если у услуги заданы мастера)
            skill_error = None
            for svc in assignment['services']:
                service = Service.objects.filter(
                    id=svc['service_id'], company=company, is_active=True
                ).prefetch_related('barbers').first()
                if service is None:
                    skill_error = f"Услуга {svc['service_id']} не найдена."
                    break
                barber_ids = set(service.barbers.values_list('id', flat=True))
                if barber_ids and master.id not in barber_ids:
                    skill_error = f"Мастер не выполняет услугу «{service.name}»."
                    break
            if skill_error:
                errors.append({'index': idx, 'detail': skill_error})
                continue

            # 4) Слот свободен (существующие брони/записи)
            if self._master_busy(company, master.id, booking_date, t_start, t_end):
                errors.append({'index': idx, 'detail': 'Слот мастера уже занят.'})
                continue

            # 5) Пересечения внутри самого запроса (тот же мастер дважды)
            intra_overlap = any(
                s < t_end and e > t_start for (s, e) in seen_master_slots.get(master.id, [])
            )
            if intra_overlap:
                errors.append({'index': idx, 'detail': 'Пересечение слотов мастера в запросе.'})
                continue
            seen_master_slots.setdefault(master.id, []).append((t_start, t_end))

            services_json = [
                {
                    'service_id': str(svc['service_id']),
                    'title': svc.get('title') or '',
                    'price': str(svc.get('price') or 0),
                    'duration_min': int(svc.get('duration_min') or 0),
                }
                for svc in assignment['services']
            ]
            master_name = f"{master.first_name or ''} {master.last_name or ''}".strip() or master.email
            prepared.append(OnlineBooking(
                company=company,
                services=services_json,
                master_id=master.id,
                master_name=master_name,
                date=booking_date,
                time_start=t_start,
                time_end=t_end,
                client_name=data['client_name'],
                client_phone=data['client_phone'],
                client_comment=data.get('client_comment'),
                payment_method=data.get('payment_method', OnlineBooking.PaymentMethod.CASH),
                status=OnlineBooking.Status.NEW,
            ))

        if errors:
            raise ValidationError({'assignments': errors})

        created = []
        with transaction.atomic():
            for booking in prepared:
                booking.save()
                created.append(booking)

        return Response(
            {'bookings': [{'id': str(b.id), 'master_id': str(b.master_id)} for b in created]},
            status=status.HTTP_201_CREATED,
        )


# ===========================
# OnlineBooking - Защищенные эндпоинты для управления заявками
# ===========================
class OnlineBookingListView(CompanyQuerysetMixin, generics.ListAPIView):
    """
    Список заявок на онлайн запись (только для авторизованных пользователей компании).
    GET /api/barbershop/bookings/
    """
    queryset = OnlineBooking.objects.select_related('company', 'branch').all()
    serializer_class = OnlineBookingSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'date', 'payment_method', 'branch']
    search_fields = ['client_name', 'client_phone']
    ordering_fields = ['created_at', 'date', 'time_start']
    ordering = ['-created_at']


class OnlineBookingDetailView(CompanyQuerysetMixin, generics.RetrieveAPIView):
    """
    Детали заявки.
    GET /api/barbershop/bookings/{pk}/
    """
    queryset = OnlineBooking.objects.select_related('company', 'branch').all()
    serializer_class = OnlineBookingSerializer
    permission_classes = [permissions.IsAuthenticated]


class OnlineBookingStatusUpdateView(CompanyQuerysetMixin, generics.UpdateAPIView):
    """
    Изменение статуса заявки.
    PATCH /api/barbershop/bookings/{pk}/status/
    """
    queryset = OnlineBooking.objects.select_related('company', 'branch').all()
    serializer_class = OnlineBookingStatusUpdateSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        return OnlineBookingStatusUpdateSerializer


# ===========================
# Публичные эндпоинты для онлайн-записи
# ===========================
class PublicServicesListView(generics.ListAPIView):
    """
    Публичный эндпоинт для получения услуг компании.
    Доступен без авторизации, требует slug компании в URL.
    URL: /api/barbershop/public/{company_slug}/services/

    Возвращает услуги вместе со связями «услуга → мастера» в нормализованном виде
    (без дублирования сотрудников):
        {
          "employees": { "<id>": {"id", "name", "avatar"}, ... },
          "services":  [ {"id", "name", "category", "category_name",
                          "price", "time", "employeeIds": [...]}, ... ]
        }

    Query params:
        - branch: UUID филиала (опционально)
    """
    serializer_class = PublicServiceSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    pagination_class = None

    def get_company(self):
        """Получаем компанию по slug из URL"""
        slug = self.kwargs.get('company_slug')
        try:
            return Company.objects.get(slug=slug)
        except Company.DoesNotExist:
            raise ValidationError({'detail': 'Компания не найдена'})

    def get_queryset(self):
        from apps.users.models import User

        company = self.get_company()
        # Только активные мастера в связях услуг (минимальный набор полей).
        active_barbers = User.objects.filter(is_active=True).only(
            'id', 'first_name', 'last_name', 'email', 'avatar'
        )
        qs = Service.objects.filter(
            company=company,
            is_active=True
        ).select_related('category').prefetch_related(
            Prefetch('barbers', queryset=active_barbers)
        ).order_by('category__name', 'name')

        # Фильтрация по филиалу (если указан)
        branch_id = self.request.query_params.get('branch')
        if branch_id:
            try:
                branch = Branch.objects.get(id=branch_id, company=company)
                # Показываем глобальные услуги + услуги филиала
                qs = qs.filter(Q(branch__isnull=True) | Q(branch=branch))
            except (Branch.DoesNotExist, ValueError):
                # Если филиал не найден - показываем только глобальные
                qs = qs.filter(branch__isnull=True)

        return qs

    @staticmethod
    def _master_name(user):
        full = f"{user.first_name or ''} {user.last_name or ''}".strip()
        return full or getattr(user, 'email', '') or str(user.id)

    def list(self, request, *args, **kwargs):
        """Нормализованный ответ: employees (словарь) + services (с employeeIds)."""
        services = list(self.get_queryset())

        employees = {}
        services_payload = []
        for service in services:
            employee_ids = []
            for barber in service.barbers.all():
                key = str(barber.id)
                if key not in employees:
                    employees[key] = {
                        'id': key,
                        'name': self._master_name(barber),
                        'avatar': barber.avatar or None,
                    }
                employee_ids.append(key)

            services_payload.append({
                'id': str(service.id),
                'name': service.name,
                'category': str(service.category_id) if service.category_id else None,
                'category_name': service.category.name if service.category else None,
                'price': str(service.price),
                'time': service.time,
                'employeeIds': employee_ids,
            })

        return Response({'employees': employees, 'services': services_payload})


class PublicServiceCategoriesListView(generics.ListAPIView):
    """
    Публичный эндпоинт для получения категорий услуг с услугами.
    Доступен без авторизации, требует slug компании в URL.
    URL: /api/barbershop/public/{company_slug}/service-categories/
    
    Query params:
        - branch: UUID филиала (опционально)
    """
    serializer_class = PublicServiceCategorySerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    
    def get_company(self):
        """Получаем компанию по slug из URL"""
        slug = self.kwargs.get('company_slug')
        try:
            return Company.objects.get(slug=slug)
        except Company.DoesNotExist:
            raise ValidationError({'detail': 'Компания не найдена'})
    
    def get_queryset(self):
        company = self.get_company()
        
        # Фильтрация по филиалу
        branch_id = self.request.query_params.get('branch')
        branch_filter = Q(branch__isnull=True)
        services_branch_filter = Q(branch__isnull=True)
        
        if branch_id:
            try:
                branch = Branch.objects.get(id=branch_id, company=company)
                branch_filter = Q(branch__isnull=True) | Q(branch=branch)
                services_branch_filter = Q(branch__isnull=True) | Q(branch=branch)
            except (Branch.DoesNotExist, ValueError):
                pass
        
        # Получаем категории с активными услугами
        qs = ServiceCategory.objects.filter(
            company=company,
            is_active=True
        ).filter(branch_filter).prefetch_related(
            Prefetch(
                'services',
                queryset=Service.objects.filter(
                    is_active=True,
                    company=company
                ).filter(services_branch_filter).prefetch_related('barbers').order_by('name')
            )
        ).order_by('name')
        
        return qs


class PublicMastersListView(generics.ListAPIView):
    """
    Публичный эндпоинт для получения мастеров компании.
    Доступен без авторизации, требует slug компании в URL.
    URL: /api/barbershop/public/{company_slug}/masters/
    
    Мастера определяются по одному из критериев:
    1. У пользователя есть записи (appointments) как barber
    2. У пользователя установлен флаг can_view_barber_records=True
    3. Роль пользователя = 'barber' или 'master'
    
    Query params:
        - branch: UUID филиала (опционально)
    """
    serializer_class = PublicMasterSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    
    def get_company(self):
        """Получаем компанию по slug из URL"""
        slug = self.kwargs.get('company_slug')
        try:
            return Company.objects.get(slug=slug)
        except Company.DoesNotExist:
            raise ValidationError({'detail': 'Компания не найдена'})
    
    def get_queryset(self):
        from apps.users.models import User
         
        company = self.get_company()
         
        # Получаем ID пользователей, у которых есть записи как мастер
        masters_with_appointments = Appointment.objects.filter(
            company=company
        ).values_list('barber_id', flat=True).distinct()
         
        # Получаем сотрудников компании, которые считаются мастерами по любому критерию:
        # 1) Есть записи как мастер (barber)
        # 2) У сотрудника есть доступ к модулям барбершопа (обычно ставится мастерам/админу)
        # 3) Кастомная роль / роль в филиале указывает на мастера
        qs = User.objects.filter(
            company=company,
            is_active=True
        ).filter(
            Q(id__in=masters_with_appointments) |
            Q(can_view_barber_records=True) |
            Q(can_view_barber_services=True) |
            Q(can_view_barber_clients=True) |
            Q(can_view_barber_history=True) |
            Q(custom_role__name__in=["barber", "master", "Барбер", "Мастер", "барбер", "мастер"]) |
            Q(branch_memberships__role__in=["barber", "master", "Барбер", "Мастер", "барбер", "мастер"])
        ).distinct().order_by('first_name', 'last_name')
         
        # Фильтрация по филиалу (если указан)
        branch_id = self.request.query_params.get('branch')
        if branch_id:
            try:
                branch = Branch.objects.get(id=branch_id, company=company)
                # Показываем мастеров, привязанных к филиалу + "глобальных" (без филиалов)
                qs = qs.filter(Q(branches=branch) | Q(branches__isnull=True)).distinct()
            except (Branch.DoesNotExist, ValueError):
                pass
         
        return qs


class PublicMasterScheduleView(generics.GenericAPIView):
    """
    Публичный эндпоинт для получения занятых слотов мастера.
    URL: /api/barbershop/public/{company_slug}/masters/{master_id}/schedule/
    
    Query params:
        - date: дата в формате YYYY-MM-DD (обязательно)
        - days: количество дней вперед (по умолчанию 1, максимум 14)
    
    Возвращает занятые временные слоты мастера на указанную дату/период.
    """
    serializer_class = PublicMasterScheduleSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    
    def get_company(self):
        slug = self.kwargs.get('company_slug')
        try:
            return Company.objects.get(slug=slug)
        except Company.DoesNotExist:
            raise ValidationError({'detail': 'Компания не найдена'})
    
    def get(self, request, *args, **kwargs):
        from datetime import datetime, timedelta
        from apps.users.models import User
        
        company = self.get_company()
        master_id = self.kwargs.get('master_id')
        
        # Проверяем, что мастер существует и принадлежит компании
        try:
            master = User.objects.get(id=master_id, company=company, is_active=True)
        except User.DoesNotExist:
            raise ValidationError({'detail': 'Мастер не найден'})
        
        # Получаем параметры запроса
        date_str = request.query_params.get('date')
        if not date_str:
            raise ValidationError({'date': 'Параметр date обязателен (формат: YYYY-MM-DD)'})
        
        try:
            start_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            raise ValidationError({'date': 'Неверный формат даты. Используйте YYYY-MM-DD'})
        
        # Количество дней
        try:
            days = min(int(request.query_params.get('days', 1)), 14)
        except ValueError:
            days = 1
        
        end_date = start_date + timedelta(days=days)
        
        # Получаем занятые слоты мастера
        # Показываем только активные записи (booked, confirmed)
        busy_slots = Appointment.objects.filter(
            company=company,
            barber=master,
            start_at__date__gte=start_date,
            start_at__date__lt=end_date,
            status__in=[Appointment.Status.BOOKED, Appointment.Status.CONFIRMED]
        ).order_by('start_at').values('id', 'start_at', 'end_at')
        
        # Формируем ответ
        result = {
            'master_id': str(master.id),
            'master_name': f"{master.first_name or ''} {master.last_name or ''}".strip() or master.email,
            'date_from': start_date.isoformat(),
            'date_to': (end_date - timedelta(days=1)).isoformat(),
            'busy_slots': list(busy_slots),
            'work_start': '09:00',
            'work_end': '21:00'
        }
        
        return Response(result)


class PublicMastersAvailabilityView(generics.GenericAPIView):
    """
    Публичный эндпоинт для получения доступности всех мастеров на дату.
    URL: /api/barbershop/public/{company_slug}/masters/availability/
    
    Query params:
        - date: дата в формате YYYY-MM-DD (обязательно)
        - branch: UUID филиала (опционально)
    
    Возвращает список мастеров с их занятыми слотами на указанную дату.
    """
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    
    def get_company(self):
        slug = self.kwargs.get('company_slug')
        try:
            return Company.objects.get(slug=slug)
        except Company.DoesNotExist:
            raise ValidationError({'detail': 'Компания не найдена'})
    
    def get(self, request, *args, **kwargs):
        from datetime import datetime, timedelta
        from apps.users.models import User
        
        company = self.get_company()
        
        # Получаем дату
        date_str = request.query_params.get('date')
        if not date_str:
            raise ValidationError({'date': 'Параметр date обязателен (формат: YYYY-MM-DD)'})
        
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            raise ValidationError({'date': 'Неверный формат даты. Используйте YYYY-MM-DD'})
        
        # Получаем мастеров
        masters_with_appointments = Appointment.objects.filter(
            company=company
        ).values_list('barber_id', flat=True).distinct()
        
        masters_qs = User.objects.filter(
            company=company,
            is_active=True
        ).filter(
            Q(id__in=masters_with_appointments) |
            Q(can_view_barber_records=True) |
            Q(can_view_barber_services=True) |
            Q(can_view_barber_clients=True) |
            Q(can_view_barber_history=True) |
            Q(custom_role__name__in=["barber", "master", "Барбер", "Мастер", "барбер", "мастер"]) |
            Q(branch_memberships__role__in=["barber", "master", "Барбер", "Мастер", "барбер", "мастер"])
        ).distinct()
         
        # Фильтрация по филиалу
        branch_id = request.query_params.get('branch')
        if branch_id:
            try:
                branch = Branch.objects.get(id=branch_id, company=company)
                masters_qs = masters_qs.filter(Q(branches=branch) | Q(branches__isnull=True)).distinct()
            except (Branch.DoesNotExist, ValueError):
                pass
        
        # Получаем все занятые слоты на дату
        busy_appointments = Appointment.objects.filter(
            company=company,
            start_at__date=target_date,
            status__in=[Appointment.Status.BOOKED, Appointment.Status.CONFIRMED]
        ).select_related('barber').order_by('start_at')
        
        # Группируем по мастерам
        busy_by_master = {}
        for apt in busy_appointments:
            if apt.barber_id not in busy_by_master:
                busy_by_master[apt.barber_id] = []
            busy_by_master[apt.barber_id].append({
                'id': str(apt.id),
                'start_at': apt.start_at.isoformat(),
                'end_at': apt.end_at.isoformat()
            })
        
        # Формируем результат
        result = []
        for master in masters_qs:
            result.append({
                'master_id': str(master.id),
                'master_name': f"{master.first_name or ''} {master.last_name or ''}".strip() or master.email,
                'avatar': master.avatar,
                'date': target_date.isoformat(),
                'busy_slots': busy_by_master.get(master.id, []),
                'work_start': '09:00',
                'work_end': '21:00'
            })
        
        return Response({
            'date': target_date.isoformat(),
            'masters': result
        })


# ===========================
# Master Salary Views (salary.md)
# ===========================
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from django.contrib.auth import get_user_model
from decimal import ROUND_HALF_UP

from .models import ServiceSalaryRate, MasterSalaryPayout, MasterSalaryAccrual
from .serializers import (
    ServiceSalaryRateSerializer, MasterSalaryPayoutSerializer, MasterSalaryAccrualSerializer
)

class IsOwnerOrAdmin(permissions.BasePermission):
    """
    Разрешает доступ только владельцам компании (owner) или администраторам (admin).
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        role = str(getattr(request.user, "role", "") or "").strip().lower()
        return role in ["admin", "owner"]


class ServiceSalaryRateListView(CompanyQuerysetMixin, generics.ListAPIView):
    """
    GET /api/barbershop/salary/rates/
    Возвращает все услуги компании (включая неактивные) с их процентными ставками.
    """
    queryset = Service.objects.select_related("salary_rate").all()
    serializer_class = ServiceSalaryRateSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrAdmin]

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(name__icontains=search)
        return qs


class ServiceSalaryRateUpdateView(APIView):
    """
    PUT /api/barbershop/salary/rates/{service_id}/
    Изменяет процентную ставку вознаграждения для конкретной услуги.
    """
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrAdmin]

    def put(self, request, service_id):
        user = request.user
        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        if not company:
            return Response({"detail": "У пользователя не задана компания."}, status=status.HTTP_400_BAD_REQUEST)

        service = get_object_or_404(Service, id=service_id, company=company)

        percent_str = request.data.get("percent")
        if percent_str is None:
            return Response({"percent": ["Обязательное поле."]}, status=status.HTTP_400_BAD_REQUEST)

        try:
            percent = Decimal(str(percent_str))
        except (ValueError, TypeError):
            return Response({"percent": ["Процент должен быть числом."]}, status=status.HTTP_400_BAD_REQUEST)

        if percent < 0 or percent > 100:
            return Response({"percent": ["Процент должен быть от 0 до 100."]}, status=status.HTTP_400_BAD_REQUEST)

        rate, created = ServiceSalaryRate.objects.get_or_create(
            company=company,
            service=service,
            defaults={"percent": percent, "updated_by": user}
        )
        if not created:
            rate.percent = percent
            rate.updated_by = user
            rate.save(update_fields=["percent", "updated_by", "updated_at"])

        serializer = ServiceSalaryRateSerializer(service)
        return Response(serializer.data, status=status.HTTP_200_OK)


class MasterSalaryAccrualListView(CompanyQuerysetMixin, generics.ListAPIView):
    """
    GET /api/barbershop/salary/accruals/
    История начислений. Мастер видит только свои (при наличии can_view_salary).
    """
    serializer_class = MasterSalaryAccrualSerializer
    queryset = MasterSalaryAccrual.objects.select_related("master", "service", "appointment").all()
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        role = str(getattr(user, "role", "") or "").strip().lower()
        is_admin_or_owner = (role in ["admin", "owner"]) or user.is_superuser

        if not is_admin_or_owner and not getattr(user, "can_view_salary", False):
            raise PermissionDenied("У вас нет доступа к разделу Зарплата.")

        qs = super().get_queryset()

        if not is_admin_or_owner:
            qs = qs.filter(master=user)
        else:
            master_id = self.request.query_params.get("master")
            if master_id:
                qs = qs.filter(master_id=master_id)

        # Фильтрация по датам
        date_from = self.request.query_params.get("date_from")
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)

        date_to = self.request.query_params.get("date_to")
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        # Фильтрация по услуге
        service_id = self.request.query_params.get("service")
        if service_id:
            qs = qs.filter(service_id=service_id)

        # Фильтрация по статусу
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        # Поиск по номеру записи (в нашей денормализации это UUID записи)
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(appointment_id__icontains=search)

        return qs


class MasterSalarySummaryView(APIView):
    """
    GET /api/barbershop/salary/summary/
    Сводка (totals + by_master).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        role = str(getattr(user, "role", "") or "").strip().lower()
        is_admin_or_owner = (role in ["admin", "owner"]) or user.is_superuser

        if not is_admin_or_owner and not getattr(user, "can_view_salary", False):
            return Response({"detail": "У вас нет доступа к разделу Зарплата."}, status=status.HTTP_403_FORBIDDEN)

        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        if not company:
            return Response({"detail": "У пользователя не задана компания."}, status=status.HTTP_400_BAD_REQUEST)

        # Собираем параметры
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        service_id = request.query_params.get("service")

        if not is_admin_or_owner:
            master_param = user.id
        else:
            master_param = request.query_params.get("master")

        # Базовые квери
        accruals_period = MasterSalaryAccrual.objects.filter(company=company)
        payouts_period = MasterSalaryPayout.objects.filter(company=company)
        accruals_balance = MasterSalaryAccrual.objects.filter(company=company, status=MasterSalaryAccrual.Status.ACCRUED)

        if master_param:
            accruals_period = accruals_period.filter(master_id=master_param)
            payouts_period = payouts_period.filter(master_id=master_param)
            accruals_balance = accruals_balance.filter(master_id=master_param)

        if service_id:
            accruals_period = accruals_period.filter(service_id=service_id)
            accruals_balance = accruals_balance.filter(service_id=service_id)

        if date_from:
            accruals_period = accruals_period.filter(created_at__date__gte=date_from)
            payouts_period = payouts_period.filter(created_at__date__gte=date_from)

        if date_to:
            accruals_period = accruals_period.filter(created_at__date__lte=date_to)
            payouts_period = payouts_period.filter(created_at__date__lte=date_to)

        # Рассчитываем Totals
        accrued_total = accruals_period.filter(
            status__in=[MasterSalaryAccrual.Status.ACCRUED, MasterSalaryAccrual.Status.PAID]
        ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

        paid_total = payouts_period.aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')
        balance = accruals_balance.aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')
        accruals_count = accruals_period.count()

        # Собираем ID мастеров для by_master
        master_ids = set()
        if master_param:
            master_ids.add(master_param)
        else:
            accrual_masters = MasterSalaryAccrual.objects.filter(company=company).values_list("master_id", flat=True).distinct()
            payout_masters = MasterSalaryPayout.objects.filter(company=company).values_list("master_id", flat=True).distinct()
            master_ids.update(accrual_masters)
            master_ids.update(payout_masters)

        UserObj = get_user_model()
        masters = UserObj.objects.filter(id__in=master_ids, company=company)

        by_master = []
        for m in masters:
            m_accruals_period = accruals_period.filter(master=m)
            m_payouts_period = payouts_period.filter(master=m)
            m_accruals_balance = accruals_balance.filter(master=m)

            m_accruals_count = m_accruals_period.count()
            m_service_amount = m_accruals_period.aggregate(sum=Sum('service_amount'))['sum'] or Decimal('0.00')
            m_accrued_total = m_accruals_period.filter(
                status__in=[MasterSalaryAccrual.Status.ACCRUED, MasterSalaryAccrual.Status.PAID]
            ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')
            m_paid_total = m_payouts_period.aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')
            m_balance = m_accruals_balance.aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

            if m.first_name or m.last_name:
                master_name = f"{m.first_name or ''} {m.last_name or ''}".strip()
            else:
                master_name = m.email

            by_master.append({
                "master": str(m.id),
                "master_name": master_name,
                "accruals_count": m_accruals_count,
                "service_amount": str(m_service_amount),
                "accrued_total": str(m_accrued_total),
                "paid_total": str(m_paid_total),
                "balance": str(m_balance),
            })

        return Response({
            "totals": {
                "accrued_total": str(accrued_total),
                "paid_total": str(paid_total),
                "balance": str(balance),
                "accruals_count": accruals_count
            },
            "by_master": by_master
        }, status=status.HTTP_200_OK)


class MasterSalaryPayoutListCreateView(CompanyQuerysetMixin, generics.ListCreateAPIView):
    """
    GET /api/barbershop/salary/payouts/ (История выплат)
    POST /api/barbershop/salary/payouts/ (Создать выплату мастеру)
    """
    serializer_class = MasterSalaryPayoutSerializer
    queryset = MasterSalaryPayout.objects.select_related("master", "created_by").all()
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        role = str(getattr(user, "role", "") or "").strip().lower()
        is_admin_or_owner = (role in ["admin", "owner"]) or user.is_superuser

        if not is_admin_or_owner and not getattr(user, "can_view_salary", False):
            raise PermissionDenied("У вас нет доступа к разделу Зарплата.")

        qs = super().get_queryset()

        if not is_admin_or_owner:
            qs = qs.filter(master=user)
        else:
            master_id = self.request.query_params.get("master")
            if master_id:
                qs = qs.filter(master_id=master_id)

        date_from = self.request.query_params.get("date_from")
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)

        date_to = self.request.query_params.get("date_to")
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        return qs

    def create(self, request, *args, **kwargs):
        user = request.user
        role = str(getattr(user, "role", "") or "").strip().lower()
        is_admin_or_owner = (role in ["admin", "owner"]) or user.is_superuser

        if not is_admin_or_owner:
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        if not company:
            return Response({"detail": "У пользователя не задана компания."}, status=status.HTTP_400_BAD_REQUEST)

        master_id = request.data.get("master")
        amount_val = request.data.get("amount")
        comment = request.data.get("comment", "")

        if not master_id:
            return Response({"master": ["Обязательное поле."]}, status=status.HTTP_400_BAD_REQUEST)
        if amount_val is None:
            return Response({"amount": ["Обязательное поле."]}, status=status.HTTP_400_BAD_REQUEST)

        UserObj = get_user_model()
        try:
            master = UserObj.objects.get(id=master_id, company=company, is_active=True)
        except (UserObj.DoesNotExist, ValueError):
            return Response({"master": ["Мастер не найден или неактивен в компании"]}, status=status.HTTP_400_BAD_REQUEST)

        try:
            amount = Decimal(str(amount_val))
        except (ValueError, TypeError):
            return Response({"amount": ["Сумма должна быть числом."]}, status=status.HTTP_400_BAD_REQUEST)

        if amount <= 0:
            return Response({"amount": ["Сумма должна быть больше нуля."]}, status=status.HTTP_400_BAD_REQUEST)

        # Вычисляем текущий баланс мастера
        balance = MasterSalaryAccrual.objects.filter(
            company=company,
            master=master,
            status=MasterSalaryAccrual.Status.ACCRUED
        ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

        if amount > balance:
            return Response({"amount": [f"Сумма превышает баланс мастера ({balance})"]}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            payout = MasterSalaryPayout.objects.create(
                company=company,
                master=master,
                amount=amount,
                comment=comment,
                created_by=user
            )

            # FIFO закрытие начислений
            accrueds = list(MasterSalaryAccrual.objects.filter(
                company=company,
                master=master,
                status=MasterSalaryAccrual.Status.ACCRUED
            ).order_by("created_at"))

            remaining = amount
            for accrual in accrueds:
                if remaining <= 0:
                    break

                acc_amount = accrual.amount
                if acc_amount <= 0:
                    # Отрицательное начисление закрываем выплатой
                    accrual.status = MasterSalaryAccrual.Status.PAID
                    accrual.payout = payout
                    accrual.save(update_fields=["status", "payout", "updated_at"])
                    remaining -= acc_amount
                else:
                    if acc_amount <= remaining:
                        # Полное покрытие положительного начисления
                        accrual.status = MasterSalaryAccrual.Status.PAID
                        accrual.payout = payout
                        accrual.save(update_fields=["status", "payout", "updated_at"])
                        remaining -= acc_amount
                    else:
                        unpaid_amount = acc_amount - remaining
                        unpaid_service_amount = (accrual.service_amount * unpaid_amount / acc_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        if unpaid_service_amount > accrual.service_amount:
                            unpaid_service_amount = accrual.service_amount

                        # Текущее начисление на оплаченную часть (сохраняем сначала, освобождая UNIQUE статус)
                        accrual.amount = remaining
                        accrual.service_amount = accrual.service_amount - unpaid_service_amount
                        accrual.status = MasterSalaryAccrual.Status.PAID
                        accrual.payout = payout
                        accrual.save(update_fields=["amount", "service_amount", "status", "payout", "updated_at"])

                        # Новое начисление на неоплаченную часть
                        MasterSalaryAccrual.objects.create(
                            company_id=accrual.company_id,
                            master=accrual.master,
                            appointment=accrual.appointment,
                            service=accrual.service,
                            service_amount=unpaid_service_amount,
                            percent=accrual.percent,
                            amount=unpaid_amount,
                            status=MasterSalaryAccrual.Status.ACCRUED
                        )

                        remaining = Decimal("0.00")
                        break

        serializer = self.get_serializer(payout)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
