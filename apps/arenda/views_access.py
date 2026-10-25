"""Этап 5: доступы сотрудников, «глазами сотрудника», пароль, вход, журнал действий (ТЗ §2.2, §3.7, §3.11)."""
import re
from datetime import datetime, time

from django.conf import settings as dj_settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import Branch, Roles, User
from core.exceptions import ApiError

from . import audit, security
from .constants import PRESETS, SECTION_TITLES, SECTIONS
from .models import ArendaAccess, AuditLog, Employee, Reminder
from .pagination import ArendaPagination
from .permissions import ArendaSection, get_ctx
from .serializers import AccessInSerializer
from .views import CtxMixin, MeView, _access_payload, _user_brief

INVITE_DOMAIN = "staff.arenda.nurcrm.kg"


def normalize_phone(raw: str) -> str:
    """+996XXXXXXXXX (Кыргызстан). Пустая строка, если номер не разобрать."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10 and digits.startswith("0"):
        digits = "996" + digits[1:]
    elif len(digits) == 9:
        digits = "996" + digits
    return "+" + digits if len(digits) == 12 and digits.startswith("996") else ""


def _app_link():
    return getattr(dj_settings, "ARENDA_APP_URL", "https://app.nurcrm.kg/arenda")


def send_invite_sms(phone, text):
    """Заглушка SMS-шлюза. Текст с паролем живёт только в памяти; возвращает True, если реально отправлено."""
    return False


class AccessGrantMixin(CtxMixin):
    """Выдача доступа — только владельцу или тому, у кого открыты «Настройки» (иначе можно выдать себе всё)."""

    permission_classes = [IsAuthenticated, ArendaSection("settings")]

    def _compute(self, d, acc):
        preset = d.get("preset", "")
        if preset and preset not in PRESETS:
            raise ValidationError({"preset": ["Неизвестный набор."]})
        if "sections" in d:
            sections = d["sections"]
        elif preset:
            sections = PRESETS[preset]["sections"]
        else:
            sections = list(acc.sections) if acc else []
        return preset, [s for s in SECTIONS if s in set(sections)]

    def _branches(self, d):
        ids = d.get("branches")
        if ids is not None:
            found = set(Branch.objects.filter(company=self.ctx.company, id__in=ids).values_list("id", flat=True))
            if found != set(ids):
                raise ValidationError({"branches": ["Филиал не найден."]})
        return ids

    def _save_access(self, request, user, d, employee=None):
        company = self.ctx.company
        acc = ArendaAccess.objects.filter(user=user).first()
        before = _access_payload(user, acc)
        preset, sections = self._compute(d, acc)
        branch_ids = self._branches(d)
        if acc is None:
            acc = ArendaAccess(company=company, user=user, created_by=request.user)
        acc.preset = preset or (acc.preset if "sections" not in d else "")
        acc.sections = sections
        if "all_branches" in d:
            acc.all_branches = d["all_branches"]
        if employee is not None:
            acc.employee = employee
        acc.is_active = bool(sections) and d.get("is_active", True)
        acc.save()
        if branch_ids is not None:
            acc.branches.set(branch_ids)
        security.sync_user_state(user, acc)
        return acc, before

    def _invite(self, request, user, employee_name, login):
        """Новый временный пароль + заглушка SMS. Пароль возвращается один раз и нигде не хранится открытым."""
        pw = security.generate_temp_password()
        user.set_password(pw)
        user.save(update_fields=["password"])
        sec = security.get_sec(user)
        sec.must_change_password = True
        sec.save(update_fields=["must_change_password", "updated_at"])
        security.revoke_sessions(user)
        link = _app_link()
        real_text = f"Кезек: вам открыт доступ. Ссылка: {link} Логин: {login} Временный пароль: {pw}"
        sent = send_invite_sms(login, real_text)
        # в БД — текст БЕЗ пароля
        Reminder.objects.create(
            company=self.ctx.company, tenant=None, channel="sms", is_auto=False, rule_kind="staff_invite",
            text=f"Кезек: вам открыт доступ. Ссылка: {link} Логин: {login} Временный пароль: выдан владельцем.",
            status="sent" if sent else "not_sent", created_by=request.user,
            sent_at=timezone.now() if sent else None,
        )
        return pw, {"channel": "sms", "status": "sent" if sent else "not_sent", "link": link}


class EmployeeAccessView(AccessGrantMixin, APIView):
    """GET/POST/PUT employees/<id>/access/ — доступ сотрудника из карточки «Сотрудники»."""

    def _employee(self, pk):
        e = Employee.objects.filter(pk=pk, company=self.ctx.company, archived_at__isnull=True).first()
        if not e:
            raise ApiError("not_found", "Сотрудник не найден.", status_code=404)
        return e

    def _payload(self, e, acc=None):
        if e.user_id:
            acc = acc or ArendaAccess.objects.filter(user_id=e.user_id).first()
            data = _access_payload(e.user, acc)
            data["login"] = e.user.phone_number or e.user.email
        else:
            data = {"user": None, "preset": "", "sections": [], "all_branches": True, "branches": [],
                    "is_active": False, "login": normalize_phone(e.phone)}
        data["employee"] = {"id": str(e.id), "full_name": e.full_name, "position": e.position, "phone": e.phone}
        return data

    def get(self, request, pk):
        return Response(self._payload(self._employee(pk)))

    def post(self, request, pk):
        e = self._employee(pk)
        ser = AccessInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        reset = bool(request.data.get("reset_password"))
        temp_password = invite = None
        with transaction.atomic():
            user = e.user
            if user is None:
                preset, sections = self._compute(d, None)
                if not sections:  # «Без доступа» у сотрудника без аккаунта — аккаунт не создаём
                    return Response(self._payload(e))
                login = normalize_phone(request.data.get("phone") or e.phone)
                if not login:
                    raise ValidationError({"phone": ["Укажите телефон сотрудника: он будет логином (+996…)."]})
                email = f"{login[1:]}@{INVITE_DOMAIN}"
                if User.objects.filter(email__iexact=email).exists() or User.objects.filter(
                        phone_number=login, company=self.ctx.company).exists():
                    raise ApiError("phone_taken", "Пользователь с таким телефоном уже есть.", status_code=409)
                parts = (e.full_name or "").split()
                user = User.objects.create_user(
                    email=email, password=None, first_name=(parts[0] if parts else "Сотрудник")[:64],
                    last_name=" ".join(parts[1:])[:64], phone_number=login, company=self.ctx.company,
                    role=Roles.SALESPERSON,
                )
                user.set_unusable_password()
                user.save(update_fields=["password"])
                sec = security.get_sec(user)
                sec.created_by_arenda = True
                sec.save(update_fields=["created_by_arenda", "updated_at"])
                e.user = user
                e.save(update_fields=["user", "updated_at"])
                reset = True
            acc, before = self._save_access(request, user, d, employee=e)
            if reset and acc.is_active:
                temp_password, invite = self._invite(request, user, e.full_name, user.phone_number or user.email)
            after = _access_payload(user, acc)
            audit.log(request, self.ctx.company, "access", "access", acc, before=before, after=after,
                      object_name=e.full_name)
        out = self._payload(e, acc)
        if temp_password:
            out["temp_password"] = temp_password  # показывается один раз
            out["invite"] = invite
        return Response(out)

    put = post


class AccessInviteView(AccessGrantMixin, APIView):
    """POST access/<user_id>/invite/ — выдать новый временный пароль и заглушку SMS (пароль виден один раз)."""

    def post(self, request, user_id):
        company = self.ctx.company
        u = User.objects.filter(pk=user_id, company=company).exclude(id=company.owner_id).first()
        acc = ArendaAccess.objects.filter(user=u).first() if u else None
        if not u or not acc:
            raise ApiError("not_found", "Сотрудник не найден.", status_code=404)
        if not acc.is_active:
            raise ApiError("access_closed", "Сначала откройте доступ.", status_code=409)
        pw, invite = self._invite(request, u, "", u.phone_number or u.email)
        audit.log(request, company, "invite", "access", acc, object_name=_user_brief(u)["name"])
        return Response({"login": u.phone_number or u.email, "temp_password": pw, "invite": invite})


# ------------------------------------------------------------------ me, «глазами сотрудника»


class ArendaMeView(MeView):
    """GET me/ ; GET me/?as_user=<id> — владелец смотрит меню и филиалы сотрудника (только просмотр)."""

    def get(self, request):
        ctx = self.ctx
        sec = security.get_sec(request.user, create=False)
        extra = {
            "password_changed_at": sec.password_changed_at if sec else None,
            "must_change_password": bool(sec and sec.must_change_password),
        }
        as_user = request.query_params.get("as_user")
        if not as_user:
            resp = super().get(request)
            resp.data.update(extra)
            resp.data["view_as"] = False
            return resp
        if not ctx.is_owner:
            raise ApiError("permission_denied", "Только владелец может смотреть глазами сотрудника.", status_code=403)
        try:
            target = User.objects.filter(pk=as_user, company=ctx.company).exclude(id=ctx.company.owner_id).first()
        except (ValueError, Exception):
            target = None
        if not target:
            raise ApiError("not_found", "Сотрудник не найден.", status_code=404)
        acc = ArendaAccess.objects.filter(user=target, company=ctx.company).first()
        sections = [s for s in (acc.sections if acc and acc.is_active else []) if s in SECTIONS]
        all_branches = bool(acc and acc.all_branches)
        bq = Branch.objects.filter(company=ctx.company, is_active=True)
        if acc and not all_branches:
            bq = bq.filter(id__in=acc.branches.values_list("id", flat=True))
        elif not acc:
            bq = bq.none()
        return Response({
            "is_owner": False, "sections": sections, "all_branches": all_branches,
            "branches": [{"id": str(b.id), "name": b.name} for b in bq.order_by("name")],
            "user": _user_brief(target),
            "section_titles": {s: SECTION_TITLES[s] for s in sections},
            "view_as": True, "viewed_by": _user_brief(request.user), "is_active": bool(acc and acc.is_active),
            "password_changed_at": None, "must_change_password": False,
        })


# ------------------------------------------------------------------ пароль, вход


class PasswordInSerializer(serializers.Serializer):
    current = serializers.CharField(write_only=True)
    new = serializers.CharField(write_only=True)
    repeat = serializers.CharField(write_only=True, required=False)


class PasswordView(CtxMixin, APIView):
    """POST me/password/ {current, new, repeat?}. Другие сеансы завершаются, в ответе — новая пара токенов."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        _ = self.ctx  # проверка сеанса
        ser = PasswordInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        user = request.user
        left = security.locked_seconds(user)
        if left:
            raise ApiError("account_locked", "Слишком много попыток. Подождите 15 минут.", status_code=429,
                           top={"retry_after": left})
        if not user.check_password(d["current"]):
            security.register_failure(user.email)
            raise ValidationError({"current": ["Текущий пароль неверный."]})
        errs = security.validate_new_password(d["new"])
        if d["new"] == d["current"]:
            errs.append("Новый пароль должен отличаться от текущего.")
        if "repeat" in d and d["repeat"] != d["new"]:
            raise ValidationError({"repeat": ["Пароли не совпадают."]})
        if errs:
            raise ValidationError({"new": errs})
        security.register_success(user.email)
        with transaction.atomic():
            user.set_password(d["new"])
            user.save(update_fields=["password"])
            sec = security.revoke_sessions(user)
            sec.password_changed_at = timezone.now()
            sec.must_change_password = False
            sec.save(update_fields=["password_changed_at", "must_change_password", "updated_at"])
            audit.log(request, self.ctx.company, "password", "user", user, object_name=_user_brief(user)["name"])
        return Response({"detail": "Пароль изменён. Другие сеансы завершены.",
                         "password_changed_at": sec.password_changed_at, **security.issue_tokens(user)})


