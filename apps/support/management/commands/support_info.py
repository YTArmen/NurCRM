"""П2-6: часы поддержки и срок ответа для GET api/public/support/info/.

  manage.py support_info show
  manage.py support_info set --hours-ru "Пн–Сб 9:00–21:00" --reply-ru "до 30 минут" [--hours-ky …] [--channel whatsapp=+996… …]
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.support.requests import LANGS, read_info, write_info


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("action", choices=["show", "set"])
        for lang in LANGS:
            p.add_argument(f"--hours-{lang}")
            p.add_argument(f"--reply-{lang}")
        p.add_argument("--channel", action="append", default=[], help="тип=значение, напр. whatsapp=+996700000000")

    def handle(self, *a, action, **o):
        cur = read_info()
        if action == "show":
            self.stdout.write(str(cur))
            return
        for lang in LANGS:
            if o.get(f"hours_{lang}"):
                cur["hours"][lang] = o[f"hours_{lang}"]
            if o.get(f"reply_{lang}"):
                cur["first_reply"][lang] = o[f"reply_{lang}"]
        if o["channel"]:
            cur["channels"] = [{"type": c.split("=", 1)[0], "value": c.split("=", 1)[1]} for c in o["channel"] if "=" in c]
        cur["updated_at"] = timezone.now().isoformat()
        write_info(cur)
        self.stdout.write("сохранено")
