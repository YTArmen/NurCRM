from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ekassa.exceptions import EkassaAPIError, EkassaConfigurationError
from apps.ekassa.serializers import (
    EkassaIntegrationReadSerializer,
    EkassaIntegrationWriteSerializer,
    default_settings_payload,
)
from apps.ekassa.sale_bridge import _som_to_tyiyun_int
from apps.ekassa.services import client_for, get_integration, inject_fiscal_number
# eko1811: eKassa — только владелец компании (и суперпользователь)
from apps.users.permissions import IsCompanyOwner

logger = logging.getLogger(__name__)


def _resolve_company(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    c = getattr(user, "owned_company", None) or getattr(user, "company", None)
    if c:
        return c
    br = getattr(user, "branch", None)
    if br is not None:
        return getattr(br, "company", None)
    return None


def _ekassa_timeout() -> int:
    return int(getattr(settings, "EKASSA_REQUEST_TIMEOUT", 45) or 45)


def _ekassa_test_receipt_allowed() -> bool:
    return bool(getattr(settings, "EKASSA_ALLOW_TEST_RECEIPT", False))


def _disabled_response():
    return Response(
        {
            "detail": "Интеграция eKassa выключена или не настроена.",
            "code": "ekassa_disabled",
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def _handle_ekassa_error(exc: EkassaAPIError):
    payload = {"detail": str(exc), "code": "ekassa_upstream"}
    if exc.payload is not None:
        payload["ekassa"] = exc.payload
    code = exc.status_code if exc.status_code and exc.status_code < 500 else status.HTTP_502_BAD_GATEWAY
    return Response(payload, status=code)


class EkassaSettingsView(APIView):
    """
    GET — текущие настройки (без пароля).
    PATCH — обновление; пароль передавайте только при смене.
    """

    permission_classes = [IsAuthenticated, IsCompanyOwner]

    def get(self, request):
        company = _resolve_company(request.user)
        if not company:
            return Response({"detail": "Компания для текущего пользователя не найдена."}, status=404)
        obj = get_integration(company)
        if obj is None:
            return Response(default_settings_payload())
        return Response(EkassaIntegrationReadSerializer(obj).data)

    def patch(self, request):
        company = _resolve_company(request.user)
        if not company:
            return Response({"detail": "Компания для текущего пользователя не найдена."}, status=404)
        obj = get_integration(company)
        if obj is None:
            ser = EkassaIntegrationWriteSerializer(data=request.data, partial=True)
            ser.is_valid(raise_exception=True)
            ser.save(company=company)
            obj = get_integration(company)
            return Response(EkassaIntegrationReadSerializer(obj).data)
        ser = EkassaIntegrationWriteSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        obj.refresh_from_db()
        return Response(EkassaIntegrationReadSerializer(obj).data)


class EkassaTestReceiptView(APIView):
    """
    Тестовая «оплата»: один фискальный чек INCOME в eKassa (как при реальной продаже, без Sale в БД).

    POST /api/ekassa/test-receipt/
    Тело (опционально): {"amount_som": "1.00", "cash": true, "name": "Тестовая оплата"}

    Включение: EKASSA_ALLOW_TEST_RECEIPT=1 или DEBUG=true (см. settings).
    Права: владелец/админ компании; интеграция eKassa должна быть настроена (как для receipt/).
    """

    permission_classes = [IsAuthenticated, IsCompanyOwner]

    def post(self, request):
        if not _ekassa_test_receipt_allowed():
            return Response(
                {
                    "detail": "Тестовые чеки eKassa отключены. Задайте EKASSA_ALLOW_TEST_RECEIPT=1 или включите DEBUG.",
                    "code": "ekassa_test_disabled",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        company = _resolve_company(request.user)
        if not company:
            return Response({"detail": "Компания для текущего пользователя не найдена."}, status=404)
        try:
            cli = client_for(company)
        except EkassaConfigurationError:
            return _disabled_response()

        cfg = get_integration(company)
        fiscal = (cfg.fiscal_number or "").strip() if cfg else ""
        if not fiscal:
            return _disabled_response()

        data = request.data if isinstance(request.data, dict) else {}
        cash = data.get("cash", True)
        if isinstance(cash, str):
            cash = cash.strip().lower() in ("1", "true", "yes", "on")

        raw_amount = data.get("amount_som", "1.00")
        try:
            amount = Decimal(str(raw_amount))
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "Некорректное amount_som.", "code": "invalid_amount"}, status=400)
        if amount < Decimal("0.01") or amount > Decimal("500000"):
            return Response(
                {"detail": "amount_som должен быть от 0.01 до 500000.", "code": "invalid_amount"},
                status=400,
            )

        name = (data.get("name") or "Тестовая оплата nurCRM")[:255]
        price_ty = _som_to_tyiyun_int(amount)
        if price_ty < 1:
            return Response({"detail": "Сумма слишком мала.", "code": "invalid_amount"}, status=400)

        newid = str(uuid.uuid4())
        body = {
            "fiscal_number": fiscal,
            "newid": newid,
            "operation": "INCOME",
            "cash": bool(cash),
            "goods": [
                {
                    "calcItemAttributeCode": 0,
                    "name": name,
                    "sgtin": "TESTNURCRM",
                    "price": price_ty,
                    "quantity": 1.0,
                    "unit": "шт.",
                    "st": 0,
                    "vat": 0,
                }
            ],
        }
        if bool(cash):
            body["received"] = str(price_ty)

        try:
            resp = cli.request_json("POST", "/api/v2/receipt", json_body=body)
        except EkassaAPIError as e:
            return _handle_ekassa_error(e)
        out = dict(resp) if isinstance(resp, dict) else {"data": resp}
        out["test_meta"] = {"newid": newid, "amount_som": str(amount.quantize(Decimal("0.01"))), "cash": bool(cash)}
        return Response(out)


class EkassaPingView(APIView):
    """
    Проверка доступности хоста eKassa (/api/ping).
    Не требует включённой интеграции — используется URL из настроек компании или дефолт из settings.
    """

    permission_classes = [IsAuthenticated, IsCompanyOwner]

    def get(self, request):
        company = _resolve_company(request.user)
        if not company:
            return Response({"detail": "Компания для текущего пользователя не найдена."}, status=404)
        cfg = get_integration(company)
        base = ""
        if cfg:
            base = cfg.effective_base_url()
        if not base:
            base = (getattr(settings, "EKASSA_DEFAULT_BASE_URL", "") or "").strip().rstrip("/")
        if not base:
            return Response({"detail": "Не задан базовый URL eKassa."}, status=500)
        url = f"{base}/api/ping"
        try:
            r = requests.get(url, timeout=_ekassa_timeout())
        except requests.RequestException as e:
            logger.warning("eKassa ping failed: %s", e)
            return Response({"detail": "Не удалось связаться с eKassa.", "error": str(e)}, status=502)
        return Response({"url": url, "http_status": r.status_code, "body": r.text[:2000]})


def _proxy_post(request, path: str, body: dict[str, Any] | None = None):
    company = _resolve_company(request.user)
    if not company:
        return Response({"detail": "Компания для текущего пользователя не найдена."}, status=404)
    try:
        cli = client_for(company)
    except EkassaConfigurationError:
        return _disabled_response()
    cfg = get_integration(company)
    fiscal = cfg.fiscal_number.strip() if cfg else ""
    payload = inject_fiscal_number(body, fiscal)
    try:
        data = cli.request_json("POST", path, json_body=payload)
    except EkassaAPIError as e:
        return _handle_ekassa_error(e)
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_pos(request):
    """POST /api/get_pos_by_fiscal_number"""
    return _proxy_post(request, "/api/get_pos_by_fiscal_number", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_catalogue(request):
    """POST /api/get_base_catalogue_by_fiscal_number"""
    return _proxy_post(request, "/api/get_base_catalogue_by_fiscal_number", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_shift_state(request):
    return _proxy_post(request, "/api/shift_state_by_fiscal_number", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_shift_open(request):
    return _proxy_post(request, "/api/shift_open_by_fiscal_number", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_shift_close(request):
    return _proxy_post(request, "/api/shift_close_by_fiscal_number", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_shift_control(request):
    return _proxy_post(request, "/api/shift_control_by_fiscal_number", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_cash_operation(request):
    return _proxy_post(request, "/api/cash_operation_by_fiscal_number", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_receipt(request):
    """POST /api/v2/receipt — новый чек или переотправка (см. документацию)."""
    return _proxy_post(request, "/api/v2/receipt", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_duplicate(request):
    return _proxy_post(request, "/api/duplicate", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_customers(request):
    return _proxy_post(request, "/api/customers", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_xpay_getqr(request):
    return _proxy_post(request, "/api/xpay/getqr", request.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_xpay_status(request):
    return _proxy_post(request, "/api/xpay/status", request.data)


def _proxy_get(request, path: str):
    company = _resolve_company(request.user)
    if not company:
        return Response({"detail": "Компания для текущего пользователя не найдена."}, status=404)
    try:
        cli = client_for(company)
    except EkassaConfigurationError:
        return _disabled_response()
    try:
        data = cli.request_json("GET", path)
    except EkassaAPIError as e:
        return _handle_ekassa_error(e)
    return Response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_info_tax_systems(request):
    return _proxy_get(request, "/api/info/tax-systems")


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_info_tax_rates(request):
    return _proxy_get(request, "/api/info/tax-rates")


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_info_gns_departments(request):
    return _proxy_get(request, "/api/info/gns-departments")


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_info_entrepreneurship_objects(request):
    return _proxy_get(request, "/api/info/entrepreneurship-objects")


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_info_calc_item_attributes(request):
    return _proxy_get(request, "/api/info/calc-item-attributes")


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_info_business_activities(request):
    return _proxy_get(request, "/api/info/business-activities")


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_logout(request):
    """Завершить сессию токена на стороне eKassa (документация: GET /api/logout)."""
    company = _resolve_company(request.user)
    if not company:
        return Response({"detail": "Компания для текущего пользователя не найдена."}, status=404)
    try:
        cli = client_for(company)
    except EkassaConfigurationError:
        return _disabled_response()
    try:
        data = cli.request_json("GET", "/api/logout")
    except EkassaAPIError as e:
        return _handle_ekassa_error(e)
    finally:
        cli.invalidate_token()
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsCompanyOwner])
def ekassa_retry(request):
    """Повторно отправить в eKassa чеки продаж и возвратов, которые не дошли (status error/pending)."""
    company = _resolve_company(request.user)
    if company is None or get_integration(company) is None:
        return _disabled_response()
    from apps.ekassa.sale_bridge import retry_failed_for_company

    try:
        limit = max(1, min(int(request.data.get("limit") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    return Response(retry_failed_for_company(company, limit=limit))
