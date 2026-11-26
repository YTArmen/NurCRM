"""
Активность компаний для админа платформы.

Считает, сколько разных дней за 30/90 дней компания что-то создавала
(продажи, товары, корзины, сделки, касса и т.д.) по всем таблицам с
company_id + created_at. Служебные таблицы, которые наполняются сами
(уведомления, кэши, журналы), не учитываются.
"""
from django.apps import apps
from django.core.cache import cache
from django.db import connection, models
from django.utils import timezone

from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Company
from .permissions import IsPlatformAdmin

# Таблицы, куда записи попадают без участия пользователя.
EXCLUDED_TABLES = {
    "main_notification",
    "main_recommendationpairscache",
    "construction_cashflowcategory",
    "clientapp_appshopsettings",
    "construction_auditlog",
    "integrations_idempotencyrecord",
}

CACHE_KEY = "platform_admin:company_activity:v1"
CACHE_TTL = 10 * 60


def _activity_tables():
    tables = set()
    for model in apps.get_models():
        meta = model._meta
        if meta.proxy or not meta.managed or meta.db_table in EXCLUDED_TABLES:
            continue
        try:
            company_field = meta.get_field("company")
            created_field = meta.get_field("created_at")
        except Exception:
            continue
        if not isinstance(company_field, models.ForeignKey) or company_field.related_model is not Company:
            continue
        if not isinstance(created_field, models.DateTimeField):
            continue
        tables.add(meta.db_table)
    return sorted(tables)


def _compute():
    tables = _activity_tables()
    qn = connection.ops.quote_name
    union = " UNION ALL ".join(
        f"SELECT company_id, created_at FROM {qn(t)} WHERE created_at > now() - interval '90 days'"
        for t in tables
    )
    sql = f"""
        WITH ev AS ({union}),
        agg AS (
            SELECT company_id,
                   count(DISTINCT created_at::date) FILTER (WHERE created_at > now() - interval '30 days') AS days30,
                   count(*) FILTER (WHERE created_at > now() - interval '30 days') AS rows30,
                   count(DISTINCT created_at::date) AS days90,
                   max(created_at) AS last_activity
            FROM ev GROUP BY company_id
        ),
        uc AS (
            SELECT company_id, count(*) AS users
            FROM users_user WHERE deleted_at IS NULL GROUP BY company_id
        )
        SELECT c.id, c.name, c.is_active, c.created_at, c.end_date,
               coalesce(s.name, '') AS sector, coalesce(o.email, '') AS owner_email,
               coalesce(nullif(c.phone, ''), o.phone_number, '') AS phone,
               coalesce(uc.users, 0), coalesce(a.days30, 0), coalesce(a.rows30, 0),
               coalesce(a.days90, 0), a.last_activity
        FROM users_company c
        LEFT JOIN users_sector s ON s.id = c.sector_id
        LEFT JOIN users_user o ON o.id = c.owner_id
        LEFT JOIN uc ON uc.company_id = c.id
        LEFT JOIN agg a ON a.company_id = c.id
        ORDER BY coalesce(a.days30, 0) DESC, coalesce(a.days90, 0) DESC, c.name
    """
    with connection.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    def iso(v):
        return v.isoformat() if v else None

    return {
        "computed_at": timezone.now().isoformat(),
        "tables_count": len(tables),
        "results": [
            {
                "id": str(r[0]),
                "name": r[1],
                "is_active": r[2],
                "created_at": iso(r[3]),
                "end_date": iso(r[4]),
                "sector": r[5],
                "owner_email": r[6],
                "phone": r[7],
                "users": r[8],
                "days30": r[9],
                "rows30": r[10],
                "days90": r[11],
                "last_activity": iso(r[12]),
            }
            for r in rows
        ],
    }


class PlatformAdminCompanyActivityAPIView(APIView):
    """GET /api/platform-admin/activity/  (?refresh=1 — пересчитать без кэша)."""
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        data = None if request.query_params.get("refresh") else cache.get(CACHE_KEY)
        if data is None:
            data = _compute()
            cache.set(CACHE_KEY, data, CACHE_TTL)
        return Response(data)
