"""Б-80: действия из чата — карточка клиента, заказ из диалога, документ файлом."""
import logging
import re
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.exceptions import ApiError

from . import services

logger = logging.getLogger("inbox")
ORDER_SOURCES = ("whatsapp", "instagram", "web_chat", "telegram_bot", "telegram_business", "whatsapp_cloud",
                 "whatsapp_bsp", "web_widget")


# ------------------------------------------------------------------ карточка клиента

def chat_card(company, client) -> dict:
    """Один запрос для панели чата: долг, бонусы, 5 последних покупок, открытые заказы, документы, метки, заметки."""
    from apps.main.ai_agent_api import client_debt_summary
    from apps.main.models import Sale, ShowcaseOrder

    debt = client_debt_summary(company, client)
    due = debt.get("next_due_date")
    overdue = 0
    if due:
        try:
            d = date.fromisoformat(str(due)[:10])
            overdue = max((timezone.localdate() - d).days, 0)
        except ValueError:
            overdue = 0
    sales = (Sale.objects.filter(company=company, client=client).exclude(status="canceled").order_by("-created_at")[:5])
    from .models import Conversation
    order_ids = set(Conversation.objects.filter(company=company, contact__client=client, order__isnull=False)
                    .values_list("order_id", flat=True))
    orders = ShowcaseOrder.objects.filter(company=company, status__in=("new", "accepted", "ready"))
    digits = re.sub(r"\D", "", client.phone or "")
    cond = orders.none()
    if order_ids:
        cond = orders.filter(id__in=order_ids)
    if len(digits) >= 9:
        cond = cond | orders.filter(customer_phone__endswith=digits[-9:])
    from .models import Label
    labels = list(Label.objects.filter(company=company, conversations__contact__client=client)
                  .values_list("name", flat=True).distinct())
    return {
        "debt": {"amount": debt.get("debt_total") or "0.00", "due_date": due, "overdue_days": overdue},
        "bonus_balance": str(getattr(client, "bonus_balance", 0) or 0),
        "last_sales": [{"id": str(s.id), "number": s.doc_number, "date": s.created_at.isoformat() if s.created_at else None,
                        "total": str(s.total or 0)} for s in sales],
        "open_orders": [{"id": str(o.id), "number": o.number, "status": o.status, "total": str(o.total),
                         "created_at": o.created_at.isoformat()} for o in cond.distinct().order_by("-created_at")[:10]],
        "documents": [],  # КП и счета (Б-28) на сервере пока нет
        "tags": labels,
        "notes": getattr(client, "notes", "") or getattr(client, "comment", "") or "",
    }


# ------------------------------------------------------------------ заказ из диалога

def attach_order(conv, order):
    """Ссылка order в диалоге и системная строка «Заказ №… создан»."""
    conv.order = order
    conv.save(update_fields=["order", "updated_at"])
    services.system_note(conv, f"Заказ №{order.number} создан")
    services.emit(conv, "inbox_conversation_updated", change="order", order=str(order.id))


