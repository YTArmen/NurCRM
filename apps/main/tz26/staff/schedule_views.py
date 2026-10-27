"""Б-64: плановый график смен (staff-schedule/) и табель (staff/timesheet/)."""
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.idempotency import idempotent
from apps.main.models import _user_display_name
from core.exceptions import ApiError

from . import perms
from .models import StaffSchedule

User = get_user_model()


def _company(user):
    c = perms.company_of(user)
    if c is None:
        raise ApiError("permission_denied", "Пользователь не привязан к компании.", status_code=403)
    return c


def _uuid(raw, code="employee_not_found"):
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        raise ApiError(code, "Неверный идентификатор.", status_code=404 if code.endswith("not_found") else 400)


def _dates(q):
    df, dt = parse_date(q.get("date_from") or ""), parse_date(q.get("date_to") or "")
    if (q.get("date_from") and df is None) or (q.get("date_to") and dt is None) or (df and dt and df > dt):
        raise ApiError("period_invalid", "Период указан неверно (YYYY-MM-DD).")
    return df, dt


def _ser(s):
    return {
        "id": str(s.id), "employee": str(s.employee_id), "employee_name": _user_display_name(s.employee),
        "date": s.date.isoformat(), "start": s.start.strftime("%H:%M"), "end": s.end.strftime("%H:%M"),
        "branch": str(s.branch_id) if s.branch_id else None,
    }


class StaffScheduleListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company, user = _company(request.user), request.user
        qs = StaffSchedule.objects.filter(company=company).select_related("employee")
        if not perms.can_view_staff(user):
            qs = qs.filter(employee=user)
        q = request.query_params
        if q.get("employee"):
            qs = qs.filter(employee_id=_uuid(q["employee"]))
        if q.get("branch"):
            qs = qs.filter(branch_id=_uuid(q["branch"], "branch_not_found"))
        df, dt = _dates(q)
        if df:
            qs = qs.filter(date__gte=df)
        if dt:
            qs = qs.filter(date__lte=dt)
        return Response([_ser(s) for s in qs.order_by("date", "start", "id")[:2000]])

    @idempotent(body_key=True, replay_flag=True)
    def post(self, request):
        company, user = _company(request.user), request.user
        if not perms.can_edit_schedule(user):
            raise ApiError("permission_denied", "Нет доступа: график смен.", status_code=403,
                           top={"required": "can_edit_employees"})
        s = _parse(request.data, company)
        obj = StaffSchedule.objects.create(company=company, created_by=user, **s)
        return Response(_ser(obj), status=status.HTTP_201_CREATED)


def _parse(d, company, partial=False):
    out = {}
    if "employee" in d or not partial:
        emp = User.objects.filter(id=_uuid(d.get("employee")), company=company).first()
        if emp is None:
            raise ApiError("employee_not_found", "Сотрудник не найден.", status_code=404)
        out["employee"] = emp
    for key, parser in (("date", parse_date), ("start", parse_time), ("end", parse_time)):
        if key in d or not partial:
            try:
                val = parser(str(d.get(key) or ""))
            except ValueError:
                val = None
            if val is None:
                raise ApiError("schedule_invalid", f"Поле {key} указано неверно.", fields={key: ["Неверное значение."]})
            out[key] = val
    if "branch" in d or not partial:
        if d.get("branch"):
            from apps.users.models import Branch

            b = Branch.objects.filter(id=_uuid(d["branch"], "branch_not_found"), company=company).first()
            if b is None:
                raise ApiError("branch_not_found", "Филиал не найден.", status_code=404)
            out["branch"] = b
        else:
            out["branch"] = None
    return out


class StaffScheduleDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _obj(self, request, pk):
        if not perms.can_edit_schedule(request.user):
            raise ApiError("permission_denied", "Нет доступа: график смен.", status_code=403,
                           top={"required": "can_edit_employees"})
        o = StaffSchedule.objects.filter(pk=pk, company=_company(request.user)).select_related("employee").first()
        if o is None:
            raise ApiError("not_found", "Запись графика не найдена.", status_code=404)
        return o

    def patch(self, request, pk):
        o = self._obj(request, pk)
        for k, v in _parse(request.data, o.company, partial=True).items():
            setattr(o, k, v)
        o.save()
        return Response(_ser(o))

    def delete(self, request, pk):
        self._obj(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffTimesheetView(APIView):
    """GET staff/timesheet/?date_from&date_to&employee= — по дням план/факт и итоги по сотруднику."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.construction.models import CashShift

        company, user = _company(request.user), request.user
        q = request.query_params
        df, dt = _dates(q)
        today = timezone.localdate()
        df = df or today.replace(day=1)
        dt = dt or today
        if (dt - df).days > 366:
            raise ApiError("period_invalid", "Период не больше 366 дней.")
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(df, datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(dt + timedelta(days=1), datetime.min.time()), tz)

        emp_id = None
        if q.get("employee"):
            emp_id = _uuid(q["employee"])
        if not perms.can_view_staff(user):
            if emp_id and emp_id != user.id:
                raise ApiError("permission_denied", "Нет доступа: сотрудники.", status_code=403,
                               top={"required": "can_view_employees"})
            emp_id = user.id

        sched = StaffSchedule.objects.filter(company=company, date__gte=df, date__lte=dt)
        shifts = CashShift.objects.filter(company=company, opened_at__gte=start, opened_at__lt=end)
        if emp_id:
            sched, shifts = sched.filter(employee_id=emp_id), shifts.filter(cashier_id=emp_id)

        days = {}  # (employee, date) -> dict
        for s in sched:
            d = days.setdefault((s.employee_id, s.date), {"plans": [], "shifts": []})
            d["plans"].append(s)
        for sh in shifts:
            d = days.setdefault((sh.cashier_id, timezone.localtime(sh.opened_at).date()), {"plans": [], "shifts": []})
            d["shifts"].append(sh)

        now = timezone.now()
        by_emp = {}
        for (eid, day), d in sorted(days.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
            plans = sorted(d["plans"], key=lambda p: p.start)
            shs = sorted(d["shifts"], key=lambda x: x.opened_at)
            row = {"date": day.isoformat(), "planned_start": None, "planned_end": None, "opened_at": None,
                   "closed_at": None, "late_minutes": 0, "worked_minutes": 0, "planned_minutes": 0,
                   "revenue": "0.00", "shifts": len(shs), "absent": False}
            if plans:
                row["planned_start"] = plans[0].start.strftime("%H:%M")
                row["planned_end"] = plans[-1].end.strftime("%H:%M")
                for p in plans:
                    mins = (p.end.hour * 60 + p.end.minute) - (p.start.hour * 60 + p.start.minute)
                    row["planned_minutes"] += mins + 1440 if mins <= 0 else mins
            if shs:
                first = timezone.localtime(shs[0].opened_at)
                row["opened_at"] = first.isoformat()
                closes = [x.closed_at for x in shs if x.closed_at]
                if closes and all(x.closed_at for x in shs):
                    row["closed_at"] = timezone.localtime(max(closes)).isoformat()
                worked = sum(((x.closed_at or now) - x.opened_at).total_seconds() for x in shs)
                row["worked_minutes"] = int(worked // 60)
                row["revenue"] = f"{sum((x.sales_total or Decimal('0') for x in shs), Decimal('0')):.2f}"
                if plans:
                    planned = timezone.make_aware(datetime.combine(day, plans[0].start), tz)
                    row["late_minutes"] = max(int((first - planned).total_seconds() // 60), 0)
            elif plans and day < timezone.localdate():
                row["absent"] = True
            by_emp.setdefault(eid, []).append(row)

        names = {u.id: _user_display_name(u) for u in User.objects.filter(id__in=list(by_emp))}
        results = []
        for eid, rows in by_emp.items():
            results.append({
                "employee": str(eid), "employee_name": names.get(eid, ""), "days": rows,
                "totals": {
                    "planned_minutes": sum(r["planned_minutes"] for r in rows),
                    "worked_minutes": sum(r["worked_minutes"] for r in rows),
                    "late_minutes": sum(r["late_minutes"] for r in rows),
                    "late_days": sum(1 for r in rows if r["late_minutes"] > 0),
                    "absent_days": sum(1 for r in rows if r["absent"]),
                    "shifts": sum(r["shifts"] for r in rows),
                    "revenue": f"{sum((Decimal(r['revenue']) for r in rows), Decimal('0')):.2f}",
                },
            })
        results.sort(key=lambda x: x["employee_name"].lower())
        return Response({"date_from": df.isoformat(), "date_to": dt.isoformat(), "results": results})
