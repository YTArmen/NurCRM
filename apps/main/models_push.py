"""П2-2 (14.11.2026, p2214): push-токены сотрудников (касса на Android, программа владельца, мобилка)."""
from django.conf import settings
from django.db import models


class StaffPushToken(models.Model):
    id = models.BigAutoField(primary_key=True)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="staff_push_tokens")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_push_tokens")
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=16, default="android")  # android | ios | web | desktop
    lang = models.CharField(max_length=4, default="ru")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "main"
        verbose_name = "Push-токен сотрудника"
        verbose_name_plural = "Push-токены сотрудников"
        indexes = [models.Index(fields=["company", "user"], name="idx_staffpush_company_user")]
