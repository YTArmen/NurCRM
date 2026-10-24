"""
Эндпоинты для кассы и владельца (JWT сотрудника, компания из токена):

    GET   /api/main/clients/by-phone/?phone=      — клиент компании по телефону (любой формат)
    POST  /api/main/clients/resolve-qr/           — QR приложения NURCRMT<token> → клиент компании
    POST  /api/main/clients/bonus/import/         — BE2-35: перенос бонусов с кассы (один раз на клиента)
    GET/PATCH /api/main/app-shop-settings/        — «показывать в приложении», адрес, часы, бонусы
    GET/PATCH /api/main/referral-rules/           — правила приглашений
"""
import logging
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.permissions import HasApiKeyScope
from apps.main.ai_agent_api import client_debt_summary
from apps.main.kassa_views import change_bonus
from apps.main.models import Client, ClientBonusTransaction
from apps.main.phone_utils import normalize_phone_e164
from apps.main.pos_serializers import _is_owner_like
from apps.main.views import CompanyBranchRestrictedMixin
from apps.users.models import Branch

from . import services
from .geocode import effective_address
from .models import AppCustomer, AppQrToken, AppShopSettings, ReferralRule, hash_secret

logger = logging.getLogger("clientapp.staff")
ZERO = Decimal("0.00")
IMPORT_MAX_ITEMS = 5000


def client_payload(client, created=False):
    phone = client.phone_normalized or normalize_phone_e164(client.phone)
    return {
        "id": str(client.id),
        "full_name": client.full_name,
        "phone": client.phone,
        "phone_normalized": phone,
        "branch": str(client.branch_id) if client.branch_id else None,
        "bonus_balance": str(client.bonus_balance or ZERO),
        "telegram_chat_id": client.telegram_chat_id,
        "has_app": bool(phone) and AppCustomer.objects.filter(phone=phone, deleted_at__isnull=True).exists(),
        "created": created,
    }


class _StaffBase(CompanyBranchRestrictedMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]

    def company_or_400(self):
        company = self._company()
        if company is None:
            raise ValidationError({"detail": "Пользователь не привязан к компании."})
        return company


class ClientByPhoneAPIView(_StaffBase):
    """
    GET /api/main/clients/by-phone/?phone=0555123456 — точное совпадение нормализованного номера.
    ТЗ Н-24 (3): + debt_total, since, next_due_date (то же число, что в clients/debtors/).
    Ключ API — с областью client:read_self.
    """

    permission_classes = [permissions.IsAuthenticated, HasApiKeyScope]
    required_scope = "client:read_self"

    def get(self, request):
        company = self.company_or_400()
        phone = normalize_phone_e164(request.query_params.get("phone"))
        if not phone:
            raise ValidationError({"phone": ["Неверный номер телефона."]})
        branch = self._auto_branch()
        qs = services.clients_for_phone_qs(phone, company=company).select_related("branch")
        if branch is not None:
            from django.db.models import Q

            qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
        from apps.main.views import _filter_clients_visible_for_user

        qs = _filter_clients_visible_for_user(qs, request.user)
        clients = [c for c in qs.order_by("created_at")[:20] if normalize_phone_e164(c.phone) == phone]
        if branch is not None:
            clients.sort(key=lambda c: 0 if c.branch_id == branch.id else 1)
        created = False
        if not clients:
            # ТЗ 14.10, п. 2.3: клиента в компании нет, но номер подтверждён в приложении —
            # заводим клиента из профиля (ФИО, телефон, дата рождения), чтобы старые кассы
            # не открывали «Новый клиент» с пустым именем. Без профиля — как раньше, client: null.
            client, created = services.client_for_kassa_or_from_app(company, branch, phone)
            if client is not None:
                clients = [client]
        return Response({
            "phone": phone,
            "client": client_payload(clients[0], created) if clients else None,
            "results": [client_payload(c, created and c is clients[0]) for c in clients],
            "has_app": services.confirmed_customer_for_phone(phone) is not None,
            **client_debt_summary(company, clients[0] if clients else None),
        })


