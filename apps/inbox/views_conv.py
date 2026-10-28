"""Б-74, Б-75, Б-77: контакты, диалоги, сообщения, действия, метки, счётчик."""
import hashlib
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import ApiError

from . import access, contacts as contacts_svc, serializers as ser, services
from .access import InboxPermission
from .models import Channel, ChannelContact, Conversation, ConversationRead, DataErasureLog, Label, Message, WebhookEvent


class ConvPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


def _conv_or_error(request, pk, flag="can_view_inbox"):
    company = access.require(request.user, flag)
    conv = (Conversation.objects.filter(company=company, pk=pk)
            .select_related("channel", "contact", "contact__client", "assignee", "channel__bot_settings").first())
    if not conv:
        raise ApiError("not_found", "Диалог не найден.", status_code=404)
    if not services.visible_conversations(request.user, company).filter(pk=pk).exists():
        raise ApiError("permission_denied", "Нет доступа к этому диалогу.", status_code=403,
                       top={"required": "can_view_all_conversations"})
    return company, conv


def _uuid_or_none(v):
    import uuid
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ диалоги

class ConversationListView(APIView):
    permission_classes = [InboxPermission]
    ORDERINGS = {"last_message_at", "updated_at", "created_at", "waiting_since"}

    def get(self, request):
        company = access.require(request.user, "can_view_inbox")
        user, q = request.user, request.query_params
        qs = services.visible_conversations(user, company)
        if q.get("status"):
            qs = qs.filter(status__in=[s for s in q["status"].split(",") if s])
        a = q.get("assignee")
        if a == "me":
            qs = qs.filter(assignee=user)
        elif a == "none":
            qs = qs.filter(assignee__isnull=True)
        elif a:
            qs = qs.filter(assignee_id=_uuid_or_none(a))
        if q.get("channel"):
            qs = qs.filter(channel_id=_uuid_or_none(q["channel"]))
        if q.get("kind"):
            qs = qs.filter(channel__kind=q["kind"])
        if q.get("label"):
            lab = q["label"]
            qs = qs.filter(labels__id=lab) if lab.isdigit() else qs.filter(labels__name=lab)
        if q.get("branch"):
            qs = qs.filter(branch_id=_uuid_or_none(q["branch"]))
        if q.get("client"):
            qs = qs.filter(contact__client_id=_uuid_or_none(q["client"]))
        if q.get("waiting_over_min"):
            try:
                qs = qs.filter(waiting_since__lte=timezone.now() - timedelta(minutes=int(q["waiting_over_min"])))
            except ValueError:
                raise ApiError("invalid", "waiting_over_min: число минут.", fields={"waiting_over_min": ["Число"]})
        if q.get("unread") in ("true", "1"):
            from django.db.models import F
            from django.db.models.functions import Coalesce
            qs = qs.annotate(_lr=Coalesce(services.read_pointer(user), 0)).filter(last_inbound_message_id__gt=F("_lr"))
        if q.get("search"):
            s = q["search"].strip()
            qs = qs.filter(Q(contact__display_name__icontains=s) | Q(contact__username__icontains=s)
                           | Q(contact__phone__icontains=s) | Q(last_message_text__icontains=s)
                           | Q(contact__client__full_name__icontains=s))
        if q.get("updated_after"):
            dt = parse_datetime(q["updated_after"])
            if not dt:
                raise ApiError("invalid", "updated_after: дата ISO 8601.", fields={"updated_after": ["Неверная дата"]})
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            qs = qs.filter(updated_at__gt=dt)
        qs = qs.distinct()

        etag = None
        if q.get("updated_after") or q.get("etag") in ("1", "true"):
            etag = self._etag(request, company, qs)
            if request.headers.get("If-None-Match") == etag:
                resp = HttpResponse(status=304)
                resp["ETag"] = etag
                return resp

        ordering = q.get("ordering") or "-last_message_at"
        field = ordering.lstrip("-")
        if field not in self.ORDERINGS:
            raise ApiError("invalid", "ordering: last_message_at, updated_at, created_at или waiting_since.",
                           fields={"ordering": ["Недопустимое значение"]})
        qs = qs.select_related("channel", "contact", "contact__client", "assignee").prefetch_related("labels")
        qs = qs.order_by(ordering, "-id" if ordering.startswith("-") else "id")
        pager = ConvPagination()
        page = pager.paginate_queryset(qs, request, view=self)
        ids = [c.id for c in page]
        unread = services.page_unread(ids, user)
        see_phone = access.can_see_phone(user)
        data = pager.get_paginated_response(
            [ser.conversation_out(c, user, unread.get(c.id, 0), see_phone) for c in page]).data
        resp = Response(data)
        if etag:
            resp["ETag"] = etag
        return resp

    def _etag(self, request, company, qs):
        agg = qs.aggregate(m=Max("updated_at"), n=Count("id", distinct=True))
        mine = ConversationRead.objects.filter(user=request.user, conversation__company=company).aggregate(m=Max("updated_at"))["m"]
        raw = f"{request.user.id}|{request.get_full_path()}|{agg['m']}|{agg['n']}|{mine}"
        return '"' + hashlib.md5(raw.encode()).hexdigest() + '"'


