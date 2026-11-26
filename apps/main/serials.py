"""13.11.2026: серийные номера и гарантия (сфера «Электроника», флаг electronics_2026_11).

- строки корзины/продажи хранят serial_numbers (список строк);
- каждая продажа серийника — запись SoldSerial (поиск продаж, гарантия, запрет повторной продажи);
- возврат помечает серийники возвращёнными, аннулирование/правка возврата — снимает пометку.
Поиск идёт по связи с продажей, а не по номеру чека, поэтому перенумерованный офлайн-чек тоже находится.
"""
import calendar
import datetime
import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

log = logging.getLogger(__name__)

ELECTRONICS_FLAG = "electronics_2026_11"
MAX_SERIALS_PER_LINE = 500


class SerialError(Exception):
    def __init__(self, code, detail, **extra):
        super().__init__(detail)
        self.code, self.detail, self.extra = code, detail, extra


def norm(value):
    return "".join(str(value or "").split()).upper()


def parse_serials(item):
    """serial_numbers (или serials) из строки запроса → список строк без пустых и повторов, или None."""
    if not isinstance(item, dict):
        return None
    raw = item.get("serial_numbers")
    if raw is None:
        raw = item.get("serials")
    if raw in (None, "", []):
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise SerialError("validation_error", "serial_numbers: ожидается список строк.")
    if len(raw) > MAX_SERIALS_PER_LINE:
        raise SerialError("validation_error", f"Не больше {MAX_SERIALS_PER_LINE} серийных номеров в строке.")
    out, seen = [], set()
    for s in raw:
        s = str(s or "").strip()
        if not s:
            continue
        if len(s) > 64:
            raise SerialError("serial_too_long", "Серийный номер длиннее 64 символов.", serial=s[:64])
        n = norm(s)
        if n in seen:
            raise SerialError("serial_duplicate_in_line", f"Серийный номер {s} указан дважды.", serial=s)
        seen.add(n)
        out.append(s)
    return out or None


def is_electronics_strict(request, company):
    """Проверки серийников — только с флагом компании и только для новых клиентов (как hardware_2026_11)."""
    from core.api_compat import company_flag, is_new_client

    return bool(company is not None and request is not None
                and company_flag(company, ELECTRONICS_FLAG) and is_new_client(request))


def active_sale_of(company_id, serial, exclude_cart_item=None):
    from apps.main.models import SoldSerial

    return (SoldSerial.objects.filter(company_id=company_id, serial_norm=norm(serial), returned_at__isnull=True)
            .order_by("-sold_at").first())


def check_line_serials(request, company, serials, qty, *, offline=False):
    """Онлайн-проверка строки: серийников не больше количества и ни один не продан (не возвращён)."""
    if not serials or offline or not is_electronics_strict(request, company):
        return
    try:
        too_many = len(serials) > qty
    except TypeError:
        too_many = False
    if too_many:
        raise SerialError("serial_count_mismatch", "Серийных номеров больше, чем количество в строке.",
                          count=len(serials), quantity=str(qty))
    for s in serials:
        sold = active_sale_of(company.id, s)
        if sold is not None:
            raise SerialError("serial_already_sold", f"Серийный номер {s} уже продан и не возвращён.",
                              serial=s, sale_id=str(sold.sale_id))


def merge_serials(a, b):
    out = list(a or [])
    have = {norm(x) for x in out}
    for s in b or []:
        if norm(s) not in have:
            out.append(s)
            have.add(norm(s))
    return out or None


def add_months(d, months):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def warranty_until_for(product, when=None):
    months = getattr(product, "warranty_months", None) if product is not None else None
    if not months:
        return None
    return add_months(timezone.localdate(when) if when else timezone.localdate(), int(months))


def record_sold_serials(sale, sale_items):
    """После создания строк продажи: записи SoldSerial. Ошибка здесь не ломает продажу."""
    from apps.main.models import SoldSerial

    try:
        with transaction.atomic():
            rows = []
            for si in sale_items:
                for s in si.serial_numbers or []:
                    n = norm(s)
                    dup = SoldSerial.objects.filter(
                        company_id=sale.company_id, serial_norm=n, returned_at__isnull=True
                    ).exists()
                    rows.append(SoldSerial(
                        company_id=sale.company_id, sale=sale, sale_item=si, product_id=si.product_id,
                        serial=s, serial_norm=n, sold_at=sale.created_at or timezone.now(),
                        warranty_until=si.warranty_until, duplicate=dup,
                    ))
            if rows:
                SoldSerial.objects.bulk_create(rows)
    except Exception:
        log.exception("serials: не удалось записать серийники продажи %s", sale.pk)


