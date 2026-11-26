"""П2-2 (14.11.2026): «Требует внимания» владельцу — смены открыты > 23 ч и товары ниже минимума.

  manage.py staff_attention shifts   — раз в час (одно уведомление на смену, повтор не раньше чем через 24 ч)
  manage.py staff_attention lowstock — раз в день (одно уведомление на компанию в день)
  --dry-run — только посчитать
Шлётся только компаниям, где есть хотя бы один push-токен сотрудника или кто-то работал за 7 дней (лента).
"""
from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db.models import F
from django.utils import timezone


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("what", choices=["shifts", "lowstock"])
        p.add_argument("--dry-run", action="store_true")

    def handle(self, *a, what, dry_run, **o):
        from apps.main.staff_push import notify

        n = 0
        if what == "shifts":
            from apps.construction.models import CashShift

            border = timezone.now() - timedelta(hours=23)
            for sh in CashShift.objects.filter(status=CashShift.Status.OPEN, opened_at__lt=border).select_related("company", "cashier", "cashbox")[:2000]:
                key = f"attn:shift:{sh.pk}"
                if cache.get(key):
                    continue
                who = (f"{getattr(sh.cashier, 'first_name', '') or ''} {getattr(sh.cashier, 'last_name', '') or ''}".strip()
                       or getattr(sh.cashier, "email", "") or "Кассир")
                n += 1
                if dry_run:
                    continue
                notify(sh.company, "shift_open_long",
                       {"who": who, "cashbox": getattr(sh.cashbox, "name", "") or "", "since": timezone.localtime(sh.opened_at).strftime("%d.%m %H:%M")},
                       {"shift": str(sh.pk)})
                cache.set(key, 1, 24 * 3600)
        else:
            from apps.main.models import ChangeEvent, Product, StaffPushToken

            since = timezone.now() - timedelta(days=7)
            cids = set(StaffPushToken.objects.values_list("company_id", flat=True)) | set(
                ChangeEvent.objects.filter(created_at__gte=since).values_list("company_id", flat=True).distinct())
            day = timezone.localdate().isoformat()
            from apps.users.models import Company

            for c in Company.objects.filter(pk__in=cids):
                key = f"attn:low:{c.pk}:{day}"
                if cache.get(key):
                    continue
                qs = Product.objects.filter(company=c, minimum_quantity__gt=0, quantity__lt=F("minimum_quantity"))
                cnt = qs.count()
                if not cnt:
                    continue
                n += 1
                if dry_run:
                    continue
                names = ", ".join(qs.order_by("quantity").values_list("name", flat=True)[:3])
                notify(c, "low_stock", {"n": cnt, "names": names}, {"count": cnt})
                cache.set(key, 1, 26 * 3600)
        self.stdout.write(f"{what}: {n}")
