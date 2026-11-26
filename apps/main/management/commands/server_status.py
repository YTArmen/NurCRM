"""П1-2 (14.11.2026): состояние сервера для касс и сайта (GET api/public/status/).

  manage.py server_status show
  manage.py server_status set maintenance --until 2026-11-20T14:00+06:00 --ru "Обновляем сервер до 14:00"
  manage.py server_status set degraded --ru "Медленно отвечают отчёты"   (--ky/--en/--tr/--uz — иначе русский)
  manage.py server_status clear                    — снова ok (плановые работы тоже снимаются)
  manage.py server_status plan --starts 2026-11-20T13:00+06:00 --ends 2026-11-20T14:00+06:00 --ru "Плановые работы с 13 до 14"
        — записывает плановые работы и шлёт событие maintenance_planned в ленту main/changes/
          компаниям, которые работали последние 30 дней.  --dry-run — только посчитать компании.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.main.server_status import LANGS, build_status, read_state, write_state


def _iso(v, name):
    if not v:
        return None
    d = parse_datetime(v)
    if d is None or timezone.is_naive(d):
        raise CommandError(f"{name}: нужно ISO-время с поясом, например 2026-11-20T14:00+06:00")
    return d.isoformat()


class Command(BaseCommand):
    help = "Состояние сервера (public/status/) и объявление плановых работ"

    def add_arguments(self, p):
        p.add_argument("action", choices=["show", "set", "clear", "plan"])
        p.add_argument("status", nargs="?", choices=["ok", "degraded", "maintenance"])
        p.add_argument("--until")
        p.add_argument("--starts")
        p.add_argument("--ends")
        p.add_argument("--dry-run", action="store_true")
        for lang in LANGS:
            p.add_argument(f"--{lang}")

    def handle(self, *a, **o):
        msg = {lang: o[lang] for lang in LANGS if o.get(lang)}
        st = read_state()
        now = timezone.now().isoformat()
        act = o["action"]
        if act == "show":
            self.stdout.write(str(build_status()))
            return
        if act == "clear":
            write_state({"status": "ok", "updated_at": now})
            self.stdout.write("статус: ok")
            return
        if act == "set":
            if not o.get("status"):
                raise CommandError("укажите ok, degraded или maintenance")
            if o["status"] != "ok" and not msg.get("ru"):
                raise CommandError("нужен текст --ru")
            st.update({"status": o["status"], "message": msg, "until": _iso(o.get("until"), "--until"), "updated_at": now})
            write_state(st)
            self.stdout.write(f"статус: {o['status']}")
            return
        starts, ends = _iso(o.get("starts"), "--starts"), _iso(o.get("ends"), "--ends")
        if not (starts and ends and msg.get("ru")):
            raise CommandError("plan: нужны --starts, --ends и --ru")
        if parse_datetime(ends) <= parse_datetime(starts):
            raise CommandError("--ends должно быть позже --starts")
        from apps.main.models import ChangeEvent
        from apps.users.models import User

        since = timezone.now() - timedelta(days=30)
        ids = set(ChangeEvent.objects.filter(created_at__gte=since).values_list("company_id", flat=True).distinct())
        ids |= set(User.objects.filter(last_login__gte=since, company__isnull=False).values_list("company_id", flat=True).distinct())
        self.stdout.write(f"компаний: {len(ids)}")
        if o["dry_run"]:
            return
        st.update({"planned": {"starts_at": starts, "ends_at": ends, "message": msg}, "updated_at": now})
        write_state(st)
        from apps.main.change_feed import record_change
        from apps.main.server_status import messages

        data = {"starts_at": starts, "ends_at": ends, "message": messages(msg)}
        for cid in ids:
            record_change(cid, "maintenance_planned", entity="server", entity_id="maintenance", data=data)
        self.stdout.write(f"объявлено: {starts} — {ends}")
