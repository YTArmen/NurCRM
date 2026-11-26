"""ТЗ 29.10 Б-99: сгорание бонусов по сроку (AppShopSettings.points_expire_days, строка компании)."""
from collections import deque
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.main.models import Client, ClientBonusTransaction

ZERO = Decimal("0.00")


def expirable_amount(client, cutoff):
    """FIFO: сколько из начислений старше cutoff ещё не покрыто списаниями/сгоранием."""
    lots = deque()  # [created_at, остаток]
    rows = ClientBonusTransaction.objects.filter(client=client).order_by("created_at", "id").values_list("created_at", "delta")
    for created, delta in rows:
        if delta > 0:
            lots.append([created, delta])
        elif delta < 0:
            need = -delta
            while need > 0 and lots:
                take = min(need, lots[0][1])
                lots[0][1] -= take
                need -= take
                if lots[0][1] <= 0:
                    lots.popleft()
    return sum((rest for created, rest in lots if created <= cutoff), ZERO)


def expire_bonuses(*, dry_run=False, now=None, company_id=None, out=None):
    """Возвращает список (client, сумма). Повторный запуск ничего не списывает: сгоревшее уже покрыто движением expire."""
    from apps.clientapp.models import AppShopSettings
    from apps.main.kassa_views import change_bonus

    now = now or timezone.now()
    day = timezone.localtime(now).date().isoformat()
    result = []
    rows = AppShopSettings.objects.filter(branch__isnull=True, points_expire_days__isnull=False)
    if company_id:
        rows = rows.filter(company_id=company_id)
    for row in rows:
        cutoff = now - timedelta(days=row.points_expire_days)
        clients = Client.objects.filter(
            company_id=row.company_id,
            bonus_transactions__created_at__lte=cutoff,
            bonus_transactions__delta__gt=0,
        ).distinct()
        for client in clients:
            amount = min(expirable_amount(client, cutoff), client.bonus_balance or ZERO)
            if amount <= 0:
                continue
            result.append((client, amount))
            if out:
                out(f"{client.pk}: сгорает {amount}")
            if dry_run:
                continue
            key = f"expire:{client.pk}:{day}"
            if ClientBonusTransaction.objects.filter(company_id=row.company_id, idempotency_key=key).exists():
                continue
            change_bonus(client=client, delta=-amount, reason=ClientBonusTransaction.Reason.EXPIRE,
                         note=f"Сгорание бонусов (срок {row.points_expire_days} дн.)", idempotency_key=key)
    return result
