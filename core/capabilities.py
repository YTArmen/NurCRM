"""
Признак версии API для касс и мобилок (единое ТЗ бэкенду 26.10.2026, Б-01).

GET /api/main/capabilities/ — любой вошедший сотрудник:
    {"api": "2026-10", "server_version": "...", "features": [...], "company_flags": {...},
     "client": "kassa/1.17.61", "client_is_new": true}
GET /api/main/client-versions/?days=7 — поддержка и суперпользователь: компания → версии клиентов.

Список features — только добавлением: касса включает новое поведение по наличию кода.
"""
import os
from datetime import timedelta

from django.db.models import Max, Sum
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.api_compat import API_VERSION, client_label, company_flags, is_new_client

FEATURES = (
    "weight_line_total",  # wlt2611: pos/checkout items[].line_total — весовая строка «по сумме»
    "bot_seller",  # bot2611: telegram-bot/settings seller_config — бот-продавец (тон/часы/языки/знания)
    "bot_handoff",  # bot2611: передача диалога владельцу в Telegram, telegram-bot/dialogs/<chat_id>/ take|return
    "bot_sales_stats",  # bot2611: telegram-bot/sales-stats/ и telegram-bot/dialogs/
    "bot_bargain",  # bot2711: торг — seller_config.bargain, telegram-bot/bargain/products|offers|test, items[].bargain
    "billing_elqr",  # elqr1911: users/billing/* — оплата подписки ELQR (доступность — billing/config/)
    "shifts_month_filter",  # tab1911: construction/shifts/?month= и main/staff/timesheet/?month=
    "shift_report_v2", "cashbox_drawer_cash", "idempotency_lookup", "price_stable",
    "negative_custom_rejected", "client_documents", "bot_owner_server", "salary_payouts",
    "supplier_pay_debt", "access_codes", "changes_feed", "receipt_create_deal", "rentals_paged",
    "strict_permissions", "route_not_found", "client_header", "employee_limits", "support_constructor",
    "events_stream",  # ТЗ 29.10 Б-102: WebSocket ws/main/events/ (SSE-адрес отвечает use_websocket)
    "etag_304", "purchases",
    # ТЗ 31.10 п.16: только то, что уже работает на сервере (receipt_patch, transfer_documents,
    # stock_balance, agents — добавятся вместе с функциями)
    "share_link", "receipt_attachments", "purchase_signature", "server_addons", "product_archive", "bulk_pay_debt",
    "receipt_patch",  # ТЗ 31.10 п.21: PATCH suppliers/receipts/{id}/ (dry_run, If-Match, коды ошибок)
    # ТЗ редактор накладных 30.10 (вариант А): PATCH с Idempotency-Key, версии/откат, деньги, receipt_changed
    "receipt_edit",
    "receipt_edit_discount",  # 01.11: PATCH прихода — скидка шапки (discount / discount_percent)
    "receipt_edit_sale_price",  # 01.11: PATCH прихода — цена продажи строки (sale_price)
    # ТЗ 01.11 (витрина): Telegram-блок в публичном дизайне, ссылка на корзину c_<token>, source_detail у заказов
    "showcase_telegram", "showcase_cart_link", "showcase_order_source_detail",
    # ТЗ 01.11: чат на витрине (web_widget, согласие, удаление чата) и ИИ-помощник витрины с ключом магазина
    "showcase_chat", "showcase_assistant",
    # 03.11 front-market: CashFlow.employee + ?employee=; миниатюры image_thumb_url; pos/sales payment_method=noncash
    # и списком через запятую; suppliers/receipts ?created_by=&amount_min=&amount_max=&product_search=
    "cashflow_employee", "product_thumbs", "sales_noncash_filter", "receipts_filters",
    # 03.11 аудит S1: себе роль/права менять нельзя; роль admin и опасные права склада — только владелец/админ;
    # PATCH employees/{id}/ теперь принимает can_post_negative_stock … can_cash_negative (раньше отбрасывал)
    "employee_rights_guard",
    # S3: pos/sales и pos/returns — кассиру только свои (сервер); pos/returns/?search=
    "sales_own_only", "returns_search",
    "debt_pay_idempotency",  # S9: POST main/debts/{id}/pay/ — Idempotency-Key или idempotency_key
    "mass_incoming_purchase_price",  # G1: mass-incoming сохраняет purchase_price новой карточки всегда
    "mass_incoming_branch",  # G2: mass-incoming {branch} или ?branch= — филиал новых карточек
    "employees_list_salary",  # S2: GET users/employees/ пускает и с can_view_salary
    "cashflow_check_note_only",  # S7: cashflows/{id}/check/ {note} без checked — отметку не трогает
    # S4: construction/shifts/?date_from=&date_to= (по opened_at); pos/cart-item-deletions/ даты, user, search
    "shifts_date_filter", "cart_deletions_filters",
    # S6: CashFlow.salary_period ("YYYY-MM" или {from,to}), фильтр ?salary_period= / ?salary_period_from=&_to=
    "cashflow_salary_period",
    "products_has_promotion",  # G4: products/list/?has_promotion=true|false
    "products_piece_sale",  # G5: products/list/?piece_sale=true|false
    "products_barcodes_filter",  # G6: products/list/?barcodes=a,b,c (основной или доп. код), до 500
    "supplier_products_page_size",  # G7: suppliers/{id}/products/?page_size= (до 500)
    "rentals_branch_filter",  # S5: rentals/?branch=<uuid>|main
    "ui_dark_style",  # S8: users/ui-preferences/ dark_style (soft|classic) = theme_dark_palette
    "bundle_component_id",  # G8: packages_input[].product_id у комплекта; в ответе packages[].product_id/product_name
    # ТЗ кассы 05.11
    "supplier_return_debt_offset_v2",  # п.1: debt_offset уменьшает долг прихода, без переплаты
    "debt_return_drawer_cash",  # п.2: возврат чека в долг — выдача гашений наличными из ящика смены
    "checkout_lines_merged",  # п.4: одинаковые строки складываются (warnings lines_merged), разные — 400 duplicate_line
    "weight_line_total_floor",  # п.5: line_total весовой строки вниз, как subtotal
    "receipt_delete",  # п.6: DELETE suppliers/receipts/{id}/ {reason} + Idempotency-Key
    "receipt_version_required",
    "rental_idempotency",
    "products_archived_filter",
    "checkout_branch",
    "checkout_below_cost_warning",
    # 06.11.2026: вход продлевается при работе (refresh 30 дн, предел 90 дн), коды 401 у auth/refresh/
    "refresh_rotation",
    # 06.11.2026: слоты устройств — 409 device_slot_taken, replace_device, 401 session_revoked, auth/devices/
    "device_slots",
    "idempotency_lookup_bonus_transfers",  # п.17: idempotency/{key}/ находит бонусы и перемещения; branch-transfers replayed  # п.15: досылка ниже закупки — 201 + warnings [{code: below_cost, index, product_id, purchase_price}]  # п.14: pos/checkout/ {branch} — филиал чека, если у смены/кассы его нет  # п.13: products/list/?is_archived=true (или status=archived) — только архив  # п.8: POST rentals/ — Idempotency-Key, replayed  # п.7: PATCH/DELETE прихода с кассы без If-Match/version — 428 version_required
    "supplier_return_edit_24h", "pos_return_edit_24h",  # ТЗ 10.11: правка/аннулирование возвратов 24 ч
    "stock_documents",  # 14.11.2026 П0-4 К-2: stock/documents/ (stock/movements/ — possync, ТЗ-28), stock/writeoff-reasons/
    "negative_stock_policy",  # 14.11.2026 П0-4 К-16: settings/stock/, warnings negative_stock
    "product_input_checks",  # 14.11.2026 П0-4 К-20: barcode_duplicate 409, barcode_checksum, price_zero, price_below_cost
    "receipt_k11",  # 14.11.2026 П0-4 К-11: receipt_duplicate 409, vat_mode, attachment_ids, фильтр по времени
    "audit_log_entity", "test_marks", "purchase_price_permission",  # 14.11.2026 П0-4 К-17, К-18, К-19
    "units_catalog", "stock_expiring", "price_changed_event", "stock_changed_all",  # 15.11.2026 П1-6
    "receipt_serials",  # 15.11.2026 П1-6 К-6: serial_numbers в приходе, stock/serials/
    "costing_method",  # 15.11.2026 П1-6 К-4: settings/stock/ costing_method last|average, cogs в строке чека
    "price_history", "price_revisions", "stock_abcxyz",  # 15.11.2026 П2-3 К-12, К-14
    "gift_cards", "promotions_active",  # 15.11.2026 П3-1: gift-cards/, оплата gift_card в pos/checkout/, promotions/active/
    "stock_ledger",  # 14.11.2026 П0-4 К-1: stock/ledger/, products/<id>/history/, stock/balance/, stock/turnover/
    "return_without_sale",  # 14.11.2026 П1-1: pos/returns/without-sale/, pos/return-reasons/, reports/returns-by-reason/, reason_code
    "opening_debt",  # 14.11.2026 Л-10: clients/<id>/opening-debt/ (начальный долг при переносе), clients/imported/
    "analytics_summary",  # 14.11.2026 П2-2: analytics/summary/ (group_by, compare)
    "staff_push",  # 14.11.2026 П2-2: devices/push-token/, лента attention
    "support_requests",  # 14.11.2026 П2-6: public/support/info/, support/requests/, rating
    "supplier_statement",  # 14.11.2026 П2-4: suppliers/<id>/statement/ (акт сверки)
    "showcase_promo",  # 14.11.2026 П2-5: промокоды витрины, showcase/orders/<id>/delivery/
    "repair", "trade_in",  # 15.11.2026 П2-1: repair-orders/, trade-in/, trade_in_credit (по addon kassa-repair / kassa-trade_in)
    "fiscal_kassa",  # 13.11.2026 П0-1: pos/sales|returns/<id>/fiscal/, settings/fiscal/, fiscal_locked
    # 19.11.2026 (bek1911): признаки, о которых спросила касса — маршруты уже работали
    "stock_balance",  # stock/balance/ (остатки на дату/по филиалам)
    "transfer_documents",  # stock/transfers/<id>/ (карточка) + stock/transfers/<id>/file/ (документы перемещения)
    # серверная eKassa: включена ли у компании — fiscal.mode == "server" в этом же ответе
    "fiscal_server",
    "ekassa_returns",  # фискальный возврат: Sale.ekassa_fiscal.returns[<id возврата>]
    "ekassa_retry",  # POST ekassa/retry/ — повторная отправка непробитого чека/возврата
    "ekassa_status",  # 20.11.2026 fis2011: GET ekassa/status/ — ЗН/РН/ФМ/срок ФМ/смена/последний чек/X-отчёт
    "ekassa_receipt_text",  # pos/sales/<id>/receipt/?receipt_text=1 — готовый чек ГНС + QR
    "offline_accept_late",
    # 19.11.2026 К-4/К-5 (k458): партии, FIFO, сроки по партиям, продажа просрочки
    "customer_orders", "customer_order_custom_lines",  # Б-60 customer-orders/ (+cocl2011: строки без товара каталога)
    "company_modules",  # mod2011: users/company/modules/, capabilities.modules, features_changed {modules_version}
    "stock_batches", "costing_fifo", "expiry_batches", "expired_sale_policy", "reservations",  # pos/checkout/ accept_late + X-Confirm-Token (PIN) — продажа из очереди 7…90 суток
)