def mark_returned(sale, partial_items, payload, sale_return):
    """Возврат: полный — все серийники чека; частичный — присланные serial_numbers строки,
    а если их нет — первые N по количеству возврата."""
    from apps.main.models import SoldSerial

    try:
        with transaction.atomic():
            qs = SoldSerial.objects.filter(sale=sale, returned_at__isnull=True)
            if partial_items is None:
                ids = list(qs.values_list("id", flat=True))
            else:
                given = {}
                for row in (payload or {}).get("items") or []:
                    if not isinstance(row, dict):
                        continue
                    sid = str(row.get("sale_item_id") or row.get("id") or "")
                    ser = row.get("serial_numbers") or row.get("serials")
                    if sid and ser:
                        given[sid] = {norm(x) for x in (ser if isinstance(ser, list) else [ser])}
                ids = []
                for uid, q in partial_items:
                    rows = list(qs.filter(sale_item_id=uid).order_by("created_at", "id"))
                    if str(uid) in given:
                        ids += [r.id for r in rows if r.serial_norm in given[str(uid)]]
                    else:
                        ids += [r.id for r in rows[: int(q)]]
            if ids:
                SoldSerial.objects.filter(id__in=ids).update(returned_at=timezone.now(), sale_return=sale_return)
    except Exception:
        log.exception("serials: не удалось отметить возврат серийников чека %s", sale.pk)


def unmark_return(sale_return):
    from apps.main.models import SoldSerial

    SoldSerial.objects.filter(sale_return=sale_return).update(returned_at=None, sale_return=None)


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _sold_json(r):
    sale = r.sale
    today = timezone.localdate()
    return {
        "serial": r.serial,
        "sale_id": str(r.sale_id),
        "sale_number": getattr(sale, "doc_number", None),
        "sale_status": getattr(sale, "status", None),
        "sale_item_id": str(r.sale_item_id),
        "product_id": str(r.product_id) if r.product_id else None,
        "name": getattr(r.sale_item, "name_snapshot", None),
        "sold_at": r.sold_at.isoformat() if r.sold_at else None,
        "warranty_until": r.warranty_until.isoformat() if r.warranty_until else None,
        "warranty_active": bool(r.warranty_until and r.warranty_until >= today),
        "returned": r.returned_at is not None,
        "returned_at": r.returned_at.isoformat() if r.returned_at else None,
        "sale_return_id": str(r.sale_return_id) if r.sale_return_id else None,
        "duplicate": r.duplicate,
    }


class SerialLookupAPIView(APIView):
    """GET api/main/serials/?serial=<номер> — продажи с этим серийником (новые сверху)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import SoldSerial

        company = _company(request.user)
        serial = norm(request.query_params.get("serial") or request.query_params.get("q")
                      or request.query_params.get("tail"))  # qf1311
        if company is None or len(serial) < 2:
            return Response({"code": "validation_error", "detail": "Укажите serial (не короче 2 символов)."},
                            status=400)
        tail = norm(request.query_params.get("tail"))  # qf1311: поиск по хвосту
        if tail or (request.query_params.get("match") or "").lower() == "suffix":
            serial = tail or serial
            if len(serial) < 4:
                return Response({"code": "validation_error", "detail": "Хвост номера — не короче 4 символов."},
                                status=400)
            flt = {"serial_norm__endswith": serial}
        else:
            flt = {"serial_norm": serial}
        rows = (SoldSerial.objects.filter(company=company, **flt)
                .select_related("sale", "sale_item").order_by("-sold_at")[:50])
        data = [_sold_json(r) for r in rows]
        return Response({"serial": serial, "sold": any(not d["returned"] for d in data), "results": data})


class CartItemSerialsAPIView(APIView):
    """POST api/main/pos/sales/<cart>/items/<item>/serials/ {serial_numbers: [...]} — заменить серийники строки."""

    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, pk, item_id):
        from django.shortcuts import get_object_or_404

        from apps.main.models import Cart, CartItem

        company = _company(request.user)
        cart = get_object_or_404(Cart.objects.select_for_update(), id=pk, company=company, status=Cart.Status.ACTIVE)
        item = get_object_or_404(CartItem.objects.select_related("product"), id=item_id, cart=cart)
        try:
            serials = parse_serials(request.data if hasattr(request.data, "get") else {})
            check_line_serials(request, company, serials, item.quantity)
        except SerialError as e:
            return Response({"code": e.code, "detail": e.detail, **e.extra}, status=400)
        item.serial_numbers = serials
        item.save(update_fields=["serial_numbers"])
        return Response({"id": str(item.id), "serial_numbers": item.serial_numbers or []})
