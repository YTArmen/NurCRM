"""П2-6 (14.11.2026, p2614): обращения в поддержку из кассы/программы владельца со статусом и оценкой."""
from django.conf import settings
from django.db import models


class SupportRequest(models.Model):
    class Status(models.TextChoices):
        RECEIVED = "received", "Получено"
        IN_PROGRESS = "in_progress", "В работе"
        ANSWERED = "answered", "Отвечено"
        CLOSED = "closed", "Закрыто"

    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="support_requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    client_request_id = models.CharField(max_length=64)
    text = models.TextField()
    app = models.CharField(max_length=32, blank=True, default="")
    version = models.CharField(max_length=32, blank=True, default="")
    contact = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEIVED, db_index=True)
    answer = models.TextField(blank=True, default="")
    answered_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    rating_comment = models.CharField(max_length=500, blank=True, default="")
    rated_at = models.DateTimeField(null=True, blank=True)
    forwarded = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "support"
        verbose_name = "Обращение в поддержку"
        verbose_name_plural = "Обращения в поддержку"
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=["company", "client_request_id"], name="uniq_supreq_company_crid")]
