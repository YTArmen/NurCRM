from datetime import date, timedelta
from unittest import mock

from django.utils import timezone

from apps.arenda.models import Charge, Counter, Payment, PaymentAllocation, Reminder
from apps.arenda.services.charges import ensure_rent_charge
from apps.arenda.services.debt import calc_penalty
from apps.arenda.services.words import fmt_som, number_to_words, som_to_words
from apps.arenda.models import ArendaAccess, Lease

from .base import ArendaBase

API = "/api/arenda/"


class MoneyBase(ArendaBase):
    """Арендатор с договором с 1 числа 3 месяцев назад: аренда 10 м² × 100000 = 1 000 000 тыйын (10 000 сом)."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.t = self.tenant()
        self.u = self.unit("101", area="10.00", rate=100000)
        start = (self.today.replace(day=1) - timedelta(days=62)).replace(day=1)
        self.start = start
        r = self.lease(self.t, [self.u], start=start.isoformat(), pay_day=5)
        self.assertEqual(r.status_code, 201, r.data)
        self.lease_obj = Lease.objects.get(pk=r.data["id"])
        # два прошлых месяца начислены задним числом
        m = start
        while m <= self.today.replace(day=1):
            ensure_rent_charge(self.lease_obj, m)
            m = (m.replace(day=28) + timedelta(days=5)).replace(day=1)

    def pay(self, amount, **kw):
        body = {"tenant": self.t["id"], "amount": amount, "method": "cash", **kw}
        return self.client.post(API + "payments/", body, format="json")

    def debt(self):
        return self.client.get(f"{API}tenants/{self.t['id']}/card/").data["debt"]


class PaymentTests(MoneyBase):
    def test_partial_payment_closes_oldest_first_and_receipt_shows_debt(self):
        before = self.debt()["debt"]
        self.assertGreater(before, 1_000_000)
        r = self.pay(400_000)
        self.assertEqual(r.status_code, 201, r.data)
        al = r.data["allocations"]
        self.assertEqual(len(al), 1)
        oldest = Charge.objects.filter(tenant_id=self.t["id"]).order_by("due_date").first()
        self.assertEqual(al[0]["charge"], str(oldest.id))
        self.assertEqual(al[0]["amount"], 400_000)
        self.assertEqual(r.data["debt_after"], before - 400_000)
        self.assertEqual(r.data["receipt"]["snapshot"]["debt_after"], before - 400_000)
        self.assertEqual(r.data["receipt"]["snapshot"]["rent"], 400_000)
        self.assertEqual(self.debt()["debt"], before - 400_000)

    def test_overpayment_becomes_advance_and_applies_to_later_charges(self):
        total = self.debt()["debt"]
        r = self.pay(total + 300_000)
        self.assertEqual(r.data["debt_after"], 0)
        self.assertEqual(r.data["receipt"]["snapshot"]["advance"], 300_000)
        self.assertEqual(self.debt()["advance"], 300_000)
        # новое начисление автоматически гасится авансом
        Charge.objects.create(company=self.company, branch=self.branch, tenant_id=self.t["id"], kind="fine",
                              period=self.today.replace(day=1), amount=500_000, due_date=self.today)
        self.assertEqual(self.debt()["debt"], 200_000)

    def test_penalty_by_formula_and_add_penalty(self):
        info = self.debt()
        self.assertGreater(info["overdue_days"], 0)
        expected = calc_penalty(self.lease_obj and self._overdue_base(), "0.100", info["overdue_days"])
        self.assertEqual(info["penalty_calc"], expected)
        self.assertGreater(expected, 0)
        debt0 = info["debt"]
        r = self.pay(100_000, add_penalty=True)
        self.assertEqual(r.data["penalty_added"], expected)
        self.assertEqual(r.data["debt_after"], debt0 + expected - 100_000)
        pen = Charge.objects.get(tenant_id=self.t["id"], kind="penalty")
        self.assertEqual(pen.amount, expected)
        # пеня не начисляется повторно в тот же день
        r2 = self.pay(1000, add_penalty=True)
        self.assertEqual(r2.data["penalty_added"], 0)

    def test_auto_penalty_and_payment_penalty_do_not_double_charge_same_day(self):
        from apps.arenda.models import Lease
        from apps.arenda.services.reminders import create_penalty_charge
        lease = Lease.objects.get(tenant_id=self.t["id"])
        base = self._overdue_base()
        ch, created = create_penalty_charge(self.company, self.t["id"], lease, self.today, base, "0.100")
        self.assertTrue(created)
        # оплата с «добавить пеню» в тот же день второй пени не создаёт
        r = self.pay(100_000, add_penalty=True)
        self.assertEqual(r.data["penalty_added"], 0)
        self.assertEqual(Charge.objects.filter(tenant_id=self.t["id"], kind="penalty",
                                               voided_at__isnull=True).count(), 1)
        # и авто-правило после оплаты не дублирует
        self.assertFalse(create_penalty_charge(self.company, self.t["id"], lease, self.today, base, "0.100")[1])

    def _overdue_base(self):
        from apps.arenda.services import stats
        i = stats.debt_map(self.company, self.today, None, [self.t["id"]])[
            __import__("uuid").UUID(self.t["id"])]
        return i.overdue_base

    def test_other_income_needs_category_and_has_no_receipt(self):
        r = self.client.post(API + "payments/", {"amount": 50_000, "method": "bank",
                                                 "branch": str(self.branch.id)}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(API + "payments/", {"amount": 50_000, "method": "bank", "category": "advertising",
                                                 "comment": "баннер", "branch": str(self.branch.id)}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(r.data["receipt"])
        self.assertEqual(r.data["payment"]["category"], "advertising")

    def test_idempotent(self):
        body = {"tenant": self.t["id"], "amount": 100_000, "method": "cash"}
        r1 = self.client.post(API + "payments/", body, format="json", HTTP_IDEMPOTENCY_KEY="k-1")
        r2 = self.client.post(API + "payments/", body, format="json", HTTP_IDEMPOTENCY_KEY="k-1")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2["Idempotent-Replayed"], "true")
        self.assertEqual(r1.data["payment"]["id"], r2.data["payment"]["id"])
        self.assertEqual(Payment.objects.count(), 1)

    def test_future_date_rejected(self):
        r = self.pay(1000, date=(self.today + timedelta(days=2)).isoformat())
        self.assertEqual(r.status_code, 400)

    def test_list_totals_and_balances(self):
        self.pay(100_000, method="cash")
        self.pay(200_000, method="bank")
        r = self.client.get(API + f"payments/?date={self.today.isoformat()}")
        self.assertEqual(r.data["count"], 2)
        self.assertEqual(r.data["totals"]["day"]["in"], 300_000)
        self.assertEqual(r.data["totals"]["month"]["by_method"]["bank"], 200_000)
        self.assertEqual(r.data["balances"]["cash"], 100_000)
        r = self.client.get(API + f"payments/?month={self.today:%Y-%m}&tenant={self.t['id']}")
        self.assertEqual(r.data["count"], 2)

    def test_cashier_without_cash_section_denied(self):
        u, c = self.staff()
        ArendaAccess.objects.create(company=self.company, user=u, sections=["chess"], all_branches=True)
        r = c.get(API + "payments/")
        self.assertEqual(r.status_code, 403)


class StornoTests(MoneyBase):
    def test_storno_only_owner(self):
        pid = self.pay(300_000).data["payment"]["id"]
        u, c = self.staff()
        ArendaAccess.objects.create(company=self.company, user=u, sections=["cash", "tenants"], all_branches=True)
        r = c.post(f"{API}payments/{pid}/storno/", {"reason": "ошибка"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Payment.objects.get(pk=pid).status, "posted")

    def test_storno_reverses_allocations_keeps_record(self):
        before = self.debt()["debt"]
        pid = self.pay(300_000, add_penalty=True).data["payment"]["id"]
        r = self.client.post(f"{API}payments/{pid}/storno/", {"reason": "ошибка кассира"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "reversed")
        p = Payment.objects.get(pk=pid)
        self.assertEqual(p.reversal_reason, "ошибка кассира")
        self.assertEqual(PaymentAllocation.objects.filter(payment=p).count(), 0)
        self.assertEqual(self.debt()["debt"], before)  # пеня «при оплате» аннулирована
        r = self.client.post(f"{API}payments/{pid}/storno/", {"reason": "ещё"}, format="json")
        self.assertEqual(r.status_code, 409)
        r = self.client.post(f"{API}payments/{pid}/storno/", {}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_storno_reallocates_other_payments_oldest_first(self):
        p1 = self.pay(1_000_000).data["payment"]["id"]
        self.pay(500_000)
        self.client.post(f"{API}payments/{p1}/storno/", {"reason": "x"}, format="json")
        oldest = Charge.objects.filter(tenant_id=self.t["id"]).order_by("due_date").first()
        a = PaymentAllocation.objects.get()
        self.assertEqual(a.charge_id, oldest.id)


class ReceiptTests(MoneyBase):
    def test_numbering_gapless_and_views(self):
        ids = [self.pay(10_000 * (i + 1)).data["receipt"]["id"] for i in range(3)]
        self.assertEqual(sorted(Counter.objects.get(company=self.company, key="receipt").last_value for _ in [0]), [3])
        r = self.client.get(f"{API}receipts/{ids[2]}/")
        self.assertEqual(r.data["number"], 3)
        self.assertEqual(r.data["label"], "Ч-00003")
        self.assertIn("Остаток долга", r.data["text"])
        t = self.client.get(f"{API}receipts/{ids[0]}/text/", HTTP_ACCEPT="text/plain")  # так просит сайт
        self.assertEqual(t.status_code, 200)
        self.assertEqual(t["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("101", t.content.decode())
        h = self.client.get(f"{API}receipts/{ids[0]}/print/")
        self.assertIn("80mm", h.content.decode())
        lst = self.client.get(f"{API}receipts/?tenant={self.t['id']}")
        self.assertEqual(lst.data["count"], 3)
        self.assertEqual([x["number"] for x in lst.data["results"]], [3, 2, 1])

    def test_failed_payment_does_not_burn_number(self):
        self.pay(1000)
        self.pay(0)  # 400
        self.client.post(API + "payments/", {"tenant": self.t["id"], "amount": 5, "method": "wire"}, format="json")
        self.assertEqual(self.pay(1000).data["receipt"]["number"], 2)

    def test_penalty_separate_line(self):
        d = self.client.get(f"{API}tenants/{self.t['id']}/card/").data["debt"]
        total = d["debt"] + d["penalty_calc"]
        r = self.pay(total + 50_000, add_penalty=True)
        s = r.data["receipt"]["snapshot"]
        self.assertGreater(s["penalty"], 0)
        self.assertGreater(s["rent"], 0)
        self.assertEqual(s["advance"], 50_000)
        self.assertEqual(s["rent"] + s["penalty"] + s["advance"], total + 50_000)
        self.assertEqual(s["units"], ["101"])
        self.assertTrue(s["company"]["name"] is not None)


class RemarkTests(MoneyBase):
    def test_fine_creates_charge_and_notify_reminder(self):
        before = self.debt()["debt"]
        r = self.client.post(f"{API}tenants/{self.t['id']}/remarks/", {
            "type": "fine", "text": "Шум", "fine_amount": 200_000, "notify": True}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        ch = Charge.objects.get(kind="fine")
        self.assertEqual(ch.due_date, self.today)
        self.assertEqual(self.debt()["debt"], before + 200_000)
        rem = Reminder.objects.get(tenant_id=self.t["id"])
        self.assertEqual(rem.status, "not_sent")
        self.assertEqual(len(self.client.get(f"{API}tenants/{self.t['id']}/remarks/").data), 1)
        d = self.client.delete(f"{API}tenants/{self.t['id']}/remarks/{r.data['id']}/")
        self.assertEqual(d.status_code, 409)

    def test_fine_requires_amount_and_remark_archive(self):
        r = self.client.post(f"{API}tenants/{self.t['id']}/remarks/", {"type": "fine"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"{API}tenants/{self.t['id']}/remarks/", {"type": "warning", "text": "x"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Charge.objects.filter(kind="fine").count(), 0)
        self.assertEqual(Reminder.objects.count(), 0)
        self.assertEqual(self.client.delete(f"{API}tenants/{self.t['id']}/remarks/{r.data['id']}/").status_code, 204)
        self.assertEqual(len(self.client.get(f"{API}tenants/{self.t['id']}/remarks/").data), 0)


class CardTests(MoneyBase):
    def test_card(self):
        self.pay(100_000)
        self.client.post(f"{API}tenants/{self.t['id']}/remarks/", {"type": "remark", "text": "a"}, format="json")
        r = self.client.get(f"{API}tenants/{self.t['id']}/card/")
        self.assertEqual(r.status_code, 200)
        d = r.data
        self.assertEqual(d["tenant"]["name"], "ИП Асанов")
        self.assertEqual(d["units"][0]["number"], "101")
        self.assertEqual(len(d["leases"]), 1)
        self.assertEqual(d["debt"]["status"], "overdue")
        self.assertEqual(len(d["payments"]), 1)
        self.assertIsNotNone(d["payments"][0]["receipt_id"])
        self.assertEqual(len(d["remarks"]), 1)


class ActTests(MoneyBase):
    def test_closing_equals_debt_and_includes_fine(self):
        self.pay(500_000)
        self.client.post(f"{API}tenants/{self.t['id']}/remarks/", {"type": "fine", "fine_amount": 250_000, "text": "Штраф"},
                         format="json")
        debt = self.debt()["debt"]
        r = self.client.post(f"{API}tenants/{self.t['id']}/acts/", {
            "date_from": self.start.isoformat(), "date_to": self.today.isoformat()}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["closing_balance"], debt)
        self.assertEqual(r.data["opening_balance"], 0)
        docs = [x["document"] for x in r.data["rows"]]
        self.assertTrue(any("Штраф" in d for d in docs))
        self.assertTrue(any("чек № Ч-00001" in d for d in docs))
        self.assertEqual(r.data["turnover_debit"] - r.data["turnover_credit"], debt)
        self.assertIn("задолженность в пользу", r.data["summary"].lower())
        self.assertEqual(r.data["label"][:3], "АС-")
        # сохранённый акт открывается и печатается
        a = self.client.get(f"{API}acts/{r.data['id']}/")
        self.assertEqual(a.data["closing_balance"], debt)
        self.assertEqual(self.client.get(f"{API}acts/?tenant={self.t['id']}").data["count"], 1)
        h = self.client.get(f"{API}acts/{r.data['id']}/print/")
        self.assertIn("A4", h.content.decode())

    def test_opening_balance_and_numbering(self):
        mid = self.today.replace(day=1)
        a1 = self.client.post(f"{API}tenants/{self.t['id']}/acts/", {
            "date_from": mid.isoformat(), "date_to": self.today.isoformat()}, format="json")
        self.assertGreater(a1.data["opening_balance"], 0)
        a2 = self.client.post(f"{API}tenants/{self.t['id']}/acts/", {
            "date_from": mid.isoformat(), "date_to": self.today.isoformat()}, format="json")
        self.assertEqual((a1.data["number"], a2.data["number"]), (1, 2))
        bad = self.client.post(f"{API}tenants/{self.t['id']}/acts/", {
            "date_from": self.today.isoformat(), "date_to": mid.isoformat()}, format="json")
        if mid != self.today:
            self.assertEqual(bad.status_code, 400)

    def test_overpaid_phrase_in_favor_of_tenant(self):
        self.pay(self.debt()["debt"] + 100_000)
        r = self.client.post(f"{API}tenants/{self.t['id']}/acts/", {
            "date_from": self.start.isoformat(), "date_to": self.today.isoformat()}, format="json")
        self.assertLess(r.data["closing_balance"], 0)
        self.assertIn("в пользу ИП Асанов", r.data["summary"])


class AuditTests(MoneyBase):
    def test_money_actions_audited(self):
        from apps.arenda.models import AuditLog
        pid = self.pay(100_000).data["payment"]["id"]
        self.client.post(f"{API}payments/{pid}/storno/", {"reason": "x"}, format="json")
        self.client.post(f"{API}tenants/{self.t['id']}/acts/", {
            "date_from": self.start.isoformat(), "date_to": self.today.isoformat()}, format="json")
        actions = set(AuditLog.objects.values_list("action", "object_type"))
        self.assertIn(("payment", "payment"), actions)
        self.assertIn(("storno", "payment"), actions)
        self.assertIn(("create", "reconciliation_act"), actions)


class WordsTests(ArendaBase):
    def test_words(self):
        self.assertEqual(number_to_words(0), "ноль")
        self.assertEqual(number_to_words(21), "двадцать один")
        self.assertEqual(number_to_words(2000, ), "две тысячи")
        self.assertEqual(number_to_words(1_000_000), "один миллион")
        self.assertEqual(som_to_words(1_250_050), "двенадцать тысяч пятьсот сомов пятьдесят тыйынов")
        self.assertEqual(som_to_words(2100), "двадцать один сом")
        self.assertEqual(som_to_words(1100 * 100 + 200), "одна тысяча сто два сома".replace("одна тысяча", "одна тысяча"))
        self.assertEqual(fmt_som(1_250_000), "12 500")
        self.assertEqual(fmt_som(1_250_050), "12 500,50")