def create_order_from_conversation(conv, user, data) -> "ShowcaseOrder":  # noqa: F821
    """Заказ витрины с позициями из тела запроса; цены и остатки — как на витрине."""
    from apps.main.models import ShowcaseOrder, ShowcaseOrderItem
    from apps.main.showcase import services as svc
    from apps.main.showcase.design_schema import ShowcaseFieldError
    from apps.main.showcase.serializers_design import ShowcaseOrderCreateSerializer
    from apps.main.showcase.views_public_design import _price_lines
    from apps.main.variant_utils import InsufficientStock, reserve_stock

    contact, company = conv.contact, conv.company
    client = contact.client if contact.client_id else None
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    name = (customer.get("name") or (client.full_name if client else "") or contact.display_name or "Покупатель").strip()
    phone = (customer.get("phone") or (client.phone if client else "") or contact.phone or "").strip()
    ser = ShowcaseOrderCreateSerializer(data={"customer": {"name": name, "phone": phone or "0"},
                                              "items": data.get("items") or []})
    ser.is_valid(raise_exception=True)
    catalog = svc.get_public_catalog(company)
    try:
        lines, items_total = _price_lines(company, ser.validated_data["items"], catalog)
    except ShowcaseFieldError as exc:
        raise ApiError("invalid", str(getattr(exc, "detail", exc)), fields={"items": [str(exc)]})
    delivery = data.get("delivery") if isinstance(data.get("delivery"), dict) else {}
    dtype = delivery.get("type") or "pickup"
    with transaction.atomic():
        last = ShowcaseOrder.objects.filter(company=company).select_for_update().aggregate(m=Max("number"))["m"] or 0
        order = ShowcaseOrder.objects.create(
            company=company, number=last + 1, status="new", customer_name=name[:128], customer_phone=phone[:64],
            delivery_type=dtype, delivery_address=str(delivery.get("address") or "")[:255],
            source=conv.channel.kind[:32], comment=str(data.get("comment") or "")[:2000],
            total=items_total, delivery_fee=Decimal("0.00"),
        )
        ShowcaseOrderItem.objects.bulk_create([
            ShowcaseOrderItem(order=order, product=ln["product"], variant=ln.get("variant"),
                              product_name=ln["product_name"][:255], qty=ln["qty"], price=ln["price"],
                              discount=ln["discount"], total=ln["total"]) for ln in lines])
        try:
            reserve_stock(lines)
        except InsufficientStock as exc:
            raise ApiError("variant_out_of_stock", str(exc), status_code=409)
        order.stock_reserved = True
        order.save(update_fields=["stock_reserved"])
        attach_order(conv, order)
    return order


def link_public_order(order, data):
    """Крючок публичного заказа витрины (source=whatsapp|instagram|web_chat + conversation_id).
    Привязка только если покупатель подтверждён: телефон заказа = телефон контакта или верный visitor_token."""
    from .models import Conversation
    cid = (data or {}).get("conversation_id") if isinstance(data, dict) else None
    if not str(cid or "").isdigit():
        return None
    conv = (Conversation.objects.select_related("contact", "channel")
            .filter(pk=int(cid), company_id=order.company_id).first())
    if not conv:
        return None
    ok = False
    digits = re.sub(r"\D", "", order.customer_phone or "")
    cdigits = re.sub(r"\D", "", conv.contact.phone or "")
    if len(digits) >= 9 and cdigits and digits[-9:] == cdigits[-9:]:
        ok = True
    token = str((data or {}).get("visitor_token") or "")
    if not ok and token:
        from .widget import session_by_token
        s = session_by_token(token)
        ok = bool(s and s.contact_id == conv.contact_id)
    if not ok:
        return None
    attach_order(conv, order)
    return conv


# ------------------------------------------------------------------ документ файлом

def file_attachment(conv, media_id):
    """Проверка файла компании (MainMedia) для kind=file → вложение."""
    from apps.main.models import MainMedia
    import uuid
    try:
        mid = uuid.UUID(str(media_id))
    except (ValueError, TypeError):
        raise ApiError("invalid", "media_id: неверный идентификатор.", fields={"media_id": ["Неверный идентификатор"]})
    m = MainMedia.objects.filter(pk=mid, company_id=conv.company_id).first()
    if not m:
        raise ApiError("invalid", "Файл не найден.", fields={"media_id": ["Нет такого файла"]}, status_code=404)
    from django.conf import settings
    try:
        url = m.file.url
    except Exception:
        url = ""
    if url.startswith("/"):
        url = (getattr(settings, "TELEGRAM_WEBHOOK_BASE_URL", "") or "https://app.nurcrm.kg").rstrip("/") + url
    return {"media_id": str(m.id), "name": (m.file.name or "").rsplit("/", 1)[-1], "mime": m.content_type,
            "size": m.size, "url": url}
