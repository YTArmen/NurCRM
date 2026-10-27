"""Б-65: хранение и чтение журнала действий ИИ."""
import hashlib
from datetime import timedelta

from django.utils import timezone

from apps.main.tz26.ai_stock.models import AiAction, AiCompanySettings

RETENTION_DAYS = 180


def store_utterances_enabled(company) -> bool:
    return AiCompanySettings.objects.filter(company=company, store_ai_utterances=True).exists()


def utterance_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode("utf-8")).hexdigest()


def purge_old(now=None) -> int:
    """Удаляет записи старше 180 дней. Возвращает число удалённых."""
    now = now or timezone.now()
    deleted, _ = AiAction.objects.filter(at__lt=now - timedelta(days=RETENTION_DAYS)).delete()
    return deleted