class LoginView(APIView):
    """POST auth/login/ {login, password} — вход по телефону или email (с блокировкой 5 попыток / 15 минут)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        from apps.users.serializers import CustomTokenObtainPairSerializer

        login = str(request.data.get("login") or "").strip()
        password = request.data.get("password") or ""
        email = login.lower()
        if login and "@" not in login:
            phone = normalize_phone(login)
            u = User.objects.filter(phone_number=phone, arenda_access__isnull=False).first() if phone else None
            if u:
                email = u.email.lower()
        ser = CustomTokenObtainPairSerializer(data={"email": email, "password": password})
        ser.is_valid(raise_exception=True)
        return Response(ser.validated_data)


# ------------------------------------------------------------------ журнал действий


class AuditListView(CtxMixin, APIView):
    """GET audit/?entity=&user=&date_from=&date_to= — только владелец."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = self.ctx
        if not ctx.is_owner:
            raise ApiError("permission_denied", "Журнал действий виден только владельцу.", status_code=403)
        q = request.query_params
        qs = AuditLog.objects.filter(company=ctx.company).select_related("user")
        entity = q.get("entity") or q.get("object_type")
        if entity:
            qs = qs.filter(object_type=entity)
        if q.get("object_id"):
            qs = qs.filter(object_id=q["object_id"])
        if q.get("user"):
            try:
                qs = qs.filter(user_id=q["user"])
            except (ValueError, Exception):
                raise ValidationError({"user": ["Некорректный идентификатор."]})
        for key, op in (("date_from", "gte"), ("date_to", "lte")):
            if q.get(key):
                try:
                    day = datetime.strptime(q[key], "%Y-%m-%d").date()
                except ValueError:
                    raise ValidationError({key: ["Формат даты YYYY-MM-DD."]})
                edge = datetime.combine(day, time.min if op == "gte" else time.max)
                qs = qs.filter(**{f"created_at__{op}": timezone.make_aware(edge)})
        pager = ArendaPagination()
        page = pager.paginate_queryset(qs, request, view=self)
        rows = [{
            "id": str(a.id), "user": str(a.user_id) if a.user_id else None,
            "user_name": _user_brief(a.user)["name"] if a.user else "",
            "action": a.action, "object_type": a.object_type, "object_id": a.object_id,
            "object_name": a.object_name, "before": a.before, "after": a.after, "ip": a.ip,
            "created_at": a.created_at,
        } for a in page]
        return pager.get_paginated_response(rows)
