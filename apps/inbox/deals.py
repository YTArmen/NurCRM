"""Б-82: воронка магазина на сервере. Вариант «main/deals + добавочные поля»: сделка — обычная main.Deal,
связи с клиентом/диалогом/источником — в отдельной модели DealExt (apps/main/models.py не трогаем)."""
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.dateparse import parse_datetime

from core.exceptions import ApiError

from .models import Conversation, DealExt

STAGES = ["new", "work", "agreed", "paid", "lost"]
STAGE_STATUS = {"new": "lead", "work": "prospect", "agreed": "deal", "paid": "closed", "lost": "lost"}
SOURCES = ("telegram", "whatsapp", "instagram", "call", "visit", "referral", "site", "other")
KIND_SOURCE = {"telegram_bot": "telegram", "telegram_business": "telegram", "whatsapp_cloud": "whatsapp",
               "whatsapp_bsp": "whatsapp", "instagram": "instagram", "web_widget": "site"}
PIPELINE_NAME = "Воронка магазина"


def default_pipeline(company, user):
    from apps.main.models import Pipeline
    p = Pipeline.objects.filter(company=company, name=PIPELINE_NAME).first()
    if p is None:
        p = Pipeline.objects.create(company=company, owner=user, name=PIPELINE_NAME, stages=list(STAGES))
    return p


def _stage(pipeline, stage):
    stage = str(stage or "new")
    allowed = [s if isinstance(s, str) else (s.get("key") or s.get("name")) for s in (pipeline.stages or [])]
    if stage not in allowed and stage not in STAGES:
        raise ApiError("invalid", f"stage: один из {', '.join(allowed or STAGES)}.", fields={"stage": ["Недопустимое значение"]})
    return stage


def _money(v):
    try:
        d = Decimal(str(v if v not in (None, "") else 0))
    except InvalidOperation:
        raise ApiError("invalid", "amount: число.", fields={"amount": ["Недопустимое значение"]})
    if d < 0 or d >= Decimal("10000000000"):
        raise ApiError("invalid", "amount: от 0 до 9 999 999 999.", fields={"amount": ["Недопустимое значение"]})
    return d.quantize(Decimal("0.01"))


def _find_client(company, phone):
    from apps.main.models import Client

    from . import contacts
    norm = contacts.normalize_phone(phone) if phone else ""
    if not norm:
        return None
    found = list(Client.objects.filter(company=company, phone_normalized=norm)[:2])
    return found[0] if len(found) == 1 else None


def out(ext) -> dict:
    d = ext.deal
    return {
        "id": str(d.id), "external_id": ext.external_id or None, "name": d.title, "phone": ext.phone or d.contact.phone,
        "amount": str(d.value), "stage": d.stage, "status": d.status, "source": ext.source, "note": ext.note,
        "client": {"id": str(ext.client_id), "full_name": ext.client.full_name} if ext.client_id else None,
        "conversation": ext.conversation_id, "instagram": ext.instagram_username or None,
        "telegram_username": ext.telegram_username or None, "chat_id": ext.chat_id or None,
        "created_at": d.created_at.isoformat(), "updated_at": d.updated_at.isoformat(),
    }


