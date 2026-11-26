"""ТЗ 26.10, кусок staff: файлы отчёта задачи (Б-62), плановый график смен (Б-64)."""
import uuid

from django.conf import settings
from django.db import models

__all__ = ["TaskReportFile", "StaffSchedule"]


def task_report_file_upload_to(instance, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"tasks/{instance.task_id}/files/{uuid.uuid4().hex}.{ext}"


class TaskReportFile(models.Model):
    """Б-62: фото/файл отчёта к задаче (как building/tasks/{id}/files/)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey("main.Task", on_delete=models.CASCADE, related_name="report_files")
    title = models.CharField(max_length=255, blank=True, default="")
    file = models.FileField(upload_to=task_report_file_upload_to)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        ordering = ["created_at", "id"]


class StaffSchedule(models.Model):
    """Б-64: плановая смена сотрудника на дату (план 09:00–18:00)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="staff_schedules")
    branch = models.ForeignKey(
        "users.Branch", on_delete=models.CASCADE, null=True, blank=True, related_name="staff_schedules",
    )
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_schedules")
    date = models.DateField(db_index=True)
    start = models.TimeField()
    end = models.TimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "main"
        ordering = ["date", "start", "id"]
        indexes = [models.Index(fields=["company", "employee", "date"], name="main_staffsch_cmp_emp_dt")]