class ClientResolveQrAPIView(_StaffBase):
    """
    POST /api/main/clients/resolve-qr/ {"token": "NURCRMT…"} (или телефонный "NURCRM996…").
    Защищённый токен → клиент этой компании; если нет — создаётся из профиля приложения
    (клиент физически у кассы этого магазина).
    """

    def post(self, request):
        company = self.company_or_400()
        branch = self._auto_branch()
        raw = str((request.data or {}).get("token") or (request.data or {}).get("qr") or "").strip()
        if raw.upper().startswith("NURCRMT"):
            raw = raw[7:]
        elif raw.upper().startswith("NURCRM"):
            # переходный формат: телефон в QR. Клиента нет — заводим из подтверждённого профиля
            # приложения (ТЗ 14.10, п. 2.3); без профиля — только поиск, client: null.
            phone = normalize_phone_e164(raw[6:])
            if not phone:
                raise ValidationError({"token": ["Неверный QR."]})
            client, created = services.client_for_kassa_or_from_app(company, branch, phone)
            return Response(
                {
                    "format": "phone",
                    "phone": phone,
                    "client": client_payload(client, created) if client else None,
                    "verified": False,
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        if not raw or len(raw) > 64:
            raise ValidationError({"token": ["Неверный QR."]})
        now = timezone.now()
        qr = (
            AppQrToken.objects.select_related("customer")
            .filter(token_hash=hash_secret(raw), expires_at__gt=now, customer__deleted_at__isnull=True)
            .first()
        )
        if qr is None or not qr.customer.phone:
            return Response(
                {"detail": "QR устарел или недействителен. Попросите клиента обновить QR.", "code": "qr_invalid"},
                status=status.HTTP_404_NOT_FOUND,
            )
        customer = qr.customer
        created = False
        with transaction.atomic():
            client = services.pick_client_for_kassa(company, branch, customer.phone)
            if client is None:
                client = services.create_client_from_customer(company, branch, customer, user=request.user)
                created = True
        AppQrToken.objects.filter(pk=qr.pk).update(last_resolved_at=now, resolve_count=qr.resolve_count + 1)
        return Response(
            {"format": "token", "phone": customer.phone, "client": client_payload(client, created), "verified": True},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ClientBonusImportAPIView(_StaffBase):
    """
    POST /api/main/clients/bonus/import/
    {"items": [{"client_id": "…" | "phone": "0555…", "balance": "300.50", "full_name": "…", "note": "…"}]}

    Для каждого клиента — один раз: если по нему ещё нет серверных движений, баланс выставляется
    корректирующей операцией MANUAL (ключ bonus-import:<client_id>). Повторная загрузка → already_imported;
    если на сервере уже есть движения → conflict (сервер главнее, баланс не трогаем).
    """

    def post(self, request):
        company = self.company_or_400()
        branch = self._auto_branch()
        items = (request.data or {}).get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError({"items": ["Передайте непустой список items."]})
        if len(items) > IMPORT_MAX_ITEMS:
            raise ValidationError({"items": [f"Не больше {IMPORT_MAX_ITEMS} строк за раз."]})
        results = []
        summary = {"imported": 0, "already_imported": 0, "conflict": 0, "not_found": 0, "invalid": 0, "zero": 0}
        for idx, item in enumerate(items):
            res = self._import_one(company, branch, request.user, idx, item if isinstance(item, dict) else {})
            summary[res["status"]] = summary.get(res["status"], 0) + 1
            results.append(res)
        return Response({"summary": summary, "results": results})

    def _import_one(self, company, branch, user, idx, item):
        res = {"index": idx, "client_id": None, "phone": None, "status": "invalid", "server_balance": None}
        try:
            balance = Decimal(str(item.get("balance"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            res["error"] = "balance"
            return res
        if balance < 0:
            res["error"] = "balance"
            return res
        client = None
        if item.get("client_id"):
            client = Client.objects.filter(company=company, pk=item["client_id"]).first() if _is_uuid(
                item["client_id"]
            ) else None
            if client is None:
                res["status"] = "not_found"
                return res
        else:
            phone = normalize_phone_e164(item.get("phone"))
            if not phone:
                res["error"] = "phone"
                return res
            res["phone"] = phone
            client = services.pick_client_for_kassa(company, branch, phone)
            if client is None:
                client = Client.objects.create(
                    company=company,
                    branch=branch,
                    full_name=(str(item.get("full_name") or "").strip() or "Клиент")[:255],
                    phone=phone,
                    sector=Client.Sector.MARKET,
                    type=Client.StatusClient.CLIENT,
                )
        res["client_id"] = str(client.id)
        res["phone"] = client.phone_normalized or res["phone"]
        key = f"bonus-import:{client.id}"
        with transaction.atomic():
            locked = Client.objects.select_for_update().get(pk=client.pk)
            prior = ClientBonusTransaction.objects.filter(company=company, idempotency_key=key).first()
            if prior is not None:
                res.update(status="already_imported", server_balance=str(locked.bonus_balance))
                return res
            if ClientBonusTransaction.objects.filter(client=locked).exists():
                res.update(status="conflict", server_balance=str(locked.bonus_balance))
                return res
            delta = balance - (locked.bonus_balance or ZERO)
            if delta == 0:
                res.update(status="zero", server_balance=str(locked.bonus_balance))
                return res
            try:
                with transaction.atomic():
                    tx = change_bonus(
                        client=locked,
                        delta=delta,
                        reason=ClientBonusTransaction.Reason.MANUAL,
                        user=user,
                        note=(str(item.get("note") or "") or "Перенос бонусов с кассы")[:255],
                        idempotency_key=key,
                    )
            except IntegrityError:
                res.update(status="already_imported", server_balance=str(Client.objects.get(pk=client.pk).bonus_balance))
                return res
        res.update(status="imported", server_balance=str(tx.balance_after))
        return res


def _is_uuid(v):
    import uuid

    try:
        uuid.UUID(str(v))
        return True
    except (TypeError, ValueError):
        return False


# ======================================================================
# Владелец: магазин в приложении и правила приглашений
# ======================================================================


def _owner_only(request):
    """Только владелец или администратор компании (не РОП и не кассир) — ТЗ экрана магазина, п. 3."""
    u = request.user
    ok = (
        getattr(u, "is_superuser", False)
        or bool(getattr(u, "owned_company", None))
        or getattr(u, "role", None) in ("owner", "admin")
    )
    if not ok:
        raise PermissionDenied("Доступно владельцу или администратору.")


def _dec(v, field, lo=None, hi=None, allow_null=True):
    if v in (None, ""):
        if allow_null:
            return None
        raise ValidationError({field: ["Обязательное поле."]})
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError({field: ["Неверное число."]})
    if (lo is not None and d < lo) or (hi is not None and d > hi):
        raise ValidationError({field: [f"Допустимо от {lo} до {hi}."]})
    return d


def _row_payload(row: AppShopSettings, branch=None):
    return {
        "branch_id": str(row.branch_id) if row.branch_id else None,
        "branch_name": branch.name if branch is not None else None,
        "show_in_app": row.show_in_app,
        "display_name": row.display_name,
        "address": row.address,
        "effective_address": effective_address(row) if row.pk else (row.address or ""),
        "phone": row.phone,
        "hours": row.hours,
        "latitude": str(row.latitude) if row.latitude is not None else None,
        "longitude": str(row.longitude) if row.longitude is not None else None,
        "points_enabled": row.points_enabled,
        "points_percent": str(row.points_percent) if row.points_percent is not None else None,
        "geocode_status": row.geocode_status,
        "geocoded_at": row.geocoded_at.isoformat() if row.geocoded_at else None,
        "hidden_by_admin": row.hidden_by_admin,
        "hidden_reason": row.hidden_reason if row.hidden_by_admin else "",
    }


TEXT_FIELDS = ("display_name", "address", "phone", "hours")


def _apply_row(row: AppShopSettings, data: dict, is_branch: bool):
    changed_address = False
    if "show_in_app" in data:
        row.show_in_app = bool(data["show_in_app"])
    for f in TEXT_FIELDS:
        if f in data:
            val = str(data.get(f) or "").strip()[:255]
            if f == "address" and val != row.address:
                changed_address = True
            setattr(row, f, val)
    if "points_enabled" in data:
        v = data["points_enabled"]
        row.points_enabled = None if (v is None and is_branch) else bool(v)
    if "points_percent" in data:
        row.points_percent = _dec(data["points_percent"], "points_percent", Decimal("0"), Decimal("100"))
    coords_given = "latitude" in data or "longitude" in data
    if coords_given:
        lat = _dec(data.get("latitude"), "latitude", Decimal("-90"), Decimal("90"))
        lon = _dec(data.get("longitude"), "longitude", Decimal("-180"), Decimal("180"))
        if (lat is None) != (lon is None):
            raise ValidationError({"latitude": ["Укажите обе координаты или ни одной."]})
        row.latitude = lat.quantize(Decimal("0.000001")) if lat is not None else None
        row.longitude = lon.quantize(Decimal("0.000001")) if lon is not None else None
        row.geocode_status = AppShopSettings.GeocodeStatus.MANUAL if lat is not None else ""
        row.geocoded_at = timezone.now() if lat is not None else None
    elif changed_address:
        # адрес сменился — старые (в т.ч. ручные) координаты больше не верны, ищем заново
        row.latitude = row.longitude = None
        row.geocode_status = ""
        row.geocode_attempts = 0
    return changed_address or coords_given


class AppShopSettingsAPIView(_StaffBase):
    """
    GET   /api/main/app-shop-settings/
    PATCH /api/main/app-shop-settings/ {"show_in_app": true, "address": "…", "points_percent": 5,
           "branches": [{"branch_id": "…", "show_in_app": false, "address": "…"}]}
    """

    def _rows(self, company):
        row, _ = AppShopSettings.objects.get_or_create(company=company, branch=None)
        branches = list(Branch.objects.filter(company=company, is_active=True).order_by("name"))
        brows = {r.branch_id: r for r in AppShopSettings.objects.filter(company=company, branch__isnull=False)}
        return row, branches, brows

    def _payload(self, company):
        row, branches, brows = self._rows(company)
        branch_items = []
        for b in branches:
            br = brows.get(b.id) or AppShopSettings(company=company, branch=b, show_in_app=True)
            item = _row_payload(br, b)
            if not br.pk:
                item["effective_address"] = b.address or ""
            branch_items.append(item)
        data = _row_payload(row)
        data["company_id"] = str(company.id)
        data["catalog_slug"] = company.slug if services._company_has_showcase(company) else None
        data["branches"] = branch_items
        data["app_preview"] = [s for s in services.build_shops() if s["companyId"] == str(company.id)]
        access = services.company_app_access(company)
        data["access"] = access
        # Почему магазина нет на карте — чтобы касса подсказала владельцу, что исправить
        reasons = []
        if not access["allowed"]:
            reasons.append("payment_required")
        if not row.show_in_app:
            reasons.append("disabled")
        if row.hidden_by_admin:
            reasons.append("hidden_by_admin")
        if row.show_in_app and not data["app_preview"]:
            if not (effective_address(row) or any(b.get("effective_address") for b in branch_items)):
                reasons.append("no_address")
            else:
                reasons.append("no_coordinates")
        data["visible_in_app"] = bool(data["app_preview"])
        data["not_visible_reasons"] = reasons
        on_map = {s["branchId"] for s in data["app_preview"] if s["branchId"]}
        for item in branch_items:
            b_reasons = []
            if not access["allowed"]:
                b_reasons.append("payment_required")
            if not row.show_in_app or not item["show_in_app"]:
                b_reasons.append("disabled")
            if row.hidden_by_admin or item["hidden_by_admin"]:
                b_reasons.append("hidden_by_admin")
            if not item["effective_address"]:
                b_reasons.append("no_address")
            elif item["latitude"] is None or item["longitude"] is None:
                b_reasons.append("no_coordinates")
            item["visible_in_app"] = item["branch_id"] in on_map
            item["not_visible_reasons"] = [] if item["visible_in_app"] else b_reasons
        return data

    def get(self, request):
        company = self.company_or_400()
        _owner_only(request)
        return Response(self._payload(company))

    def patch(self, request):
        company = self.company_or_400()
        _owner_only(request)
        data = request.data if isinstance(request.data, dict) else {}
        if data.get("show_in_app") and not services.company_app_access(company)["allowed"]:
            return Response(
                {"detail": "Бесплатный период приложения закончился — подключите функцию «Приложение клиентов».",
                 "code": "client_app_payment_required"},
                status=status.HTTP_403_FORBIDDEN,
            )
        to_geocode = []
        with transaction.atomic():
            row, branches, brows = self._rows(company)
            if _apply_row(row, data, is_branch=False):
                to_geocode.append(row)
            row.save()
            branch_by_id = {str(b.id): b for b in branches}
            branch_errors = {}
            for bdata in data.get("branches") or []:
                if not isinstance(bdata, dict):
                    continue
                bid = str(bdata.get("branch_id"))
                b = branch_by_id.get(bid)
                if b is None:
                    branch_errors[bid] = {"branch_id": ["Филиал не найден."]}
                    continue
                brow = brows.get(b.id) or AppShopSettings(company=company, branch=b, show_in_app=True)
                try:
                    if _apply_row(brow, bdata, is_branch=True):
                        to_geocode.append(brow)
                except ValidationError as exc:
                    # ошибки филиала — под его id: {"branches": {"<id>": {"latitude": ["…"]}}}
                    branch_errors[bid] = exc.detail
                    continue
                brow.save()
                brows[b.id] = brow
            if branch_errors:
                raise ValidationError({"branches": branch_errors})
            # филиалы без строки настроек тоже геокодируем по их адресу
            if row.show_in_app:
                for b in branches:
                    if b.id not in brows and (b.address or "").strip():
                        brow = AppShopSettings.objects.create(company=company, branch=b, show_in_app=True)
                        brows[b.id] = brow
                        to_geocode.append(brow)
            if row.show_in_app and not row.latitude and effective_address(row) and row not in to_geocode:
                to_geocode.append(row)
        services.invalidate_shops_cache()
        from .tasks import enqueue, geocode_shop

        for r in to_geocode:
            if r.latitude is None and r.geocode_status != AppShopSettings.GeocodeStatus.MANUAL:
                transaction.on_commit(lambda pk=r.pk: enqueue(geocode_shop, pk))
        return Response(self._payload(company))


class ReferralRuleAPIView(_StaffBase):
    """GET/PATCH /api/main/referral-rules/ {"enabled": true, "inviter_points": 100, "invitee_points": 50}"""

    def _payload(self, rule):
        return {
            "enabled": rule.enabled,
            "inviter_points": str(rule.inviter_points),
            "invitee_points": str(rule.invitee_points),
            "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
        }

    def get(self, request):
        company = self.company_or_400()
        _owner_only(request)
        rule, _ = ReferralRule.objects.get_or_create(company=company)
        return Response(self._payload(rule))

    def patch(self, request):
        company = self.company_or_400()
        _owner_only(request)
        data = request.data if isinstance(request.data, dict) else {}
        rule, _ = ReferralRule.objects.get_or_create(company=company)
        if "enabled" in data:
            rule.enabled = bool(data["enabled"])
        for f in ("inviter_points", "invitee_points"):
            if f in data:
                setattr(rule, f, _dec(data[f], f, Decimal("0"), Decimal("1000000"), allow_null=False))
        rule.save()
        return Response(self._payload(rule))


# ======================================================================
# Администратор платформы NurCRM: отчёт и скрытие магазинов с карты
# ======================================================================


class PlatformClientAppWeeklyReportAPIView(APIView):
    """GET /api/platform-admin/client-app/weekly-report/?weeks=12"""

    def get_permissions(self):
        from apps.users.permissions import IsPlatformAdmin

        return [IsPlatformAdmin()]

    def get(self, request):
        from .report import weekly_report

        try:
            weeks = max(1, min(int(request.query_params.get("weeks") or 12), 104))
        except ValueError:
            raise ValidationError({"weeks": ["Число недель от 1 до 104."]})
        return Response(weekly_report(weeks))


class PlatformClientAppShopHideAPIView(APIView):
    """
    PATCH /api/platform-admin/client-app/shops/<company_id>/ {"hidden": true, "reason": "…", "branch_id": null}
    Скрыть с карты магазин (строка компании — весь магазин) или отдельный филиал.
    """

    def get_permissions(self):
        from apps.users.permissions import IsPlatformAdmin

        return [IsPlatformAdmin()]

    def patch(self, request, company_id):
        from apps.users.models import Company

        company = Company.objects.filter(id=company_id).first() if _is_uuid(company_id) else None
        if company is None:
            raise ValidationError({"company_id": ["Компания не найдена."]})
        data = request.data if isinstance(request.data, dict) else {}
        branch = None
        if data.get("branch_id"):
            branch = Branch.objects.filter(id=data["branch_id"], company=company).first() if _is_uuid(data["branch_id"]) else None
            if branch is None:
                raise ValidationError({"branch_id": ["Филиал не найден."]})
        row, _ = AppShopSettings.objects.get_or_create(company=company, branch=branch)
        row.hidden_by_admin = bool(data.get("hidden", True))
        row.hidden_reason = str(data.get("reason") or "")[:255] if row.hidden_by_admin else ""
        row.save(update_fields=["hidden_by_admin", "hidden_reason", "updated_at"])
        services.invalidate_shops_cache()
        return Response({
            "company_id": str(company.id),
            "branch_id": str(branch.id) if branch else None,
            "hidden_by_admin": row.hidden_by_admin,
            "hidden_reason": row.hidden_reason,
        })


class AppShopGeocodeAPIView(_StaffBase):
    """
    POST /api/main/app-shop-settings/geocode/ {"address": "Бишкек, Чуй 100"}
    Координаты по адресу сразу (до сохранения), чтобы показать точку на карте.
    200 → {"found": true, "latitude": "42.874600", "longitude": "74.569800"} | {"found": false}
    503 {"code": "geocoder_busy"} — геокодер занят, повторите через пару секунд; 502 — геокодер недоступен.
    """

    def post(self, request):
        self.company_or_400()
        _owner_only(request)
        address = str((request.data or {}).get("address") or "").strip()
        if not address:
            raise ValidationError({"address": ["Укажите адрес."]})
        if len(address) > 255:
            raise ValidationError({"address": ["Не длиннее 255 символов."]})
        from .geocode import geocode_query

        try:
            res = geocode_query(address)
        except RuntimeError:
            return Response({"detail": "Поиск адреса занят, повторите через пару секунд.", "code": "geocoder_busy"},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception:
            return Response({"detail": "Поиск адреса сейчас недоступен.", "code": "geocoder_error"},
                            status=status.HTTP_502_BAD_GATEWAY)
        if not res:
            return Response({"found": False})
        lat, lon = res
        return Response({"found": True, "latitude": str(lat), "longitude": str(lon)})


class AppShopPointsAPIView(_StaffBase):
    """
    GET /api/main/app-shop-settings/points/?branch=<id>
    Процент начисления баллов магазина/филиала — один источник для кассы и приложения.
    Читает любой сотрудник компании. Без ?branch= — филиал текущего пользователя (или компания).
    → {"points_enabled": true, "points_percent": "5.00", "branch_id": "…" | null, "source": "branch" | "company"}
    """

    def get(self, request):
        company = self.company_or_400()
        raw = request.query_params.get("branch")
        if raw:
            branch = Branch.objects.filter(id=raw, company=company).first() if _is_uuid(raw) else None
            if branch is None:
                raise ValidationError({"branch": ["Филиал не найден."]})
        else:
            branch = self._auto_branch()
        crow = AppShopSettings.objects.filter(company=company, branch__isnull=True).first()
        brow = AppShopSettings.objects.filter(company=company, branch=branch).first() if branch else None
        enabled = bool(crow.points_enabled) if crow else False
        percent = crow.points_percent if crow else None
        source = "company"
        if brow is not None and brow.points_enabled is not None:
            enabled, source = bool(brow.points_enabled), "branch"
        if brow is not None and brow.points_percent is not None:
            percent, source = brow.points_percent, "branch"
        return Response({
            "points_enabled": enabled,
            "points_percent": str(percent) if enabled and percent is not None else None,
            "branch_id": str(branch.id) if branch else None,
            "source": source,
        })
