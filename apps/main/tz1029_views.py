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
