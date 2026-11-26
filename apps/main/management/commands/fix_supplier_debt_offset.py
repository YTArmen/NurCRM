"""
ТЗ кассы 05.11 п.1: исправить долги поставщиков, испорченные «возвратом поставщику» способом debt_offset.

До исправления зачёт писался в prepayment сделки-долга (а payables показывает prepayment как переплату),
и для прихода без сделки уходил на чужой, самый старый долг поставщика.

Что делает команда (по каждому поставщику с возвратами debt_offset начиная с --since):
  1) своя сделка прихода с prepayment: amount −= x, prepayment −= x (переплату убрать, долг остаётся меньше);
  2) чужие сделки: prepayment −= x (зачёт ушёл не туда — долг вернуть);
  3) остаток зачёта — на долг сделки своего прихода (_apply_deal_diff); приход без сделки считается сам.
  Сделки с платежами не трогаются — выводятся для ручной проверки.
  Долг прихода без сделки после выкатки считается сам (_receipt_pay_info вычитает возвраты debt_offset).

Запуск:
  python manage.py fix_supplier_debt_offset                 # только показать (dry-run)
  python manage.py fix_supplier_debt_offset --apply         # записать
  python manage.py fix_supplier_debt_offset --company-id <uuid> --since 2026-10-31
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.main.models import ClientDeal, SupplierReturn, SupplierReturnItem

Q2 = Decimal("0.01")


class Command(BaseCommand):
    help = "Исправить долги поставщиков после возвратов debt_offset (ТЗ 05.11 п.1). По умолчанию dry-run."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--company-id", default=None)
        parser.add_argument("--since", default="2026-10-31", help="Возвраты с этой даты (ГГГГ-ММ-ДД).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        since = date.fromisoformat(opts["since"])
        rets = SupplierReturn.objects.filter(
            compensation=SupplierReturn.Compensation.DEBT_OFFSET,
            status=SupplierReturn.Status.POSTED,
            created_at__date__gte=since,
        )
        if opts["company_id"]:
            rets = rets.filter(company_id=opts["company_id"])

        by_supplier = {}
        for r in rets.order_by("created_at"):
            amount = sum(
                (Decimal(str(q)) * Decimal(str(p)) for q, p in
                 SupplierReturnItem.objects.filter(supplier_return=r).values_list("qty", "purchase_price")),
                Decimal("0"),
            ).quantize(Q2)
            rids = {str(r.receipt_id)} if r.receipt_id else set()
            rids |= {str(x) for x in r.items.exclude(receipt_item=None).values_list("receipt_item__receipt_id", flat=True)}
            s = by_supplier.setdefault((r.company_id, r.supplier_id),
                                       {"budget": Decimal("0"), "receipts": set(), "returns": [], "per_receipt": {}})
            s["budget"] += amount
            s["receipts"] |= rids
            # сумма возврата по каждому приходу (по строкам)
            for rid_, q_, p_ in SupplierReturnItem.objects.filter(supplier_return=r).exclude(receipt_item=None).values_list(
                    "receipt_item__receipt_id", "qty", "purchase_price"):
                k = str(rid_)
                s["per_receipt"][k] = (s["per_receipt"].get(k, Decimal("0")) + Decimal(str(q_)) * Decimal(str(p_))).quantize(Q2)
            s["returns"].append(f"ВП-{r.number} {amount}")

        changed = skipped = 0
        from apps.main.receipt_edit import _apply_deal_diff

        for (company_id, supplier_id), s in by_supplier.items():
            self.stdout.write(f"Поставщик {supplier_id} (компания {company_id}): возвраты {', '.join(s['returns'])}; всего {s['budget']}")
            per_rcpt = dict(s["per_receipt"])  # сколько должно уйти на долг каждого прихода
            with transaction.atomic():
                deals = list(
                    ClientDeal.objects.select_for_update()
                    .filter(company_id=company_id, client_id=supplier_id, kind=ClientDeal.Kind.DEBT,
                            note__contains="receipt:")
                    .order_by("created_at")
                )

                def own_rid(deal):
                    return next((rid for rid in per_rcpt if f"receipt:{rid}" in (deal.note or "")), None)

                # 1) prepayment своих сделок: долг остаётся уменьшенным, переплату убираем
                own_pp = Decimal("0")
                for deal in deals:
                    rid = own_rid(deal)
                    if rid is None or deal.prepayment <= 0 or per_rcpt[rid] <= 0:
                        continue
                    x = min(deal.prepayment, per_rcpt[rid])
                    if deal.payments.exists() or deal.paid_total > 0:
                        self.stdout.write(f"  ПРОПУСК (есть платежи, вручную): своя сделка {deal.id} −{x}")
                        skipped += 1
                        continue
                    self.stdout.write(f"  своя сделка {deal.id}: amount {deal.amount}→{deal.amount - x}, prepayment {deal.prepayment}→{deal.prepayment - x}")
                    if apply:
                        deal.amount -= x
                        deal.prepayment -= x
                        deal.save()
                    per_rcpt[rid] -= x
                    own_pp += x
                    changed += 1
                # 2) чужие сделки: зачёт ушёл не туда — prepayment снять (долг вернётся как был)
                foreign_left = s["budget"] - own_pp
                for deal in deals:
                    if foreign_left <= 0:
                        break
                    if own_rid(deal) is not None:
                        continue
                    deal.refresh_from_db()
                    if deal.prepayment <= 0:
                        continue
                    x = min(deal.prepayment, foreign_left)
                    if deal.payments.exists() or deal.paid_total > 0:
                        self.stdout.write(f"  ПРОПУСК (есть платежи, вручную): чужая сделка {deal.id} prepayment −{x}")
                        skipped += 1
                        foreign_left -= x
                        continue
                    self.stdout.write(f"  чужая сделка {deal.id}: prepayment {deal.prepayment}→{deal.prepayment - x} (долг вернуть)")
                    if apply:
                        deal.prepayment -= x
                        deal.save()
                    foreign_left -= x
                    changed += 1
                # 3) остаток зачёта — на долг своего прихода (у прихода без сделки долг считается сам)
                for rid, left in per_rcpt.items():
                    if left <= 0:
                        continue
                    deal = next((d for d in deals if f"receipt:{rid}" in (d.note or "")), None)
                    if deal is None:
                        self.stdout.write(f"  приход {rid}: сделки нет — долг {left} учтётся сам (оплата прихода)")
                        continue
                    deal.refresh_from_db()
                    take = min(left, deal.remaining_debt)
                    if take <= 0:
                        continue
                    self.stdout.write(f"  сделка прихода {deal.id}: долг −{take} (amount {deal.amount}→{deal.amount - take})")
                    if apply:
                        _apply_deal_diff(deal, -take)
                    changed += 1
        mode = "ЗАПИСАНО" if apply else "dry-run (ничего не записано)"
        self.stdout.write(self.style.SUCCESS(f"{mode}: сделок {changed}, пропущено {skipped}"))
