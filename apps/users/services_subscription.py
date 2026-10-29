from __future__ import annotations

import zoneinfo
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, Optional

from django.utils import timezone

BISHKEK_TZ = zoneinfo.ZoneInfo("Asia/Bishkek")


def get_bishkek_date(dt_or_date) -> Optional[date]:
    if dt_or_date is None:
        return None
    if isinstance(dt_or_date, datetime):
        return timezone.localtime(dt_or_date, BISHKEK_TZ).date()
    if isinstance(dt_or_date, date):
        return dt_or_date
    return None


def get_bishkek_datetime_str(dt) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return timezone.localtime(dt, BISHKEK_TZ).isoformat()
    return str(dt)


def resolve_plan_code(plan) -> Optional[str]:
    if not plan:
        return None
    raw_code = getattr(plan, "code", None)
    if raw_code:
        return str(raw_code).strip().lower()
    name = (getattr(plan, "name", "") or "").lower().strip()
    if "старт" in name or "start" in name:
        return "start"
    if "стандарт" in name or "standard" in name:
        return "standard"
    return "custom"


def is_user_owner(user, company) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "role", None) in ("owner", "admin"):
        return True
    if hasattr(company, "owner_id") and company.owner_id == user.id:
        return True
    return False


def build_subscription_payload(company, is_owner: bool = True) -> Dict[str, Any]:
    today_local = timezone.localtime(timezone.now(), BISHKEK_TZ).date()

    start_date_local = get_bishkek_date(company.start_date)
    end_date_local = get_bishkek_date(company.end_date)

    started_at_str = start_date_local.isoformat() if start_date_local else None
    end_date_str = end_date_local.isoformat() if end_date_local else None

    if end_date_local is not None:
        days_left = (end_date_local - today_local).days
    else:
        days_left = None

    # status: active | expiring_soon | expired | trial | grace | unknown
    if getattr(company, "is_trial", False):
        status = "trial"
    elif end_date_local is None or days_left is None:
        status = "unknown"
    elif days_left < 0:
        status = "expired"
    elif 0 <= days_left <= 7:
        status = "expiring_soon"
    else:
        status = "active"

    plan = company.subscription_plan
    plan_code = resolve_plan_code(plan)

    if plan:
        plan_data: Dict[str, Any] = {
            "id": str(plan.id) if getattr(plan, "id", None) is not None else None,
            "code": plan_code,
            "name": plan.name,
            "period": getattr(plan, "period", "month") or "month",
            "description": plan.description or "",
        }
        if is_owner:
            plan_data["price"] = f"{plan.price:.2f}" if plan.price is not None else "0.00"
            plan_data["currency"] = "KGS"
    else:
        plan_data = None

    subscription: Dict[str, Any] = {
        "status": status,
        "started_at": started_at_str,
        "end_date": end_date_str,
        "days_left": days_left,
        "is_trial": getattr(company, "is_trial", False),
        "plan": plan_data,
    }

    if is_owner:
        subscription["auto_renew"] = None
        subscription["next_payment_at"] = end_date_str
        subscription["last_payment"] = None

    return subscription


# Сотрудников вместе с владельцем: «Старт» — 3 (доп. услуга extra-employees снимает лимит), «Стандарт» — 10,
# «По договору» — сколько задал оператор в конструкторе support (не задал — без ограничений).
START_EMPLOYEES = 3
STANDARD_EMPLOYEES = 10


def employee_limit(company, plan_code: Optional[str] = None) -> Optional[int]:
    if plan_code is None:
        plan_code = resolve_plan_code(getattr(company, "subscription_plan", None))
    if plan_code == "start":
        from apps.users.models import CompanyAddon

        extra = CompanyAddon.objects.filter(company=company, code="extra-employees").first()
        return None if extra is not None and extra.is_effective() else START_EMPLOYEES
    if plan_code == "standard":
        return STANDARD_EMPLOYEES
    if plan_code == "custom":
        from apps.users.support_views import constructor_max_employees

        return constructor_max_employees(company)
    return None


def employee_limit_error(company) -> Optional[str]:
    """Текст ошибки, если лимит сотрудников тарифа уже набран; иначе None."""
    limit = employee_limit(company)
    if limit is None:
        return None
    used = company.employees.filter(is_active=True).count()
    if used < limit:
        return None
    return f"По тарифу можно не больше {limit} сотрудников вместе с владельцем. Чтобы добавить ещё, смените тариф или обратитесь в поддержку."


def employee_limit_reached_body(company, detail=None) -> Dict[str, Any]:
    """ТЗ 29.10 Б-101: тело 403 employee_limit_reached {limit, current}."""
    return {
        "detail": detail or employee_limit_error(company) or "Лимит сотрудников по тарифу исчерпан.",
        "code": "employee_limit_reached",
        "limit": employee_limit(company),
        "current": company.employees.filter(is_active=True).count(),
    }


def get_company_limits(company, plan_code: Optional[str] = None) -> Dict[str, Any]:
    try:
        emp_used = company.employees.filter(is_active=True).count()
    except Exception:
        emp_used = 0

    emp_max = employee_limit(company, plan_code)

    # QA B36: считаем и модуль магазина (main.Warehouse/Product), и модуль «Склад»
    # (warehouse.Warehouse/WarehouseProduct). Раньше для оптового склада used был по
    # модулю магазина и не совпадал с фактом. Копии одного товара на разных складах
    # (общий catalog_key) считаются одним товаром (D-B36).
    try:
        wh_used = company.warehouses.count()
    except Exception:
        wh_used = 0
    try:
        from apps.warehouse.models import Warehouse as WhWarehouse

        wh_used += WhWarehouse.objects.filter(company=company).count()
    except Exception:
        pass

    try:
        prod_used = company.products.count()
    except Exception:
        prod_used = 0
    try:
        from django.db.models import F
        from django.db.models.functions import Cast, Coalesce
        from django.db.models import CharField

        from apps.warehouse.models import WarehouseProduct

        prod_used += (
            WarehouseProduct.objects.filter(company=company)
            .annotate(
                unique_key=Coalesce(
                    Cast("catalog_key", CharField()), Cast("id", CharField()), output_field=CharField()
                )
            )
            .values("unique_key")
            .distinct()
            .count()
        )
    except Exception:
        pass

    # ТЗ 12.22: лимит филиалов. Поля «максимум филиалов» в тарифе нет, поэтому max=None
    # (без ограничения); как только лимит появится в данных тарифа - подставить сюда.
    try:
        br_used = company.branches.count()
    except Exception:
        br_used = 0

    return {
        "employees": {"used": emp_used, "max": emp_max},
        "branches": {"used": br_used, "max": None},
        "warehouses": {"used": wh_used, "max": None},
        "products": {"used": prod_used, "max": None},
    }


def build_company_subscription_panel_payload(company, is_owner: bool = True) -> Dict[str, Any]:
    sub = build_subscription_payload(company, is_owner=is_owner)
    plan_code = sub["plan"]["code"] if sub.get("plan") else None
    limits = get_company_limits(company, plan_code=plan_code)

    created_at_dt = company.created_at
    company_created_at_str = get_bishkek_datetime_str(created_at_dt)

    return {
        "company_created_at": company_created_at_str,
        "status": sub["status"],
        "days_left": sub["days_left"],
        "started_at": sub["started_at"],
        "end_date": sub["end_date"],
        "next_payment_at": sub.get("next_payment_at"),
        "auto_renew": sub.get("auto_renew"),
        "is_trial": sub["is_trial"],
        "plan": sub["plan"],
        "limits": limits,
        "payments": [],
    }
