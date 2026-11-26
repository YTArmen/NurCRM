"""03.11 front-market: заполнить CashFlow.employee у старых выплат зарплаты.

1) source_kind=salary_payout, source_id = id выплаты MarketSalaryPayout (или «<id>:reversal») → сотрудник выплаты.
2) расходы с названием «Зарплата: Имя» / «Зарплата: Имя · комментарий» (так платят front-market и программа
   владельца) → сотрудник компании с таким именем. Если имя совпало у нескольких — строку пропускаем.

По умолчанию только показывает, что будет сделано (dry-run). Записать: --apply.
Идёт батчами по --batch (каждый батч — своя транзакция), можно прервать Ctrl+C и запустить снова:
заполненные строки (employee не пустой) повторно не трогаются.
"""
import time
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.construction.models import CashFlow

PREFIX = "Зарплата: "


def _norm(s):
    return " ".join((s or "").split()).lower()


def parse_payout_name(name):
    if not name or not name.startswith(PREFIX):
        return None
    rest = name[len(PREFIX):]
    i = rest.find(" · ")
    who = rest if i < 0 else rest[:i]
    return _norm(who) or None


class Command(BaseCommand):
    help = "Заполнить CashFlow.employee у старых выплат зарплаты (dry-run по умолчанию, --apply — записать)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Записать в базу (без него — только отчёт).")
        parser.add_argument("--company", help="Только эта компания (uuid).")
        parser.add_argument("--batch", type=int, default=500, help="Строк в батче (по умолчанию 500).")
        parser.add_argument("--sleep", type=float, default=0.2, help="Пауза между батчами, сек.")
        parser.add_argument("--limit", type=int, default=0, help="Остановиться после N найденных строк (0 — все).")

    def handle(self, *args, **o):
        apply_ = o["apply"]
        batch = max(1, o["batch"])
        base = CashFlow.objects.filter(employee__isnull=True, type=CashFlow.Type.EXPENSE)
        if o.get("company"):
            base = base.filter(company_id=o["company"])
        stats = {"by_payout": 0, "by_name": 0, "ambiguous": 0, "not_found": 0}
        self._names = {}

        # строки по salary_payout бывают и приходом (отмена выплаты), поэтому без фильтра по типу
        kinds = CashFlow.objects.filter(employee__isnull=True, source_kind=CashFlow.SourceKind.SALARY_PAYOUT)
        if o.get("company"):
            kinds = kinds.filter(company_id=o["company"])
        found = self._run(kinds, batch, apply_, o, stats, mode="payout")
        if not (o["limit"] and found >= o["limit"]):
            self._run(base.filter(name__startswith=PREFIX), batch, apply_, o, stats, mode="name", already=found)
        self._report(stats, apply_)

    def _run(self, qs, batch, apply_, o, stats, mode, already=0):
        from apps.main.models import MarketSalaryPayout

        found = 0
        last_pk = None
        while True:
            page = qs.order_by("pk")
            if last_pk is not None:
                page = page.filter(pk__gt=last_pk)
            rows = list(page.values("pk", "company_id", "name", "source_id")[:batch])
            if not rows:
                break
            last_pk = rows[-1]["pk"]
            updates = {}
            if mode == "payout":
                ids = {}
                for r in rows:
                    sid = (r["source_id"] or "").split(":")[0]
                    try:
                        ids[r["pk"]] = uuid.UUID(sid)
                    except ValueError:
                        stats["not_found"] += 1
                emp = dict(MarketSalaryPayout.objects.filter(id__in=set(ids.values())).values_list("id", "employee_id"))
                for pk, pid in ids.items():
                    if emp.get(pid):
                        updates[pk] = emp[pid]
                        stats["by_payout"] += 1
                    else:
                        stats["not_found"] += 1
            else:
                for r in rows:
                    key = parse_payout_name(r["name"])
                    cands = self._company_names(r["company_id"]).get(key) if key else None
                    if not cands:
                        stats["not_found"] += 1
                    elif len(cands) > 1:
                        stats["ambiguous"] += 1
                    else:
                        updates[r["pk"]] = next(iter(cands))
                        stats["by_name"] += 1
            found += len(updates)
            if apply_ and updates:
                with transaction.atomic():
                    for pk, emp_id in updates.items():
                        CashFlow.objects.filter(pk=pk, employee__isnull=True).update(employee_id=emp_id)
            self.stdout.write(f"{mode}: батч до {last_pk}, найдено {len(updates)} из {len(rows)}")
            if o["limit"] and already + found >= o["limit"]:
                break
            if apply_ and o["sleep"]:
                time.sleep(o["sleep"])
        return found

    def _company_names(self, company_id):
        if company_id not in self._names:
            User = get_user_model()
            m = {}
            for u in User.objects.filter(company_id=company_id).only("id", "first_name", "last_name", "email"):
                first, last = (u.first_name or "").strip(), (u.last_name or "").strip()
                keys = {_norm(f"{first} {last}"), _norm(f"{last} {first}"), _norm(u.email)}
                for k in keys:
                    if k:
                        m.setdefault(k, set()).add(u.id)
            self._names[company_id] = m
        return self._names[company_id]

    def _report(self, stats, apply_):  # noqa: D401
        mode = "ЗАПИСАНО" if apply_ else "dry-run (ничего не записано, добавьте --apply)"
        self.stdout.write(
            f"{mode}: по выплатам {stats['by_payout']}, по имени {stats['by_name']}, "
            f"имя у нескольких сотрудников {stats['ambiguous']}, не найдено {stats['not_found']}"
        )
