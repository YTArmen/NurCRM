"""Б-84: аналитика ящика. Время первого ответа считается только в рабочие часы канала (settings.working_hours)."""
import math
import statistics
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Min, Sum
from django.utils import timezone

from core.exceptions import ApiError

from . import access, services
from .ai import _hm
from .models import AiRun, Channel, Conversation, Message

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _day_range(hours, weekday):
    """(начало, конец) рабочего дня в минутах или None — выходной. Пустые часы — круглосуточно."""
    if not hours:
        return 0, 1440
    if isinstance(hours, dict) and any(d in hours for d in DAYS):
        v = hours.get(DAYS[weekday])
        if not v:
            return None
        if isinstance(v, dict):
            a, b = _hm(v.get("from")), _hm(v.get("to"))
        else:
            a, b = _hm(v[0]), _hm(v[1])
    elif isinstance(hours, dict):
        a, b = _hm(hours.get("from")), _hm(hours.get("to"))
    else:
        return 0, 1440
    if a is None or b is None:
        return 0, 1440
    return a, (b if b > a else b + 1440)


def business_seconds(start, end, hours) -> float:
    if end <= start:
        return 0.0
    if not hours:
        return (end - start).total_seconds()
    s, e = timezone.localtime(start), timezone.localtime(end)
    tz = s.tzinfo
    total, day = 0.0, s.date()
    while day <= e.date():
        rng = _day_range(hours, day.weekday())
        if rng:
            base = datetime.combine(day, time(0, 0), tzinfo=tz)
            a, b = base + timedelta(minutes=rng[0]), base + timedelta(minutes=rng[1])
            lo, hi = max(a, s), min(b, e)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


def _p(values, q):
    if not values:
        return None
    vals = sorted(values)
    return round(vals[max(math.ceil(q * len(vals)) - 1, 0)])


def _median(values):
    return round(statistics.median(values)) if values else None


def _parse_day(raw, end=False):
    if not raw:
        return None
    try:
        d = datetime.strptime(str(raw)[:10], "%Y-%m-%d")
    except ValueError:
        raise ApiError("invalid", "Дата: YYYY-MM-DD.", fields={"date": ["Неверная дата"]})
    d = d + timedelta(days=1) if end else d
    return timezone.make_aware(d)


