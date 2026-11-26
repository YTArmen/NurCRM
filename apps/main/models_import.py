"""Л-10 (14.11.2026, l1014): начальный долг клиента при переносе из другой программы и пометка «перенесён из файла»."""
import uuid

from django.conf import settings
from django.db import models


class ClientImportMark(models.Model):
    """Клиент перенесён из файла (Excel/1С/другая программа). Одна запись на клиента."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("users.Company", on_delete=models.CASCADE, related_name="client_import_marks")
    client = models.OneToOneField("main.Client", on_delete=models.CASCADE, related_name="import_mark")
    source = models.CharField("Откуда", max_length=64, blank=True, default="")
    batch = models.CharField("Пачка переноса", max_length=64, blank=True, default="", db_index=True)
    opening_debt_deal = models.OneToOneField("main.ClientDeal", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    idempotency_key = models.CharField(max_length=128, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "main"
        verbose_name = "Перенос клиента"
        verbose_name_plural = "Перенос клиентов"
        indexes = [models.Index(fields=["company", "batch"], name="idx_cimport_company_batch")]