class ConversationDetailView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request, pk):
        _, conv = _conv_or_error(request, pk)
        unread = services.page_unread([conv.id], request.user).get(conv.id, 0)
        return Response(ser.conversation_out(conv, request.user, unread))


class MessagesView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request, pk):
        _, conv = _conv_or_error(request, pk)
        q = request.query_params
        try:
            size = min(max(int(q.get("page_size") or 50), 1), 100)
            before = int(q["before"]) if q.get("before") else None
            after = int(q["after"]) if q.get("after") else None
        except ValueError:
            raise ApiError("invalid", "before, after, page_size: числа.")
        qs = Message.objects.filter(conversation=conv).select_related("sender_user")
        if after is not None:
            rows = list(qs.filter(id__gt=after).order_by("id")[: size + 1])
            more_after, more_before = len(rows) > size, True
            rows = rows[:size]
        else:
            if before is not None:
                qs = qs.filter(id__lt=before)
            rows = list(qs.order_by("-id")[: size + 1])
            more_before, more_after = len(rows) > size, before is not None
            rows = list(reversed(rows[:size]))
        return Response({"results": [ser.message_out(m) for m in rows],
                         "has_more_before": more_before, "has_more_after": more_after})

    def post(self, request, pk):
        company, conv = _conv_or_error(request, pk, "can_reply_inbox")
        data = request.data if isinstance(request.data, dict) else {}
        key = (request.headers.get("Idempotency-Key") or data.get("idempotency_key") or "").strip()
        if key and not (16 <= len(key) <= 64 and all(ch.isalnum() or ch in "-_" for ch in key)):
            raise ApiError("invalid", "Idempotency-Key: 16–64 символа [A-Za-z0-9_-].", fields={"Idempotency-Key": ["Неверный формат"]})
        msg, replayed = services.create_outgoing(conv, request.user, data, key)
        body = {"message": ser.message_out(msg)}
        if msg.template:
            body["cost"] = {"category": msg.template.get("category")}
        if replayed:
            body["replayed"] = True
        return Response(body, status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED)


# ------------------------------------------------------------------ действия

class AssignView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        company, conv = _conv_or_error(request, pk, "can_reply_inbox")
        uid = request.data.get("user")
        if uid in (None, ""):
            conv.assignee = None
        else:
            from django.contrib.auth import get_user_model
            target = get_user_model().objects.filter(pk=_uuid_or_none(uid), company=company).first()
            if not target:
                raise ApiError("invalid", "Сотрудник не найден.", fields={"user": ["Нет такого сотрудника"]})
            conv.assignee = target
            conv.first_taken_at = conv.first_taken_at or timezone.now()
        conv.save()
        services.emit(conv, "inbox_conversation_updated", change="assignee")
        return Response(ser.conversation_out(conv, request.user))


class TakeView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        company = access.require(request.user, "can_reply_inbox")
        cur = Conversation.objects.filter(company=company, pk=pk).only("assignee_id").first()
        if not cur:
            raise ApiError("not_found", "Диалог не найден.", status_code=404)
        if cur.assignee_id is None:  # занятый чужим — отвечаем 409 ниже, а не 403
            _conv_or_error(request, pk, "can_reply_inbox")
        conv = services.take(pk, request.user)
        return Response(ser.conversation_out(conv, request.user))


class StatusView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        _, conv = _conv_or_error(request, pk, "can_reply_inbox")
        d = request.data
        until = None
        if d.get("snoozed_until"):
            until = parse_datetime(str(d["snoozed_until"]))
            if until and timezone.is_naive(until):
                until = timezone.make_aware(until)
        services.set_status(conv, d.get("status"), until, d.get("closed_reason") or "")
        return Response(ser.conversation_out(conv, request.user))


