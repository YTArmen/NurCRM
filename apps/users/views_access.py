"""ТЗ Н-16: сброс пароля сотрудника владельцем/админом и коды доступа сотрудников на сервере."""
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import EmployeeAccessCode, User
from apps.users.permissions_flags import denied, is_privileged
from apps.users.serializers import _generate_password

VERIFY_MAX_FAILS = 5
VERIFY_WINDOW_SEC = 60


class TooManyAttempts(APIException):
    status_code = 429
    default_code = "too_many_attempts"
    default_detail = {"code": "too_many_attempts", "detail": "Слишком много попыток. Подождите минуту."}


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _employee_of_company(request, pk):
    company = _company(request.user)
    return get_object_or_404(
        User.objects.filter(deleted_at__isnull=True).filter(company=company), pk=pk
    ) if company else get_object_or_404(User.objects.none(), pk=pk)


def _require_owner_admin(request):
    if not is_privileged(request.user):
        raise denied("role:owner|admin", "Только владелец или администратор.")


def _name(u):
    return (f"{u.first_name or ''} {u.last_name or ''}".strip()) or u.email


def _audit(request, action, target, after=None):
    from apps.construction.audit import log_request

    try:
        log_request(request, action, "user", target.pk, _name(target), after=after)
    except Exception:
        pass


class EmployeeResetPasswordAPIView(APIView):
    """POST users/employees/{id}/reset-password/ -> {"password"} (показывается один раз)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        _require_owner_admin(request)
        target = _employee_of_company(request, pk)
        if target.role == "owner" and not request.user.is_superuser and target.pk != request.user.pk:
            raise denied("role:owner", "Пароль владельца сбрасывает только он сам.")
        password = _generate_password()
        target.set_password(password)
        target.save(update_fields=["password"])
        _audit(request, "reset_password", target)  # сам пароль в журнал не пишем
        return Response({"password": password}, status=status.HTTP_200_OK)


class EmployeeAccessCodesAPIView(APIView):
    """GET users/employees/{id}/access-codes/ -> {"actions": [...]} (самих кодов нет)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        _require_owner_admin(request)
        target = _employee_of_company(request, pk)
        actions = sorted(EmployeeAccessCode.objects.filter(employee=target).values_list("action", flat=True))
        return Response({"employee": str(target.pk), "actions": actions})


class EmployeeAccessCodeDetailAPIView(APIView):
    """PUT users/employees/{id}/access-codes/{action}/ {"code"}; DELETE — убрать код."""

    permission_classes = [permissions.IsAuthenticated]

    def put(self, request, pk, action):
        _require_owner_admin(request)
        target = _employee_of_company(request, pk)
        code = str(request.data.get("code") or "").strip()
        if not (4 <= len(code) <= 32):
            raise ValidationError({"code": "invalid", "detail": "Код — от 4 до 32 символов."})
        EmployeeAccessCode.objects.update_or_create(
            employee=target, action=action,
            defaults={"company": _company(request.user), "code_hash": make_password(code), "updated_by": request.user},
        )
        _audit(request, "access_code_set", target, after={"action": action})
        return Response({"ok": True, "action": action})

    def delete(self, request, pk, action):
        _require_owner_admin(request)
        target = _employee_of_company(request, pk)
        EmployeeAccessCode.objects.filter(employee=target, action=action).delete()
        _audit(request, "access_code_delete", target, after={"action": action})
        return Response(status=status.HTTP_204_NO_CONTENT)


class AccessCodeVerifyAPIView(APIView):
    """POST users/access-codes/verify/ {"action","code"} -> {"ok":true,"employee":{id,name}} | 400 code_invalid.

    Проверять может любой сотрудник компании (кассир вводит код старшего). 5 неверных попыток
    в минуту на сотрудника-вызывающего, дальше 429."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        company = _company(request.user)
        action = str(request.data.get("action") or "").strip()
        code = str(request.data.get("code") or "").strip()
        if company is None or not action or not code:
            raise ValidationError({"code": "code_invalid", "detail": "Нужны action и code."})
        key = f"acc_verify_fail:{request.user.pk}"
        fails = cache.get(key, 0)
        if fails >= VERIFY_MAX_FAILS:
            raise TooManyAttempts()
        for row in EmployeeAccessCode.objects.filter(company=company, action=action).select_related("employee"):
            if row.employee.is_active and not row.employee.deleted_at and check_password(code, row.code_hash):
                cache.delete(key)
                return Response({"ok": True, "employee": {"id": str(row.employee_id), "name": _name(row.employee)}})
        if cache.add(key, 1, VERIFY_WINDOW_SEC) is False:
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, VERIFY_WINDOW_SEC)
        return Response({"code": "code_invalid", "detail": "Неверный код."}, status=status.HTTP_400_BAD_REQUEST)
