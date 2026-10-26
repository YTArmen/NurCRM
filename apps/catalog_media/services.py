"""Логика заполнения фото глобального каталога (используется командой fill_global_photos)."""
import time
from datetime import timedelta

import requests
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.main.models import GlobalProduct, ProductImage

from .models import GlobalProductPhoto

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}"
OFF_FIELDS = "code,image_front_url,image_url"
OFF_USER_AGENT = "NurCRM/1.0 (+https://nurcrm.kg)"


# ---------- общие ----------
def gtin_ok(code: str, check_digit: bool = True) -> bool:
    """8–14 цифр (+ контрольная цифра GTIN). Внутренние/весовые коды 02, 20–29 в OFF не бывают."""
    if not code or not code.isascii() or not code.isdigit() or not (8 <= len(code) <= 14):
        return False
    if not check_digit:
        return True
    if len(code) == 13 and code[0] == "2":
        return False
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def _chunks(qs_ids, size):
    for i in range(0, len(qs_ids), size):
        yield qs_ids[i:i + size]


def iter_gp_batches(qs, size=2000):
    """Пачки (id, barcode) по порядку id — без загрузки всех 128k в память."""
    last = None
    while True:
        page = qs.order_by("id")
        if last is not None:
            page = page.filter(id__gt=last)
        rows = list(page.values_list("id", "barcode")[:size])
        if not rows:
            return
        yield rows
        last = rows[-1][0]


# ---------- store ----------
def store_candidates():
    """Глобальные товары со штрихкодом, у которых нет рабочего фото из магазина (проверка файла — позже)."""
    return (
        GlobalProduct.objects.exclude(barcode__isnull=True).exclude(barcode="")
        .exclude(photo__source=GlobalProductPhoto.SOURCE_STORE, photo__image_name__gt="")
    )


def pick_store_images(barcodes):
    """
    {barcode: (image_name, product_id)} — одним запросом на пачку штрихкодов.
    Выбор детерминированный: самый недавно обновлённый товар компании, у него основное фото, затем самое раннее.
    Берётся первый вариант, файл которого реально лежит на диске.
    """
    rows = (
        ProductImage.objects
        .filter(product__barcode__in=barcodes)
        .exclude(image__isnull=True).exclude(image="")
        .order_by("product__barcode", "-product__updated_at", "-is_primary", "created_at", "id")
        .values_list("product__barcode", "image", "product_id")
    )
    result = {}
    for barcode, name, product_id in rows:
        if barcode in result:
            continue
        try:
            if default_storage.exists(name):
                result[barcode] = (name, product_id)
        except Exception:
            continue
    return result


def fill_store(limit=None, dry_run=False, batch_size=2000, out=None):
    stats = {"checked": 0, "found": 0, "created": 0, "replaced": 0}
    for rows in iter_gp_batches(store_candidates(), batch_size):
        if limit is not None and stats["found"] >= limit:
            break
        by_barcode = {}
        for gp_id, barcode in rows:
            by_barcode.setdefault(barcode, gp_id)
        picked = pick_store_images(list(by_barcode))
        stats["checked"] += len(rows)
        existing = {p.product_id: p for p in GlobalProductPhoto.objects.filter(product_id__in=[by_barcode[b] for b in picked])}
        for barcode, (name, src_id) in picked.items():
            if limit is not None and stats["found"] >= limit:
                break
            gp_id = by_barcode[barcode]
            stats["found"] += 1
            if dry_run:
                continue
            obj = existing.get(gp_id)
            if obj is None:
                GlobalProductPhoto.objects.create(
                    product_id=gp_id, source=GlobalProductPhoto.SOURCE_STORE,
                    image_name=name, source_product_id=src_id, url="",
                )
                stats["created"] += 1
            else:  # была ссылка off / промах / битый store — store лучше
                obj.source = GlobalProductPhoto.SOURCE_STORE
                obj.image_name, obj.source_product_id, obj.url = name, src_id, ""
                obj.save()
                stats["replaced"] += 1
        if out:
            out(f"store: проверено {stats['checked']}, найдено {stats['found']}")
    return stats