class LabelsActionView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        company, conv = _conv_or_error(request, pk, "can_reply_inbox")
        add, remove = request.data.get("add") or [], request.data.get("remove") or []
        if not isinstance(add, list) or not isinstance(remove, list):
            raise ApiError("invalid", "add и remove — списки id меток.")
        found = {l.id: l for l in Label.objects.filter(company=company, id__in=[*add, *remove])} if (add or remove) else {}
        missing = [i for i in [*add, *remove] if i not in found]
        if missing:
            raise ApiError("invalid", "Метка не найдена.", fields={"labels": [f"Нет метки {missing[0]}"]})
        conv.labels.add(*[found[i] for i in add])
        conv.labels.remove(*[found[i] for i in remove])
        conv.save(update_fields=["updated_at"])
        services.emit(conv, "inbox_conversation_updated", change="labels")
        return Response(ser.conversation_out(conv, request.user))


class ReadView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        _, conv = _conv_or_error(request, pk)
        services.mark_read(conv, request.user)
        return Response({"unread_count": 0})


class TransferView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        company, conv = _conv_or_error(request, pk, "can_reply_inbox")
        from apps.users.models import Branch
        br = Branch.objects.filter(pk=_uuid_or_none(request.data.get("branch")), company=company).first()
        if not br:
            raise ApiError("invalid", "Филиал не найден.", fields={"branch": ["Нет такого филиала"]})
        conv.branch = br
        conv.assignee = None
        conv.save()
        services.emit(conv, "inbox_conversation_updated", change="branch")
        return Response(ser.conversation_out(conv, request.user))


# ------------------------------------------------------------------ метки компании

def _label_out(l):
    return {"id": l.id, "name": l.name, "color": l.color}


class LabelListCreateView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.require(request.user, "can_view_inbox")
        return Response([_label_out(l) for l in Label.objects.filter(company=company)])

    def post(self, request):
        company = access.require(request.user, "can_reply_inbox")
        name = str(request.data.get("name") or "").strip()[:64]
        if not name:
            raise ApiError("invalid", "Укажите название метки.", fields={"name": ["Обязательное поле"]})
        if Label.objects.filter(company=company, name=name).exists():
            raise ApiError("label_exists", "Метка с таким названием уже есть.", status_code=409)
        l = Label.objects.create(company=company, name=name, color=str(request.data.get("color") or "#888888")[:16])
        return Response(_label_out(l), status=status.HTTP_201_CREATED)


class LabelDetailView(APIView):
    permission_classes = [InboxPermission]

    def _get(self, request, pk, flag):
        company = access.require(request.user, flag)
        l = Label.objects.filter(company=company, pk=pk).first()
        if not l:
            raise ApiError("not_found", "Метка не найдена.", status_code=404)
        return l

    def get(self, request, pk):
        return Response(_label_out(self._get(request, pk, "can_view_inbox")))

    def patch(self, request, pk):
        l = self._get(request, pk, "can_reply_inbox")
        if "name" in request.data:
            name = str(request.data["name"] or "").strip()[:64]
            if not name:
                raise ApiError("invalid", "Пустое название.", fields={"name": ["Обязательное поле"]})
            if Label.objects.filter(company=l.company, name=name).exclude(pk=l.pk).exists():
                raise ApiError("label_exists", "Метка с таким названием уже есть.", status_code=409)
            l.name = name
        if "color" in request.data:
            l.color = str(request.data["color"] or "")[:16]
        l.save()
        return Response(_label_out(l))

    def delete(self, request, pk):
        self._get(request, pk, "can_reply_inbox").delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ счётчик (Б-77)

class UnreadCountView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.require(request.user, "can_view_inbox")
        return Response(services.unread_counts(request.user, company))


# ------------------------------------------------------------------ контакты (Б-74)

def _contact_or_404(request, pk, flag="can_view_inbox"):
    company = access.require(request.user, flag)
    c = ChannelContact.objects.filter(company=company, pk=pk).select_related("client", "channel").first()
    if not c:
        raise ApiError("not_found", "Контакт не найден.", status_code=404)
    return company, c


class ContactDetailView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request, pk):
        _, c = _contact_or_404(request, pk)
        return Response(contacts_svc.serialize_contact(c))


class ContactLinkView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        company, c = _contact_or_404(request, pk, "can_reply_inbox")
        if not access.can_see_phone(request.user):
            raise ApiError("permission_denied", "Нет доступа: Клиенты", status_code=403, top={"required": "can_view_clients"})
        from apps.main.models import Client
        client = Client.objects.filter(company=company, pk=_uuid_or_none(request.data.get("client"))).first()
        if not client:
            raise ApiError("invalid", "Клиент не найден.", fields={"client": ["Нет такого клиента"]})
        contacts_svc.link(c, client)
        return Response(contacts_svc.serialize_contact(c))


