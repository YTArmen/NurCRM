"""ТЗ 11–12 (22.10.2026): мелкие серверные ошибки, найденные при проверке сайта."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.construction.models import Cashbox, CashFlow
from apps.main.models import Client, ClientDeal, Sale
from apps.main.tests_kassa_api import KassaBase


class SalesDateToTests(KassaBase):
    """11.1: date_to=YYYY-MM-DD включает весь последний день."""

    def test_bare_date_to_includes_whole_day(self):
        today = timezone.localdate()
        morning = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time())) + timedelta(hours=3)
        evening = morning.replace(hour=23, minute=40)
        s1 = Sale.objects.create(company=self.company, user=self.owner, cashbox=self.cashbox, status=Sale.Status.PAID)
        s2 = Sale.objects.create(company=self.company, user=self.owner, cashbox=self.cashbox, status=Sale.Status.PAID)
        Sale.objects.filter(pk=s1.pk).update(created_at=morning)
        Sale.objects.filter(pk=s2.pk).update(created_at=evening)
        r = self.api.get("/api/main/pos/sales/", {"date_from": today.isoformat(), "date_to": today.isoformat()})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual({row["id"] for row in r.data["results"]}, {str(s1.id), str(s2.id)})