# ---------- off ----------
def parse_off_response(data):
    """URL фото из ответа OFF API v2 или None."""
    if not isinstance(data, dict) or data.get("status") not in (1, "1"):
        return None
    product = data.get("product") or {}
    for key in ("image_front_url", "image_url"):
        url = (product.get(key) or "").strip()
        if url.startswith("https://") or url.startswith("http://"):
            return url[:500]
    return None


class OffRateLimited(Exception):
    def __init__(self, retry_after=60):
        self.retry_after = retry_after


def fetch_off(code, session=None, timeout=15):
    """
    Возвращает (found: bool, url|None). Исключения: OffRateLimited (429), requests.RequestException (сеть/5xx).
    404 = товара нет в OFF (промах).
    """
    http = session or requests
    resp = http.get(
        OFF_URL.format(code=code), params={"fields": OFF_FIELDS},
        headers={"User-Agent": OFF_USER_AGENT}, timeout=timeout,
    )
    if resp.status_code == 429:
        try:
            ra = int(resp.headers.get("Retry-After", 60))
        except (TypeError, ValueError):
            ra = 60
        raise OffRateLimited(max(ra, 5))
    if resp.status_code == 404:
        return False, None
    resp.raise_for_status()
    url = parse_off_response(resp.json())
    return bool(url), url


def off_candidates(recheck_days=30, check_digit=True):
    """Без фото вообще либо старые промахи OFF (пустая строка off с checked_at старше recheck_days)."""
    base = GlobalProduct.objects.exclude(barcode__isnull=True).exclude(barcode="")
    from django.db.models import Q
    cutoff = timezone.now() - timedelta(days=recheck_days)
    miss_old = Q(photo__source=GlobalProductPhoto.SOURCE_OFF, photo__url="", photo__image_name="", photo__checked_at__lt=cutoff)
    return base.filter(Q(photo__isnull=True) | miss_old)


def fill_off(limit=None, dry_run=False, rate_per_min=80, recheck_days=30, check_digit=True,
             out=None, sleep=time.sleep, session=None, max_errors=20):
    stats = {"candidates": 0, "requests": 0, "found": 0, "miss": 0, "errors": 0, "skipped_invalid": 0}
    interval = 60.0 / max(rate_per_min, 1)
    session = session or requests.Session()
    consecutive_errors = 0
    for rows in iter_gp_batches(off_candidates(recheck_days, check_digit), 500):
        for gp_id, barcode in rows:
            if limit is not None and stats["candidates" if dry_run else "requests"] >= limit:
                return stats
            code = (barcode or "").strip()
            if not gtin_ok(code, check_digit):
                stats["skipped_invalid"] += 1
                continue
            stats["candidates"] += 1
            if dry_run:
                continue
            started = time.monotonic()
            try:
                found, url = fetch_off(code, session=session)
            except OffRateLimited as e:
                stats["errors"] += 1
                if out:
                    out(f"off: 429, пауза {e.retry_after} с")
                sleep(e.retry_after)
                continue  # этот товар попадёт в следующий запуск (строка не записана)
            except (requests.RequestException, ValueError) as e:
                stats["errors"] += 1
                consecutive_errors += 1
                if out:
                    out(f"off: ошибка {code}: {e}")
                if consecutive_errors >= max_errors:
                    if out:
                        out("off: слишком много ошибок подряд, остановка (перезапустите позже)")
                    return stats
                sleep(min(5 * consecutive_errors, 60))
                continue
            consecutive_errors = 0
            stats["requests"] += 1
            obj, _ = GlobalProductPhoto.objects.get_or_create(
                product_id=gp_id, defaults={"source": GlobalProductPhoto.SOURCE_OFF})
            if obj.source == GlobalProductPhoto.SOURCE_STORE and obj.image_name:
                continue  # фото магазина никогда не перезаписываем
            obj.source = GlobalProductPhoto.SOURCE_OFF
            obj.image_name = ""
            obj.source_product_id = None
            obj.url = url or ""
            obj.save()  # auto_now обновит checked_at
            stats["found" if found else "miss"] += 1
            if out and stats["requests"] % 200 == 0:
                out(f"off: запросов {stats['requests']}, фото {stats['found']}, промахов {stats['miss']}")
            left = interval - (time.monotonic() - started)
            if left > 0:
                sleep(left)
    return stats
