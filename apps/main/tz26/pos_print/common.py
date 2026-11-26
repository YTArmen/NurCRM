"""Общие помощники куска pos_print."""
import re

from django.core.paginator import EmptyPage, Paginator
from django.utils import timezone
from rest_framework.response import Response

from apps.utils import _is_owner_like
from core.exceptions import ApiError

DEVICE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def company_of(request):
    user = request.user
    company = getattr(user, "owned_company", None) or getattr(user, "company", None)
    if company is None:
        raise ApiError("validation_error", "Пользователь не привязан к компании.", status_code=400)
    return company


def user_branch_id(user):
    try:
        b = user.primary_branch
        return b.pk if b else None
    except Exception:  # noqa: BLE001
        return None


def branch_ok(user, branch_id):
    """Устройство видит задания своего филиала (или без филиала). Владелец/админ — любые."""
    if _is_owner_like(user) or not branch_id:
        return True
    mine = user_branch_id(user)
    return mine is None or str(mine) == str(branch_id)


def iso(dt):
    return timezone.localtime(dt).isoformat(timespec="seconds") if dt else None


def clean_device_key(raw, *, field="device_key", required=False):
    v = str(raw or "").strip()
    if not v:
        if required:
            raise ApiError("validation_error", "Укажите device_key.", fields={field: ["Обязательное поле."]})
        return ""
    if not DEVICE_KEY_RE.match(v):
        raise ApiError("validation_error", "device_key: 8–64 символа [A-Za-z0-9_-].",
                       fields={field: ["Неверный формат."]})
    return v


def require_idem_key(request):
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key and isinstance(request.data, dict):
        key = str(request.data.get("idempotency_key") or "").strip()
    if not key:
        raise ApiError("idempotency_key_required", "Нужен заголовок Idempotency-Key.",
                       fields={"Idempotency-Key": ["Обязательный заголовок."]})
    return key


def get_device_cashbox_id(company, device_key):
    """Касса, к которой привязано устройство (Б-70). Модели Device может ещё не быть — тогда None."""
    if not device_key:
        return None
    try:
        from apps.main.tz26.pos_sync.models import Device  # noqa: WPS433
    except Exception:  # noqa: BLE001
        return None
    try:
        dev = Device.objects.filter(company=company, device_key=device_key).first()
        return getattr(dev, "cashbox_id", None) if dev else None
    except Exception:  # noqa: BLE001
        return None


def paginate(request, qs, serialize, *, default_size=100):
    """Список в формате О4: {count,next,previous,results}; страница за последней — пустой results."""
    try:
        size = max(1, min(int(request.query_params.get("page_size") or default_size), 500))
    except (TypeError, ValueError):
        size = default_size
    try:
        page_no = max(1, int(request.query_params.get("page") or 1))
    except (TypeError, ValueError):
        page_no = 1
    pager = Paginator(qs, size)
    try:
        page = pager.page(page_no)
        rows = list(page.object_list)
    except EmptyPage:
        rows = []

    def link(n):
        q = request.query_params.copy()
        q["page"] = n
        return request.build_absolute_uri(request.path) + "?" + q.urlencode()

    return Response({
        "count": pager.count,
        "next": link(page_no + 1) if page_no < pager.num_pages else None,
        "previous": link(page_no - 1) if page_no > 1 and rows else None,
        "results": [serialize(r) for r in rows],
    })
