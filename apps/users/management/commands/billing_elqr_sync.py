"""elqr1911: сверка счетов ELQR — раз в час (cron). Спрашивает банк о незавершённых счетах за 3 дня
(ловит потерянные callback'и) и закрывает истёкшие."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.users.billing_elqr import refresh
from apps.users.models import BillingInvoice


class Command(BaseCommand):
    help = "Сверка счетов ELQR"

    def handle(self, *args, **opts):
        since = timezone.now() - timedelta(days=3)
        qs = BillingInvoice.objects.filter(created_at__gte=since, extended_at__isnull=True,
                                           status__in=["pending", "processing", "cancelled"]).select_related("company")
        n = paid = 0
        for inv in qs:
            n += 1
            inv = refresh(inv, force=True)
            paid += inv.status == "paid"
        self.stdout.write(f"{timezone.now():%F %T} checked={n} paid={paid}")