def build(request, company) -> dict:
    q = request.query_params
    user = request.user
    own_only = not access.is_owner_like(user)
    now = timezone.now()
    start = _parse_day(q.get("date_from")) or now - timedelta(days=30)
    end = _parse_day(q.get("date_to"), end=True) or now + timedelta(seconds=1)
    qs = services.visible_conversations(user, company)
    if own_only:
        qs = qs.filter(assignee=user)
    elif q.get("user"):
        qs = qs.filter(assignee_id=q["user"])
    if q.get("channel"):
        qs = qs.filter(channel_id=q["channel"])
    if q.get("branch"):
        qs = qs.filter(branch_id=q["branch"])
    created = qs.filter(created_at__gte=start, created_at__lt=end)
    closed = qs.filter(closed_at__gte=start, closed_at__lt=end, status="closed")
    conv_ids = list(created.values_list("id", flat=True))
    convs = {c.id: c for c in Conversation.objects.filter(id__in=conv_ids).select_related("channel", "assignee", "order")}

    # первый ответ
    first_in = dict(Message.objects.filter(conversation_id__in=conv_ids, direction="in", sender="contact")
                    .values_list("conversation_id").annotate(t=Min("created_at")))
    outs = (Message.objects.filter(conversation_id__in=conv_ids, direction="out", sender__in=("user", "ai"))
            .exclude(kind="note").order_by("created_at").values_list("conversation_id", "created_at", "sender", "sender_user_id"))
    first_resp = {}
    for cid, at, sender, uid in outs:
        t_in = first_in.get(cid)
        if t_in and cid not in first_resp and at >= t_in:
            first_resp[cid] = (at, sender, uid)
    resp_s, per_conv = [], {}
    for cid, (at, sender, uid) in first_resp.items():
        hours = (convs[cid].channel.settings or {}).get("working_hours")
        per_conv[cid] = business_seconds(first_in[cid], at, hours)
        resp_s.append(per_conv[cid])
    resolution = [(c.closed_at - c.created_at).total_seconds() for c in qs.filter(
        status="closed", closed_at__gte=start, closed_at__lt=end)]

    reasons = dict(closed.values_list("closed_reason").annotate(n=Count("id")))
    sold = reasons.get("sold", 0)
    by_user = {}
    for cid in conv_ids:
        c = convs[cid]
        key = c.assignee_id
        row = by_user.setdefault(key, {"user": {"id": str(c.assignee_id), "name": _name(c.assignee)} if key else None,
                                       "conversations": 0, "_r": [], "sold": 0})
        row["conversations"] += 1
        if cid in per_conv:
            row["_r"].append(per_conv[cid])
        if c.status == "closed" and c.closed_reason == "sold":
            row["sold"] += 1
    by_channel = {}
    for cid in conv_ids:
        c = convs[cid]
        row = by_channel.setdefault(c.channel_id, {"channel": {"id": str(c.channel_id), "kind": c.channel.kind,
                                                               "name": c.channel.name}, "conversations": 0, "_r": [], "sold": 0})
        row["conversations"] += 1
        if cid in per_conv:
            row["_r"].append(per_conv[cid])
        if c.status == "closed" and c.closed_reason == "sold":
            row["sold"] += 1
    by_hour = [0] * 24
    for (at,) in Message.objects.filter(conversation__in=qs, direction="in", sender="contact", created_at__gte=start,
                                        created_at__lt=end).values_list("created_at"):
        by_hour[timezone.localtime(at).hour] += 1

    orders = [c.order for c in convs.values() if c.order_id]
    sales = [o for o in orders if o.sale_id]
    revenue = Decimal("0")
    if sales:
        from apps.main.models import Sale
        revenue = Sale.objects.filter(id__in=[o.sale_id for o in sales]).aggregate(s=Sum("total"))["s"] or Decimal("0")
    n = len(conv_ids)
    runs = AiRun.objects.filter(company=company, conversation__in=qs, created_at__gte=start, created_at__lt=end)
    return {
        "period": {"date_from": timezone.localtime(start).date().isoformat(),
                   "date_to": timezone.localtime(end - timedelta(seconds=1)).date().isoformat()},
        "scope": "own" if own_only else "company",
        "first_response": {"median_s": _median(resp_s), "p90_s": _p(resp_s, 0.9)},
        "resolution": {"median_s": _median(resolution)},
        "conversations": {"new": n, "closed": closed.count(), "sold": sold, "no_answer": reasons.get("no_answer", 0),
                          "lost": sum(v for k, v in reasons.items() if k not in ("sold", "no_answer"))},
        "waiting_now": qs.filter(status="open", waiting_since__isnull=False).count(),
        "by_user": [_fin(r) for r in by_user.values()],
        "by_channel": [_fin(r) for r in by_channel.values()],
        "by_hour": by_hour,
        "conversion": {"to_order": round(len(orders) / n, 4) if n else 0, "to_sale": round(len(sales) / n, 4) if n else 0,
                       "orders": len(orders), "sales": len(sales)},
        "revenue_from_chats": str(revenue.quantize(Decimal("0.01"))),
        "ai": {"answered": runs.filter(outcome="answered", sent_by="auto").count(),
               "drafts_sent": runs.filter(sent_by="user").count(), "handed_off": runs.filter(outcome="handoff").count()},
    }


def _name(u):
    return f"{u.first_name or ''} {u.last_name or ''}".strip() or getattr(u, "email", "") if u else ""


def _fin(row):
    r = row.pop("_r")
    row["median_first_response_s"] = _median(r)
    return row
