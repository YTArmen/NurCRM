"""
Функции компании из support.nurcrm.kg (feat2011, 20.11.2026): поддержка находит компанию и включает функции
галочками — без знания кодов. Единый каталог FEATURES ниже: панель строит галочки по нему.

Доступ — как у промокодов и «Вида магазина»: платформенный админ ИЛИ X-Support-Key (nginx /nurfeatures/).

GET  /api/users/platform-admin/company-features/?q=<название | почта | slug | id> → {"results": [компания…], "catalog": [...]}
GET  /api/users/platform-admin/company-features/?company_id=<uuid>               → {"results": [компания], "catalog": [...]}
POST /api/users/platform-admin/company-features/
     {"company_id": "<uuid>", "set": {"kassa-scales": true, "kassa-pricetag": false}, "until": "YYYY-MM-DD" | null}
     → компания. Только коды из каталога. Включение — CompanyAddon active, source=support (until пусто — бессрочно);
     выключение — active=false (запись и история остаются).
"""
import uuid
from datetime import date
from urllib.parse import unquote

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import Company, CompanyAddon
from apps.users.promo_views import IsPlatformAdminOrSupportKey
from apps.users.sphere_support_views import SupportCompanyKindAPIView

# (группа, [(код, название, подсказка)]). Касса NurMarket читает только коды kassa-* (company/addons/).
FEATURES = [
    ("Касса NurMarket", [
        ("kassa-scales", "Весы", "весовой товар, весы и весы-принтеры"),
        ("kassa-labeleditor", "Печать штрихкода (этикетки)", "редактор этикеток: штрихкод, название, цена"),
        ("kassa-pricetag", "Ценник на товар", "редактор ценников"),
        ("kassa-bulktag", "Массовые ценники", "ценники на много товаров сразу"),
        ("kassa-loyalty", "Программа лояльности", "бонусы покупателям"),
        ("kassa-timesheet", "Табель сотрудников", ""),
        ("kassa-analytics", "Аналитика склада", ""),
        ("kassa-shiftstats", "Статистика смен", ""),
        ("kassa-export", "Выгрузка (экспорт)", ""),
        ("kassa-telegram", "Отчёты в Telegram", "бот владельца"),
        ("kassa-language", "Язык кассы", "английский, турецкий, узбекский"),
    ]),
    ("Сайт (веб-админка)", [
        ("barcode-print", "Печать штрихкодов и ценников на сайте", "кнопки «Печать ценников» в товарах, складе, закупках (Старт)"),
        ("extra-employees", "Сотрудники сверх 3", "снимает лимит «Старта»: 3 человека с владельцем"),
        ("whatsapp", "WhatsApp магазина", ""),
        ("procurement", "Закупки", ""),
        ("branches", "Филиалы", ""),
        ("analytics", "Аналитика", ""),
        ("inventory-documents", "Инвентаризация и документы", ""),
        ("double-warehouse", "Два склада", ""),
        ("online-showcase", "Онлайн-витрина", ""),
    ]),
    ("Разделы меню", [
        ("m-debts", "Книга долгов", ""),
        ("m-salary", "Зарплата сотрудников", ""),
        ("m-roles", "Свои роли", ""),
        ("m-documents", "Инвентаризация и перемещения", ""),
        ("m-supplier-returns", "Возвраты поставщику", ""),
        ("m-variants", "Варианты товара", "размеры, цвета"),
        ("m-rentals", "Прокат", ""),
        ("m-customer-display", "Экран покупателя", ""),
    ]),
    ("Пакеты «Стандарта» (для «Старта»)", [
        ("ai", "ИИ-советник", ""),
        ("clients", "Клиенты и бонусы", ""),
        ("sales_analytics", "Аналитика продаж", ""),
        ("restock", "Подсказки по закупкам", ""),
        ("salary", "Зарплата", ""),
        ("debts", "Долги", ""),
        ("service", "Сервис", ""),
    ]),
]
CODES = {c for _, items in FEATURES for c, _, _ in items}
NAMES = {c: n for _, items in FEATURES for c, n, _ in items}


def catalog():
    return [{"group": g, "items": [{"code": c, "name": n, "hint": h} for c, n, h in items]} for g, items in FEATURES]


def _payload(company):
    today = timezone.localdate()
    owner = getattr(company, "owner", None)
    feats, other = {}, []
    for a in CompanyAddon.objects.filter(company=company).order_by("code"):
        row = {"on": a.is_effective(today), "until": a.until.isoformat() if a.until else None, "source": a.source}
        if a.code in CODES:
            feats[a.code] = row
        elif row["on"]:
            other.append(a.code)
    plan = getattr(company, "subscription_plan", None)
    return {
        "id": str(company.id), "name": company.name, "slug": company.slug,
        "owner_email": getattr(owner, "email", "") or "",
        "plan": getattr(plan, "name", "") or "",
        "end_date": company.end_date.isoformat() if company.end_date else None,
        "features": feats, "other_active": other,
    }


class SupportCompanyFeaturesCatalogAPIView(APIView):
    permission_classes = [IsPlatformAdminOrSupportKey]

    def get(self, request):
        cid = (request.query_params.get("company_id") or "").strip()
        if cid:
            try:
                rows = list(Company.objects.filter(id=uuid.UUID(cid)).select_related("owner", "subscription_plan"))
            except ValueError:
                rows = []
        else:
            found = SupportCompanyKindAPIView().get(request).data.get("results") or []
            ids = [r["id"] for r in found]
            rows = list(Company.objects.filter(id__in=ids).select_related("owner", "subscription_plan").order_by("name"))
        return Response({"results": [_payload(c) for c in rows], "catalog": catalog()})

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            company = Company.objects.filter(id=uuid.UUID(str(data.get("company_id")))).first()
        except ValueError:
            company = None
        if company is None:
            return Response({"detail": "Компания не найдена."}, status=status.HTTP_404_NOT_FOUND)
        changes = data.get("set") or {}
        if not isinstance(changes, dict) or not changes:
            return Response({"detail": "Ничего не выбрано."}, status=status.HTTP_400_BAD_REQUEST)
        unknown = [k for k in changes if k not in CODES]
        if unknown:
            return Response({"detail": "Неизвестная функция: " + ", ".join(unknown)}, status=status.HTTP_400_BAD_REQUEST)
        until = None
        if data.get("until"):
            try:
                until = date.fromisoformat(str(data["until"])[:10])
            except ValueError:
                return Response({"detail": "Дата «до» — ГГГГ-ММ-ДД."}, status=status.HTTP_400_BAD_REQUEST)
            if until < timezone.localdate():
                return Response({"detail": "Дата «до» уже прошла."}, status=status.HTTP_400_BAD_REQUEST)
        who = unquote(request.headers.get("X-Support-Operator", ""))[:60] or getattr(request.user, "email", "") or "поддержка"
        note = f"Функции: {who}, {timezone.localdate():%d.%m.%Y}"
        with transaction.atomic():
            for code, on in changes.items():
                addon = CompanyAddon.objects.select_for_update().filter(company=company, code=code).first()
                if on:
                    addon = addon or CompanyAddon(company=company, code=code)
                    addon.active, addon.until, addon.source, addon.note = True, until, "support", note[:255]
                    addon.save()
                elif addon is not None and addon.active:
                    addon.active, addon.note = False, note[:255]
                    addon.save(update_fields=["active", "note", "updated_at"])
        company = Company.objects.select_related("owner", "subscription_plan").get(pk=company.pk)
        return Response(_payload(company))