_VERSION_CACHE = []


def _detect_version():
    """ТЗ 29.10 Б-01: SERVER_VERSION (env) → /home/nur/.server_version → git short hash → "dev"."""
    for name in ("SERVER_VERSION", "NURCRM_SERVER_VERSION"):
        v = (os.getenv(name) or "").strip()
        if v:
            return v
    try:
        with open("/home/nur/.server_version", encoding="utf-8") as fh:
            v = fh.read().strip()
        if v:
            return v
    except OSError:
        pass
    try:
        import subprocess

        from django.conf import settings

        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(settings.BASE_DIR),
                             capture_output=True, text=True, timeout=3)
        v = (out.stdout or "").strip()
        if out.returncode == 0 and v:
            return v
    except Exception:
        pass
    return "dev"


def server_version():
    if not _VERSION_CACHE:
        _VERSION_CACHE.append(_detect_version())
    return _VERSION_CACHE[0]


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _company_features(company):
    """rps2611: признаки, зависящие от компании (касса включает поведение по наличию кода)."""
    out = []
    try:
        from apps.repair.views_ext import public_enabled

        if public_enabled(company):
            out.append("repair_status_public")
    except Exception:
        pass
    return out


class CapabilitiesAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = _company(request.user)
        return Response({
            "api": API_VERSION,
            "server_version": server_version(),
            "features": list(FEATURES) + _company_features(company),  # rps2611
            "company_flags": company_flags(company),
            "fiscal": __import__("apps.main.fiscal_kassa", fromlist=["capabilities_block"]).capabilities_block(company),  # П0-1
            "client": client_label(request) or None,
            "client_is_new": is_new_client(request),
            "plan": __import__("apps.users.plan_mini", fromlist=["plan_block"]).plan_block(company),  # mini2011
            "modules": __import__("apps.users.market_modules", fromlist=["capabilities_block"]).capabilities_block(company),  # mod2011
            "client_versions": __import__("apps.users.client_versions", fromlist=["block"]).block(),  # cver2011
        })


class IsSupportOrSuperuser(permissions.BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and (u.is_superuser or getattr(u, "is_platform_admin", False)))


class ClientVersionsReportAPIView(APIView):
    permission_classes = [IsSupportOrSuperuser]

    def get(self, request):
        from apps.users.models import ClientVersionSeen

        try:
            days = max(1, min(int(request.query_params.get("days", 7)), 90))
        except ValueError:
            days = 7
        since = timezone.now() - timedelta(days=days)
        rows = (
            ClientVersionSeen.objects.filter(last_seen_at__gte=since)
            .values("company_id", "company__name", "client")
            .annotate(last_seen_at=Max("last_seen_at"), hits=Sum("hits"))
            .order_by("company__name", "client")
        )
        companies = {}
        for r in rows:
            c = companies.setdefault(str(r["company_id"]), {"company": str(r["company_id"]), "name": r["company__name"], "clients": []})
            c["clients"].append({"client": r["client"], "last_seen_at": r["last_seen_at"], "hits": r["hits"]})
        return Response({"days": days, "companies": list(companies.values())})
