"""ТЗ 29.10: Б-97 выгрузка справочника, Б-98 отметки тестировщиков (Б-99, Б-100 — в отдельных модулях)."""
from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils.dateparse import parse_date, parse_datetime
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import GlobalProduct


def company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


class GlobalProductExportPagination(PageNumberPagination):
    page_size = 500
    page_size_query_param = "page_size"
    max_page_size = 1000


class GlobalProductExportView(APIView):
    """ТЗ 29.10 Б-97. GET main/global-products/export/?since=&page=&page_size="""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = GlobalProduct.objects.select_related("brand", "category").annotate(
            _changed=Coalesce(F("updated_at"), F("created_at"))
        )
        since = (request.query_params.get("since") or "").strip()
        if since:
            dt = parse_datetime(since)
            if dt is None:
                d = parse_date(since)
                if d is None:
                    raise ValidationError({"since": "Ожидается дата или дата-время ISO."})
                import datetime
                dt = datetime.datetime.combine(d, datetime.time.min)
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            qs = qs.filter(_changed__gt=dt)
        qs = qs.order_by("_changed", "id")
        pager = GlobalProductExportPagination()
        page = pager.paginate_queryset(qs, request, view=self)
        results = [
            {
                "barcode": g.barcode,
                "name": g.name,
                "brand": g.brand.name if g.brand_id else None,
                "category": g.category.name if g.category_id else None,
                "updated_at": g._changed.isoformat() if g._changed else None,
            }
            for g in page
        ]
        return Response({
            "count": pager.page.paginator.count,
            "next": pager.get_next_link(),
            "results": results,
        })


# ----------------------------------------------------------------------
# Б-98. main/app-tests/checks/
# ----------------------------------------------------------------------
import re

from .models import AppTestCheck

_DEVICE_RE = re.compile(r"^[A-Za-z0-9]{8}$")


class AppTestChecksView(APIView):
    """ТЗ 29.10 Б-98. POST — поставить/снять отметку, GET ?version= — сводка. Любой сотрудник компании."""

    permission_classes = [IsAuthenticated]

    def _company(self, request):
        c = company_of(request.user)
        if c is None:
            raise ValidationError({"detail": "Пользователь не привязан к компании."})
        return c

    def get(self, request):
        company = self._company(request)
        version = (request.query_params.get("version") or "").strip()
        if not version:
            raise ValidationError({"version": "Укажите version."})
        out = {}
        for c in AppTestCheck.objects.filter(company=company, version=version).order_by("item_id", "updated_at"):
            row = out.setdefault(c.item_id, {"ok_count": 0, "issue_count": 0, "devices": []})
            row["ok_count" if c.status == "ok" else "issue_count"] += 1
            row["devices"].append({
                "device_id": c.device_id, "name": c.name, "status": c.status,
                "ts": c.updated_at.isoformat(), "comment": c.comment,
            })
        return Response(out)

    def post(self, request):
        company = self._company(request)
        d = request.data
        version = str(d.get("version") or "").strip()
        item_id = str(d.get("item_id") or "").strip()
        device_id = str(d.get("device_id") or "").strip()
        status_ = str(d.get("status") or "").strip()
        errors = {}
        if not version or len(version) > 32:
            errors["version"] = "Обязательное поле, до 32 символов."
        if not item_id or len(item_id) > 128:
            errors["item_id"] = "Обязательное поле, до 128 символов."
        if not _DEVICE_RE.match(device_id):
            errors["device_id"] = "Ровно 8 символов (латиница и цифры)."
        if status_ not in ("ok", "issue", "none"):
            errors["status"] = "Допустимо: ok, issue, none."
        if errors:
            raise ValidationError(errors)
        key = dict(company=company, version=version, item_id=item_id, device_id=device_id)
        if status_ == "none":
            AppTestCheck.objects.filter(**key).delete()
            return Response({"item_id": item_id, "device_id": device_id, "status": "none"})
        obj, _ = AppTestCheck.objects.update_or_create(
            **key,
            defaults={
                "status": status_,
                "comment": str(d.get("comment") or "")[:2000],
                "name": str(d.get("name") or "")[:128],
            },
        )
        return Response({
            "item_id": item_id, "device_id": device_id, "status": obj.status,
            "ts": obj.updated_at.isoformat(), "comment": obj.comment,
        })
