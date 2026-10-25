from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from apps.arenda.services.charges import due_date_for, indexed_amount, prorata_amount
from apps.arenda.services.debt import (calc_penalty, compute_debt, lease_days_left, lease_display_status)
from apps.arenda.services.money import calc_rent


class MoneyTests(SimpleTestCase):
    def test_rent_is_integer_tyiyn(self):
        r = calc_rent(Decimal("42.50"), 150000)
        self.assertEqual(r, 6375000)
        self.assertIsInstance(r, int)

    def test_rounding(self):
        self.assertEqual(calc_rent(Decimal("0.01"), 50), 1)  # 0.5 -> 1


class ProrataTests(SimpleTestCase):
    def test_full_month(self):
        self.assertEqual(prorata_amount(3000000, date(2026, 4, 1), date(2026, 1, 1), None), 3000000)

    def test_first_month_partial(self):
        # аренда с 16 апреля: 15 дней из 30
        self.assertEqual(prorata_amount(3000000, date(2026, 4, 1), date(2026, 4, 16), None), 1500000)

    def test_last_month_partial(self):
        # до 10 апреля: 10 дней из 30
        self.assertEqual(prorata_amount(3000000, date(2026, 4, 1), date(2026, 1, 1), date(2026, 4, 10)), 1000000)

    def test_start_and_end_same_month(self):
        self.assertEqual(prorata_amount(3100000, date(2026, 5, 1), date(2026, 5, 11), date(2026, 5, 20)), 1000000)

    def test_outside_month(self):
        self.assertEqual(prorata_amount(100, date(2026, 5, 1), date(2026, 6, 1), None), 0)
        self.assertEqual(prorata_amount(100, date(2026, 5, 1), date(2026, 1, 1), date(2026, 4, 30)), 0)

    def test_due_date_clamped(self):
        self.assertEqual(due_date_for(date(2026, 2, 1), 31), date(2026, 2, 28))
        self.assertEqual(due_date_for(date(2026, 3, 1), 10), date(2026, 3, 10))

    def test_indexation(self):
        self.assertEqual(indexed_amount(1000000, 10, date(2025, 3, 5), date(2026, 2, 1)), 1000000)
        self.assertEqual(indexed_amount(1000000, 10, date(2025, 3, 5), date(2026, 3, 1)), 1100000)
        self.assertEqual(indexed_amount(1000000, 0, date(2020, 3, 5), date(2026, 3, 1)), 1000000)


class DebtTests(SimpleTestCase):
    today = date(2026, 5, 20)

    def test_no_debt(self):
        i = compute_debt([(date(2026, 5, 25), 100, "rent")], 0, date(2026, 5, 1))
        # срок через 24 дня: в порядке
        self.assertEqual((i.debt, i.status, i.overdue_days), (0, "ok", 0))

    def test_due_soon(self):
        i = compute_debt([(date(2026, 5, 25), 100, "rent")], 0, self.today)
        self.assertEqual(i.status, "due_soon")
        self.assertEqual(i.debt, 0)  # срок ещё не наступил

    def test_due_today_counts_as_debt_not_overdue(self):
        i = compute_debt([(self.today, 100, "rent")], 0, self.today)
        self.assertEqual((i.debt, i.status, i.overdue_days), (100, "due_soon", 0))

    def test_overdue_days_from_oldest(self):
        ch = [(date(2026, 4, 10), 100, "rent"), (date(2026, 5, 10), 100, "rent")]
        i = compute_debt(ch, 0, self.today)
        self.assertEqual((i.debt, i.status, i.overdue_days), (200, "overdue", 40))

    def test_payment_closes_oldest_first(self):
        ch = [(date(2026, 4, 10), 100, "rent"), (date(2026, 5, 10), 100, "rent")]
        i = compute_debt(ch, 100, self.today)
        self.assertEqual((i.debt, i.overdue_days), (100, 10))  # остался только май

    def test_overpayment_is_advance(self):
        i = compute_debt([(date(2026, 5, 10), 100, "rent")], 250, self.today)
        self.assertEqual((i.debt, i.advance, i.status), (0, 150, "ok"))

    def test_soon_boundary(self):
        self.assertEqual(compute_debt([(date(2026, 5, 27), 1, "rent")], 0, self.today).status, "due_soon")
        self.assertEqual(compute_debt([(date(2026, 5, 28), 1, "rent")], 0, self.today).status, "ok")

    def test_penalty_formula_and_no_penalty_on_penalty(self):
        self.assertEqual(calc_penalty(1000000, Decimal("0.1"), 10), 10000)
        self.assertEqual(calc_penalty(0, 0.1, 10), 0)
        ch = [(date(2026, 5, 10), 1000, "rent"), (date(2026, 5, 11), 50, "penalty")]
        i = compute_debt(ch, 0, self.today)
        self.assertEqual(i.overdue_base, 1000)

    def test_lease_status(self):
        t = self.today
        self.assertEqual(lease_display_status("active", date(2026, 1, 1), date(2026, 5, 19), t), "expired")
        self.assertEqual(lease_display_status("active", date(2026, 1, 1), date(2026, 7, 1), t), "expiring")
        self.assertEqual(lease_display_status("active", date(2026, 1, 1), date(2027, 7, 1), t), "active")
        self.assertEqual(lease_display_status("draft", date(2026, 1, 1), None, t), "draft")
        self.assertIsNone(lease_days_left(date(2026, 5, 19), t))
        self.assertEqual(lease_days_left(date(2026, 5, 30), t), 10)
