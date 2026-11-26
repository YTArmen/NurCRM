"""03.11 front-market: миниатюры (320px WebP) для уже загруженных фото товаров.

Берёт фото без миниатюры батчами (--batch), каждое сохраняет сразу — можно прервать Ctrl+C и запустить снова,
сделанные повторно не трогаются. Сам файл фото не меняется (пишется только поле thumb).
  manage.py make_product_thumbs --dry-run        # сколько фото без миниатюры
  manage.py make_product_thumbs --limit 1000     # сделать первые 1000
  nice -n 10 manage.py make_product_thumbs       # все, с низким приоритетом
"""
import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.main.models import ProductImage


class Command(BaseCommand):
    help = "Сделать миниатюры 320px WebP для фото товаров без миниатюры (батчами, можно прерывать)."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=200)
        parser.add_argument("--limit", type=int, default=0, help="Сколько фото обработать (0 — все).")
        parser.add_argument("--company", help="Только эта компания (uuid).")
        parser.add_argument("--sleep", type=float, default=0.5, help="Пауза между батчами, сек.")
        parser.add_argument("--dry-run", action="store_true", help="Только посчитать.")

    def handle(self, *args, **o):
        no_thumb = Q(thumb__isnull=True) | Q(thumb="")
        qs = ProductImage.objects.filter(no_thumb)
        qs = qs.exclude(image__isnull=True).exclude(image="")
        if o.get("company"):
            qs = qs.filter(company_id=o["company"])
        total = qs.count()
        self.stdout.write(f"фото без миниатюры: {total}")
        if o["dry_run"] or not total:
            return
        field = ProductImage._meta.get_field("thumb")
        done = failed = 0
        last_pk = None
        limit = o["limit"]
        while True:
            page = qs.order_by("pk")
            if last_pk is not None:
                page = page.filter(pk__gt=last_pk)
            rows = list(page.only("pk", "product_id", "image", "thumb")[: max(1, o["batch"])])
            if not rows:
                break
            for img in rows:
                last_pk = img.pk
                try:
                    with img.image.open("rb") as f:
                        content = ProductImage.make_thumb_content(f)
                    name = field.generate_filename(img, content.name)
                    name = field.storage.save(name, content, max_length=field.max_length)
                    # только поле thumb, без save(): не трогаем оригинал и is_primary
                    if ProductImage.objects.filter(no_thumb, pk=img.pk).update(thumb=name) == 0:
                        field.storage.delete(name)  # кто-то успел раньше (новое фото) — свою копию убираем
                    done += 1
                except FileNotFoundError:
                    failed += 1
                except Exception as e:  # битый файл и т.п. — пропускаем, идём дальше
                    failed += 1
                    self.stderr.write(f"{img.pk}: {type(e).__name__}: {e}")
                if limit and done + failed >= limit:
                    break
            self.stdout.write(f"готово {done}, пропущено {failed} из {total}")
            if limit and done + failed >= limit:
                break
            if o["sleep"]:
                time.sleep(o["sleep"])
        self.stdout.write(f"ИТОГО: сделано {done}, пропущено (нет файла / битый) {failed}")
