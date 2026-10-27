"""
ТЗ 26.10 Б-62: задачи сотрудникам на существующем api/main/tasks/ (priority, remind_at, created_by, completed_at,
report, report_files, source; фильтры; tasks/summary/; права исполнителя; Idempotency-Key на POST).

Адреса из urls.py этого куска стоят выше старых в apps/main/urls.py и перекрывают их; старые классы не правятся.
Ограничения прав (создавать/удалять — только управляющие, исполнитель меняет только status/report/report_files)
включаются для нового клиента или компании с флагом api_strict_2026_10 (core.api_compat.is_strict);
старые кассы и сайт работают как раньше.
"""
import os
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, permissions, serializers, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.main.models import Task, _user_display_name
from apps.main.serializers import TaskSerializer
from apps.main.views import CompanyBranchRestrictedMixin
from core.api_compat import is_strict, parse_client
from core.exceptions import ApiError

from . import perms
from .models import TaskReportFile
from .tasks import schedule_notifications

User = get_user_model()

PRIORITIES = ("low", "normal", "high")
SOURCES = ("web", "pos", "mobile", "voice_ai", "telegram")
ASSIGNEE_FIELDS = {"status", "report", "report_files"}
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "heic", "gif", "pdf"}
MAX_FILE_BYTES = 10 * 1024 * 1024
ORDERING = {"due_date", "created_at", "updated_at", "status", "title", "completed_at", "priority"}


# ---------------------------------------------------------------- вспомогательное
def _is_overdue_q(today):
    """Просрочена: срок раньше сегодняшнего дня (по местному времени) и задача не выполнена."""
    tz = timezone.get_current_timezone()
    start_today = timezone.make_aware(datetime.combine(today, time.min), tz)
    return Q(due_date__lt=start_today) & ~Q(status="done")


def _bound(raw, *, end):
    """due_from/due_to: дата (включительно, по местному времени) или дата-время. → (значение, lookup) | None"""
    raw = (raw or "").strip()
    if not raw:
        return None
    tz = timezone.get_current_timezone()
    d = parse_date(raw) if len(raw) == 10 else None
    if d is not None:
        if end:
            return timezone.make_aware(datetime.combine(d + timedelta(days=1), time.min), tz), "lt"
        return timezone.make_aware(datetime.combine(d, time.min), tz), "gte"
    dt = parse_datetime(raw)
    if dt is None:
        raise ApiError("period_invalid", f"Неверная дата: {raw}.")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, tz)
    return dt, ("lte" if end else "gte")


def _default_source(request) -> str:
    kind, _ = parse_client(request)
    return {"kassa": "pos", "owner": "pos", "mobile": "mobile"}.get(kind, "")


def _user_ref(u):
    return {"id": str(u.id), "name": _user_display_name(u)} if u else None


def _can_edit_task(user, task) -> bool:
    return perms.can_manage_tasks(user) or task.assigned_to_id == user.id


def _check_file(f):
    ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
    if ext not in ALLOWED_EXT:
        raise ApiError("file_type_invalid", "Допустимы фото (jpg, png, webp, heic, gif) и pdf.")
    if f.size > MAX_FILE_BYTES:
        raise ApiError("file_too_large", "Файл больше 10 МБ.")


def save_report_files(task, request):
    """Файлы отчёта из multipart (поля report_files / file)."""
    files = list(request.FILES.getlist("report_files")) + list(request.FILES.getlist("file"))
    for f in files:
        _check_file(f)
    for f in files:
        TaskReportFile.objects.create(task=task, file=f, title=os.path.basename(f.name)[:255], created_by=request.user)


# ---------------------------------------------------------------- сериализатор
class StaffTaskSerializer(TaskSerializer):
    priority = serializers.ChoiceField(choices=PRIORITIES, required=False)
    remind_at = serializers.DateTimeField(required=False, allow_null=True)
    report = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    source = serializers.ChoiceField(choices=SOURCES, required=False, allow_blank=True)
    created_by = serializers.SerializerMethodField()
    completed_at = serializers.DateTimeField(read_only=True)
    report_files = serializers.SerializerMethodField()

    class Meta(TaskSerializer.Meta):
        fields = TaskSerializer.Meta.fields + [
            "priority", "remind_at", "created_by", "completed_at", "report", "report_files", "source",
        ]
        read_only_fields = TaskSerializer.Meta.read_only_fields + ["created_by", "completed_at"]

    def get_created_by(self, obj):
        return _user_ref(obj.created_by)

    def get_report_files(self, obj):
        req = self.context.get("request")
        out = []
        for f in obj.report_files.all():
            try:
                url = f.file.url
                url = req.build_absolute_uri(url) if req else url
            except Exception:
                url = None
            out.append({"id": str(f.id), "title": f.title, "url": url, "created_at": f.created_at.isoformat()})
        return out

    @staticmethod
    def _apply_done(validated, instance=None):
        st = validated.get("status", getattr(instance, "status", None))
        if st == "done":
            if instance is None or instance.completed_at is None:
                validated["completed_at"] = timezone.now()
        elif "status" in validated:
            validated["completed_at"] = None

    def create(self, validated_data):
        self._apply_done(validated_data)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        self._apply_done(validated_data, instance)
        if "remind_at" in validated_data and validated_data["remind_at"] != instance.remind_at:
            validated_data["reminded_at"] = None
        return super().update(instance, validated_data)


