"""Б-67/Б-68: фильтры и ETag списка pos/sales/, поля источника в сериализаторах."""
import hashlib

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, DateTimeField, Max, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from apps.main.tz26.pos_sync.services import DEVICE_KEY_RE, SOURCES

ETAG_VERSION = "v1"


def parse_iso_param(request, name):
    """ISO 8601 со смещением (плюс в query приходит пробелом) -> aware datetime или None."""
    raw = (request.query_params.get(name) or "").strip()
    if not raw:
        return None
    dt = parse_datetime(raw.replace(" ", "+"))
    if dt is None:
        raise ValidationError({name: "Ожидается дата-время ISO 8601."})
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def last_change_expr():
    """
    Когда чек последний раз менялся: создан/оплачен/принят сервером (досылка офлайна),
    возврат, платёж по долгу. Поля Sale.updated_at в модели нет.
    """
    from apps.main.models import SalePayment, SaleReturn

    ret = (
        SaleReturn.objects.filter(sale=OuterRef("pk")).order_by().values("sale")
        .annotate(m=Max("created_at")).values("m")
    )
    pay = (
        SalePayment.objects.filter(sale=OuterRef("pk")).order_by().values("sale")
        .annotate(m=Max("created_at")).values("m")
    )
    f = DateTimeField()
    return Greatest(
        "created_at",
        Coalesce("paid_at", "created_at"),
        Coalesce("received_at", "created_at"),
        Coalesce(Subquery(ret, output_field=f), "created_at"),
        Coalesce(Subquery(pay, output_field=f), "created_at"),
        output_field=f,
    )


def apply_sync_filters(qs, request):
    """?source= (можно через запятую) &device_key= &created_by= &updated_after="""
    qp = request.query_params

    raw = (qp.get("source") or "").strip()
    if raw:
        values = [x.strip() for x in raw.split(",") if x.strip()]
        bad = [x for x in values if x not in SOURCES]
        if bad:
            raise ValidationError({"source": [f"Допустимо: {', '.join(SOURCES)}."]})
        qs = qs.filter(source__in=values)

    device_key = (qp.get("device_key") or "").strip()
    if device_key:
        if not DEVICE_KEY_RE.match(device_key):
            raise ValidationError({"device_key": ["8–64 символа: латиница, цифры, _ и -."]})
        qs = qs.filter(device_key=device_key)

    created_by = (qp.get("created_by") or "").strip()
    if created_by:
        try:
            qs = qs.filter(user_id=created_by)
        except (ValueError, TypeError, DjangoValidationError) as exc:
            raise ValidationError({"created_by": ["Некорректный id сотрудника."]}) from exc

    updated_after = parse_iso_param(request, "updated_after")
    if updated_after is not None:
        qs = qs.annotate(last_change_at=last_change_expr()).filter(last_change_at__gte=updated_after)
    return qs


def sales_etag(request, queryset):
    """Слабый ETag по сводке отфильтрованного набора + параметрам запроса. Без сериализации страницы."""
    from apps.main.models import SaleReturn

    try:
        qs = queryset.order_by()
        agg = qs.aggregate(
            n=Count("id"), c=Max("created_at"), p=Max("paid_at"), r=Max("received_at"),
            t=Sum("total"), d=Sum("debt_remaining"), pn=Sum("paid_now"),
        )
        ret = SaleReturn.objects.filter(sale__in=qs.values("pk")).aggregate(m=Max("created_at"), n=Count("id"))
    except Exception:  # noqa: BLE001 — ETag не должен ронять список
        return None
    params = "&".join(f"{k}={v}" for k, v in sorted(request.query_params.items()))
    raw = "|".join([
        ETAG_VERSION, str(getattr(request.user, "pk", "")), params,
        *[str(agg[k]) for k in ("n", "c", "p", "r", "t", "d", "pn")], str(ret["m"]), str(ret["n"]),
    ])
    return 'W/"%s"' % hashlib.sha1(raw.encode("utf-8")).hexdigest()


def etag_matches(request, etag) -> bool:
    header = request.META.get("HTTP_IF_NONE_MATCH", "")
    if not etag or not header:
        return False
    if header.strip() == "*":
        return True
    weak = lambda v: v.strip()[2:] if v.strip().startswith("W/") else v.strip()  # noqa: E731
    return weak(etag) in {weak(x) for x in header.split(",")}