def create(company, user, data, *, external_id="", conversation=None):
    from apps.main.models import Client, Contact, Deal

    name = str(data.get("name") or data.get("title") or "").strip()[:255]
    if not name:
        raise ApiError("invalid", "Укажите name.", fields={"name": ["Обязательное поле"]})
    source = data.get("source") or "other"
    if source not in SOURCES:
        raise ApiError("invalid", f"source: {', '.join(SOURCES)}.", fields={"source": ["Недопустимое значение"]})
    pipeline = default_pipeline(company, user)
    stage = _stage(pipeline, data.get("stage"))
    phone = str(data.get("phone") or "").strip()[:32]
    client = None
    if data.get("client"):
        client = Client.objects.filter(company=company, pk=data["client"]).first()
        if not client:
            raise ApiError("invalid", "Клиент не найден.", fields={"client": ["Нет такого клиента"]})
    elif conversation is not None and conversation.contact.client_id:
        client = conversation.contact.client
    elif phone:
        client = _find_client(company, phone)
    with transaction.atomic():
        contact = Contact.objects.create(company=company, owner=user, name=name[:128], phone=phone or (client.phone if client else ""),
                                         email="", address="", client_company="")
        deal = Deal.objects.create(company=company, pipeline=pipeline, contact=contact, assigned_to=user, title=name,
                                   value=_money(data.get("amount")), stage=stage, status=STAGE_STATUS.get(stage, "deal"))
        ext = DealExt.objects.create(
            deal=deal, company=company, client=client, source=source, conversation=conversation,
            note=str(data.get("note") or "")[:4000], phone=phone, external_id=external_id[:64],
            instagram_username=str(data.get("instagram") or data.get("instagram_username") or "")[:64],
            telegram_username=str(data.get("telegram_username") or "")[:64], chat_id=str(data.get("chat_id") or "")[:64])
        if conversation is not None:
            Conversation.objects.filter(pk=conversation.pk).update(deal_id=deal.id)
    return ext


def update(ext, data):
    d = ext.deal
    if "name" in data or "title" in data:
        d.title = str(data.get("name") or data.get("title") or "").strip()[:255] or d.title
    if "amount" in data:
        d.value = _money(data["amount"])
    if "stage" in data:
        d.stage = _stage(d.pipeline, data["stage"])
        d.status = STAGE_STATUS.get(d.stage, d.status)
    d.save()
    for f, key in (("note", "note"), ("instagram_username", "instagram"), ("telegram_username", "telegram_username"),
                   ("chat_id", "chat_id"), ("phone", "phone")):
        if key in data:
            setattr(ext, f, str(data[key] or "")[:4000 if f == "note" else 64])
    if "source" in data:
        if data["source"] not in SOURCES:
            raise ApiError("invalid", f"source: {', '.join(SOURCES)}.", fields={"source": ["Недопустимое значение"]})
        ext.source = data["source"]
    ext.save()
    return ext


def import_cards(company, user, cards) -> dict:
    """Перенос funnel.json: повтор по id без дублей (существующая карточка обновляется)."""
    if not isinstance(cards, list):
        raise ApiError("invalid", "Ожидается массив карточек.", fields={"cards": ["Ожидается массив"]})
    if len(cards) > 5000:
        raise ApiError("invalid", "Не больше 5000 карточек за раз.")
    created = updated = 0
    errors = []
    for i, card in enumerate(cards):
        try:
            if not isinstance(card, dict) or not str(card.get("id") or "").strip():
                raise ApiError("invalid", "У карточки нет id.")
            cid = str(card["id"]).strip()[:64]
            data = {"name": card.get("name"), "phone": card.get("phone"), "amount": card.get("amount"),
                    "note": card.get("note"), "stage": card.get("stage") or "new",
                    "source": card.get("source") if card.get("source") in SOURCES else "other",
                    "instagram": card.get("instagram"), "telegram_username": card.get("telegram_username"),
                    "chat_id": card.get("chat_id")}
            ext = DealExt.objects.filter(company=company, external_id=cid).select_related("deal", "deal__pipeline").first()
            with transaction.atomic():
                if ext:
                    update(ext, data)
                    updated += 1
                else:
                    ext = create(company, user, data, external_id=cid)
                    created += 1
                from apps.main.models import Deal
                upd = {}
                for key, field in (("created_at", "created_at"), ("updated_at", "updated_at")):
                    dt = parse_datetime(str(card.get(key) or "")) if card.get(key) else None
                    if dt:
                        upd[field] = dt
                if upd and ext.deal_id:
                    Deal.objects.filter(pk=ext.deal_id).update(**upd)
        except ApiError as exc:
            errors.append({"index": i, "id": str(card.get("id") if isinstance(card, dict) else ""), "code": exc.code,
                           "detail": str(exc.detail)})
    return {"created": created, "updated": updated, "errors": errors, "total": len(cards)}
