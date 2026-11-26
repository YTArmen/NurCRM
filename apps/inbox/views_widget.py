"""Б-83: публичные адреса чата витрины и настройки виджета (владелец)."""
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.showcase.services import resolve_public_company
from core.exceptions import ApiError

from . import access, assistant, widget
from .access import InboxPermission


def _client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or ""


def _limited(exc):
    r = Response({"detail": "Слишком много запросов. Повторите позже.", "code": "rate_limited"},
                 status=status.HTTP_429_TOO_MANY_REQUESTS)
    r["Retry-After"] = str(exc.retry_after)
    return r


class PublicBase(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = []

    def company(self, request, slug):
        company = resolve_public_company(slug, request, redirect_get=False)
        return company, widget.require_enabled(company)

    def session(self, request, company, sid):
        token = request.headers.get("X-Visitor-Token") or (
            request.data.get("visitor_token") if isinstance(request.data, dict) else "") or ""
        s = widget.session_by_token(token)
        if s is None or str(s.id) != str(sid) or s.company_id != company.id:
            raise ApiError("invalid_session", "Сеанс не найден или истёк.", status_code=401)
        return s


class PublicConfigView(PublicBase):
    def get(self, request, slug):
        company, w = self.company(request, slug)
        return Response(widget.settings_out(w))


class PublicSessionCreateView(PublicBase):
    def post(self, request, slug):
        company, w = self.company(request, slug)
        data = request.data if isinstance(request.data, dict) else {}
        ip = _client_ip(request)
        tt = str(data.get("turnstile_token") or request.headers.get("X-Turnstile-Token") or "")
        if not assistant.turnstile_verify(tt, ip):
            raise ApiError("turnstile_failed", "Проверка «я не робот» не пройдена.", status_code=403)
        try:
            s, token = widget.create_session(company, ip, str(data.get("page") or ""))
        except widget.RateLimited as exc:
            return _limited(exc)
        return Response({"session_id": str(s.id), "visitor_token": token, "expires_at": s.expires_at.isoformat(),
                         "config": widget.settings_out(w)}, status=status.HTTP_201_CREATED)


class PublicMessagesView(PublicBase):
    def post(self, request, slug, sid):
        company, w = self.company(request, slug)
        s = self.session(request, company, sid)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            msg, created = widget.post_message(s, data.get("text"), data.get("client_msg_id"))
        except widget.RateLimited as exc:
            return _limited(exc)
        return Response({"message": widget.visitor_message_out(msg), "duplicate": not created},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def get(self, request, slug, sid):
        company, w = self.company(request, slug)
        s = self.session(request, company, sid)
        try:
            after = int(request.query_params.get("after") or 0)
            raw_wait = request.query_params.get("wait")
            wait = int(raw_wait) if raw_wait is not None else 25
        except ValueError:
            raise ApiError("invalid", "after, wait: числа.")
        rows = widget.poll(s, after, wait)
        return Response({"results": [widget.visitor_message_out(m) for m in rows],
                         "last_id": rows[-1].id if rows else after})


class PublicContactView(PublicBase):
    def post(self, request, slug, sid):
        company, w = self.company(request, slug)
        s = self.session(request, company, sid)
        d = request.data if isinstance(request.data, dict) else {}
        widget.save_contact(s, d.get("name"), d.get("phone"), d.get("consent"), w, ip=_client_ip(request))
        return Response({"ok": True})


class PublicConsentView(PublicBase):
    """POST .../consent/ {consent: true, phone, name?} — явное согласие передать номер продавцу; пишется в журнал."""

    def post(self, request, slug, sid):
        company, w = self.company(request, slug)
        s = self.session(request, company, sid)
        d = request.data if isinstance(request.data, dict) else {}
        if d.get("consent") is not True:
            raise ApiError("consent_required", "Нужно согласие (consent: true).",
                           fields={"consent": ["Обязательно"]})
        if not str(d.get("phone") or "").strip():
            raise ApiError("invalid", "Укажите телефон.", fields={"phone": ["Обязательное поле"]})
        widget.save_contact(s, d.get("name"), d.get("phone"), True, w, ip=_client_ip(request))
        return Response({"ok": True, "consent": True})


class PublicAssistantView(PublicBase):
    """POST .../sessions/{id}/assistant/ {text, lang?, client_msg_id?} — вопрос ИИ-помощнику."""

    def post(self, request, slug, sid):
        company, w = self.company(request, slug)
        s = self.session(request, company, sid)
        d = request.data if isinstance(request.data, dict) else {}
        res = assistant.ask(s, d.get("text"), str(d.get("lang") or "ru"), _client_ip(request), d.get("client_msg_id"))
        return Response(res)


class PublicSessionView(PublicBase):
    """DELETE .../sessions/{id}/ — «Удалить мой чат» (по токену сеанса)."""

    def delete(self, request, slug, sid):
        company, w = self.company(request, slug)
        s = self.session(request, company, sid)
        out = assistant.delete_session_data(s)
        return Response({"ok": True, **out})


class PublicTelegramLinkView(PublicBase):
    def get(self, request, slug, sid):
        company, w = self.company(request, slug)
        s = self.session(request, company, sid)
        return Response({"url": widget.telegram_link(s, w)})


class WidgetSettingsView(APIView):
    """GET/PUT api/main/inbox/widget/settings/ — настройки виджета (can_manage_channels / владелец)."""
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.require(request.user, "can_manage_channels")
        return Response(widget.settings_out(widget.settings_for(company)))

    def put(self, request):
        company = access.require(request.user, "can_manage_channels")
        w = widget.settings_for(company)
        widget.apply_settings(w, request.data if isinstance(request.data, dict) else {})
        return Response(widget.settings_out(w))

    patch = put


class AssistantSettingsView(APIView):
    """GET/PUT api/main/inbox/assistant/settings/ — помощник витрины: включён, потолок в сутки, «Правила магазина»."""
    permission_classes = [InboxPermission]

    def get(self, request):
        company = access.require(request.user, "can_manage_channels")
        return Response(assistant.settings_out(assistant.get_settings(company), company))

    def put(self, request):
        company = access.require(request.user, "can_manage_channels")
        a = assistant.get_settings(company)
        assistant.apply_settings(a, request.data if isinstance(request.data, dict) else {})
        return Response(assistant.settings_out(a, company))

    patch = put


class AssistantTestView(APIView):
    """POST api/main/inbox/assistant/test/ {text} — «Проверить помощника»."""
    permission_classes = [InboxPermission]

    def post(self, request):
        company = access.require(request.user, "can_manage_channels")
        d = request.data if isinstance(request.data, dict) else {}
        return Response(assistant.test_question(company, d.get("text") or "Сколько стоит доставка?", str(d.get("lang") or "ru")))
