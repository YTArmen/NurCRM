from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "mm1811: включить компаниям разделы Маркетплейса, которыми они уже пользовались"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, dry_run=False, **kw):
        from collections import Counter
        from apps.users.menu_modules import autoinstall
        from apps.users.models import Company

        total, per = 0, Counter()
        for c in Company.objects.all().iterator():
            new = autoinstall(c, dry_run=dry_run)
            total += bool(new)
            per.update(new)
        self.stdout.write(f"{'DRY ' if dry_run else ''}компаний: {total}; разделы: {dict(per)}")