def apply_task_filters(qs, request):
    q = request.query_params
    raw = (q.get("assigned_to") or "").strip()
    if raw:
        if raw.lower() == "me":
            qs = qs.filter(assigned_to=request.user)
        else:
            try:
                qs = qs.filter(assigned_to_id=raw)
            except Exception:
                raise ApiError("employee_not_found", "Сотрудник не найден.", status_code=404)
    st = (q.get("status") or "").strip()
    if st:
        qs = qs.filter(status__in=[s.strip() for s in st.split(",") if s.strip()])
    pr = (q.get("priority") or "").strip()
    if pr:
        qs = qs.filter(priority__in=[s.strip() for s in pr.split(",") if s.strip()])
    ov = (q.get("overdue") or "").strip().lower()
    if ov in ("1", "true", "yes"):
        qs = qs.filter(_is_overdue_q(timezone.localdate()))
    elif ov in ("0", "false", "no"):
        qs = qs.exclude(_is_overdue_q(timezone.localdate()))
    b = _bound(q.get("due_from"), end=False)
    if b:
        qs = qs.filter(**{f"due_date__{b[1]}": b[0]})
    b = _bound(q.get("due_to"), end=True)
    if b:
        qs = qs.filter(**{f"due_date__{b[1]}": b[0]})

    keys = []
    for part in [p.strip() for p in (q.get("ordering") or "").split(",") if p.strip()]:
        name = part.lstrip("-")
        if name not in ORDERING:
            continue
        if name == "priority":
            qs = qs.annotate(_prio=Case(When(priority="high", then=Value(3)), When(priority="normal", then=Value(2)),
                                        default=Value(1), output_field=IntegerField()))
            name = "_prio"
        keys.append(("-" if part.startswith("-") else "") + name)
    return qs.order_by(*(keys or ["-due_date"]), "id")


# ---------------------------------------------------------------- список / создание
class StaffTaskListCreateView(CompanyBranchRestrictedMixin, generics.ListCreateAPIView):
    serializer_class = StaffTaskSerializer
    queryset = Task.objects.select_related("assigned_to", "created_by").prefetch_related("report_files")
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["title", "description"]
    filterset_fields = ["deal", "branch"]

    def get_queryset(self):
        return apply_task_filters(super().get_queryset(), self.request)

    @idempotent(body_key=True, replay_flag=True)
    def post(self, request, *args, **kwargs):
        if is_strict(request, self._company()) and not perms.can_manage_tasks(request.user):
            raise ApiError("permission_denied", "Нет доступа: ставить задачи может владелец, администратор или сотрудник с правом «Сотрудники».",
                           status_code=403, top={"required": "can_view_employees"})
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        request = self.request
        extra = {"created_by": request.user}
        if not serializer.validated_data.get("source"):
            extra["source"] = _default_source(request)
        with transaction.atomic():
            self._save_with_company_branch(serializer, **extra)
            task = serializer.instance
            save_report_files(task, request)
            notify_now = bool(task.assigned_to_id and task.assigned_to_id != request.user.id)
            transaction.on_commit(lambda: schedule_notifications(
                Task.objects.select_related("assigned_to").get(pk=task.pk), notify_now=notify_now, plan_reminder=True,
            ))