class ContactUnlinkView(APIView):
    permission_classes = [InboxPermission]

    def post(self, request, pk):
        _, c = _contact_or_404(request, pk, "can_reply_inbox")
        if not access.can_see_phone(request.user):
            raise ApiError("permission_denied", "Нет доступа: Клиенты", status_code=403, top={"required": "can_view_clients"})
        contacts_svc.unlink(c)
        return Response(contacts_svc.serialize_contact(c))


class ContactConsentsView(APIView):
    """PATCH …/contacts/{id}/consents/ {service?, marketing?} — сотрудник (source=cashier)."""
    permission_classes = [InboxPermission]

    def patch(self, request, pk):
        _, c = _contact_or_404(request, pk, "can_reply_inbox")
        d = request.data
        contacts_svc.set_consents(c, service=d.get("service") if "service" in d else None,
                                  marketing=d.get("marketing") if "marketing" in d else None, source="cashier")
        return Response(contacts_svc.serialize_contact(c))


class ContactDataEraseView(APIView):
    """DELETE …/contacts/{id}/data/ — удалить переписку контакта (запрос о персональных данных), владелец."""
    permission_classes = [InboxPermission]

    def delete(self, request, pk):
        company, c = _contact_or_404(request, pk)
        if not access.is_owner_like(request.user):
            raise ApiError("permission_denied", "Удалять данные контакта может только владелец.", status_code=403,
                           top={"required": "owner"})
        with transaction.atomic():
            msgs = Message.objects.filter(conversation__contact=c).count()
            convs = Conversation.objects.filter(contact=c)
            n_conv = convs.count()
            convs.delete()
            ext = c.external_id
            if c.channel.kind.startswith("telegram"):
                from apps.main.telegram_bot.models import TelegramCustomerProfile, TelegramInquiry
                TelegramInquiry.objects.filter(company=company, chat_id=ext).delete()
                TelegramCustomerProfile.objects.filter(company=company, chat_id=ext).delete()
            DataErasureLog.objects.create(
                company=company, channel_id=c.channel_id, contact_id=c.id, requested_by=request.user,
                contact_external_hash=hashlib.sha256(ext.encode()).hexdigest(),
                messages_deleted=msgs, conversations_deleted=n_conv,
            )
            # карточку оставляем обезличенной: нужна для отказов (СТОП) и уникальности
            c.display_name = c.username = c.phone = c.avatar_url = ""
            c.client, c.client_candidates = None, []
            c.save()
        return Response({"deleted": True, "messages": msgs, "conversations": n_conv})


class ClientChannelsView(APIView):
    """GET api/main/inbox/clients/{id}/channels/ — контакты клиента по каналам."""
    permission_classes = [InboxPermission]

    def get(self, request, client_id):
        company = access.require(request.user, "can_view_inbox")
        if not access.can_see_phone(request.user):
            raise ApiError("permission_denied", "Нет доступа: Клиенты", status_code=403, top={"required": "can_view_clients"})
        from apps.main.models import Client
        if not Client.objects.filter(company=company, pk=client_id).exists():
            raise ApiError("not_found", "Клиент не найден.", status_code=404)
        rows = ChannelContact.objects.filter(company=company, client_id=client_id).select_related("channel", "client")
        out = []
        for c in rows:
            d = contacts_svc.serialize_contact(c, with_candidates=False)
            d["channel"] = {"id": str(c.channel_id), "kind": c.channel.kind, "name": c.channel.name}
            out.append(d)
        return Response({"count": len(out), "results": out})


# ------------------------------------------------------------------ здоровье вебхуков (Б-76 п. 8)

class WebhookHealthView(APIView):
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.company_of(request.user)
        if not company or not access.is_owner_like(request.user):
            raise ApiError("permission_denied", "Только владелец или админ.", status_code=403, top={"required": "owner"})
        day = timezone.now() - timedelta(hours=24)
        out = []
        for ch in Channel.objects.filter(company=company).order_by("created_at", "id"):
            ev = WebhookEvent.objects.filter(channel=ch)
            last = ev.aggregate(m=Max("received_at"))["m"]
            bad = ev.filter(received_at__gte=day).filter(Q(signature_ok=False) | ~Q(error=""))
            out.append({
                "channel": {"id": str(ch.id), "kind": ch.kind, "name": ch.name},
                "status": ch.status,
                "last_event_at": last.isoformat() if last else None,
                "errors_24h": bad.count(),
                "invalid_signatures_24h": ev.filter(received_at__gte=day, signature_ok=False).count(),
                "queue_length": (ev.filter(processed_at__isnull=True, signature_ok=True).count()
                                 + Message.objects.filter(channel=ch, status="queued", direction="out").exclude(kind="note").count()),
            })
        return Response({"channels": out})