# ---------------------------------------------------------------- карточка
class StaffTaskDetailView(CompanyBranchRestrictedMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = StaffTaskSerializer
    queryset = Task.objects.select_related("company", "branch", "assigned_to", "deal", "created_by").prefetch_related("report_files")
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        strict = is_strict(request, self._company())
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        if strict and not perms.can_manage_tasks(request.user):
            if instance.assigned_to_id != request.user.id:
                raise ApiError("permission_denied", "Нет доступа к чужой задаче.", status_code=403)
            changed = sorted(
                k for k, v in serializer.validated_data.items()
                if k not in ASSIGNEE_FIELDS and getattr(instance, k, None) != v
            )
            if changed:
                raise ApiError("only_status_for_assignee", "Исполнитель может менять только статус, отчёт и файлы отчёта.",
                               status_code=403, data={"fields": changed})
        with transaction.atomic():
            old_assignee, old_remind = instance.assigned_to_id, instance.remind_at
            serializer.save()
            task = serializer.instance
            save_report_files(task, request)
            reassigned = bool(task.assigned_to_id and task.assigned_to_id != old_assignee
                              and task.assigned_to_id != request.user.id)
            if reassigned or task.remind_at != old_remind:
                if reassigned:
                    Task.objects.filter(pk=task.pk).update(reminded_at=None)
                transaction.on_commit(lambda: schedule_notifications(
                    Task.objects.select_related("assigned_to").get(pk=task.pk), notify_now=reassigned, plan_reminder=True,
                ))
        task = self.get_queryset().get(pk=task.pk)
        return Response(self.get_serializer(task).data)

    def destroy(self, request, *args, **kwargs):
        if is_strict(request, self._company()) and not perms.can_manage_tasks(request.user):
            raise ApiError("permission_denied", "Удалять задачи может владелец, администратор или сотрудник с правом «Сотрудники».",
                           status_code=403, top={"required": "can_view_employees"})
        return super().destroy(request, *args, **kwargs)


# ---------------------------------------------------------------- файлы отчёта
class StaffTaskFilesView(CompanyBranchRestrictedMixin, APIView):
    """GET/POST tasks/{id}/files/ (multipart: file), DELETE tasks/{id}/files/{file_id}/."""
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _task(self, pk):
        task = self._filter_qs_company_branch(Task.objects.all()).filter(pk=pk).first()
        if task is None:
            raise ApiError("not_found", "Задача не найдена.", status_code=404)
        return task

    @staticmethod
    def _row(f, request):
        try:
            url = request.build_absolute_uri(f.file.url)
        except Exception:
            url = None
        return {"id": str(f.id), "title": f.title, "url": url, "created_at": f.created_at.isoformat()}

    def get(self, request, pk):
        task = self._task(pk)
        return Response([self._row(f, request) for f in task.report_files.all()])

    def post(self, request, pk):
        task = self._task(pk)
        if not _can_edit_task(request.user, task):
            raise ApiError("permission_denied", "Нет доступа к чужой задаче.", status_code=403)
        before = task.report_files.count()
        save_report_files(task, request)
        if task.report_files.count() == before:
            raise ApiError("file_required", "Приложите файл (поле file).")
        return Response([self._row(f, request) for f in task.report_files.all()], status=status.HTTP_201_CREATED)

    def delete(self, request, pk, file_id):
        task = self._task(pk)
        if not _can_edit_task(request.user, task):
            raise ApiError("permission_denied", "Нет доступа к чужой задаче.", status_code=403)
        f = task.report_files.filter(pk=file_id).first()
        if f is None:
            raise ApiError("not_found", "Файл не найден.", status_code=404)
        f.file.delete(save=False)
        f.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- сводка
class StaffTaskSummaryView(CompanyBranchRestrictedMixin, APIView):
    """GET tasks/summary/?date_from&date_to — по сотрудникам: кто что не сделал (период — по сроку задачи)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = self._filter_qs_company_branch(Task.objects.all())
        if not perms.can_manage_tasks(request.user):
            qs = qs.filter(assigned_to=request.user)
        q = request.query_params
        df, dt = (q.get("date_from") or "").strip(), (q.get("date_to") or "").strip()
        for raw in (df, dt):
            if raw and parse_date(raw) is None:
                raise ApiError("period_invalid", "Дата указана неверно (YYYY-MM-DD).")
        b = _bound(df, end=False)
        if b:
            qs = qs.filter(due_date__gte=b[0])
        b = _bound(dt, end=True)
        if b:
            qs = qs.filter(due_date__lt=b[0])
        on_time = Q(status="done") & (
            Q(completed_at__isnull=False, completed_at__lte=F("due_date"))
            | Q(completed_at__isnull=True, updated_at__lte=F("due_date"))
        )
        rows = list(
            qs.order_by().values("assigned_to").annotate(
                total=Count("id"),
                done=Count("id", filter=Q(status="done")),
                in_progress=Count("id", filter=Q(status="in_progress")),
                pending=Count("id", filter=Q(status="pending")),
                overdue=Count("id", filter=_is_overdue_q(timezone.localdate())),
                done_on_time=Count("id", filter=on_time),
            )
        )
        names = {u.id: _user_display_name(u) for u in User.objects.filter(id__in=[r["assigned_to"] for r in rows if r["assigned_to"]])}
        keys = ("total", "done", "in_progress", "pending", "overdue", "done_on_time")
        results, totals = [], {k: 0 for k in keys}
        for r in rows:
            results.append({
                "assigned_to": str(r["assigned_to"]) if r["assigned_to"] else None,
                "assigned_to_name": names.get(r["assigned_to"], "") if r["assigned_to"] else "Не назначено",
                **{k: r[k] for k in keys},
            })
            for k in keys:
                totals[k] += r[k]
        results.sort(key=lambda x: (x["assigned_to_name"] or "").lower())
        return Response({"date_from": df or None, "date_to": dt or None, "results": results, "totals": totals})
