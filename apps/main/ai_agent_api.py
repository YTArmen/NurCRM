"""ТЗ Н-24: API для голосового агента и ИИ-ботов (ключи с областями)."""
import re
import uuid
from collections import defaultdict
from decimal import Decimal
from difflib import SequenceMatcher

from django.db.models import Q
from rest_framework import permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from apps.integrations.models import ApiKey
from apps.integrations.permissions import HasApiKeyScope
from apps.main.models import Client, Product, ShowcaseOrder
from apps.main.phone_utils import normalize_phone_e164, phone_digits
from apps.main.telegram_bot.models import VoiceCall


def agent_company(request):
    """Компания ключа или пользователя."""
    if isinstance(request.auth, ApiKey):
        return request.auth.company
    user = request.user
    company = getattr(user, "owned_company", None) or getattr(user, "company", None)
    if company is None:
        raise ValidationError({"detail": "Пользователь не привязан к компании."})
    return company


def _can(request, scope):
    """Видит ли вызывающий данные области: обычный пользователь — да, ключ — по области."""
    if not isinstance(request.auth, ApiKey):
        return True
    k = request.auth
    return k.has_scope(scope) or k.has_scope(ApiKey.Scope.WRITE) or k.has_scope(ApiKey.Scope.READ)


class AgentBase(APIView):
    permission_classes = [permissions.IsAuthenticated, HasApiKeyScope]
    required_scope = None


# ---------------------------------------------------------------- поиск товара
_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "ү": "u", "ө": "o", "ң": "n",
}
_LAT_RULES = (("ck", "k"), ("kh", "h"), ("c", "k"), ("q", "k"), ("w", "v"), ("x", "ks"), ("y", "i"), ("j", "zh"))


def skeleton(text: str) -> str:
    """Транслит в латиницу и сведение близких букв: «кока-кола» == «Coca Cola»."""
    s = "".join(_CYR.get(ch, ch) for ch in (text or "").lower())
    s = re.sub(r"[^a-z0-9]+", "", s)
    for a, b in _LAT_RULES:
        s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)


def _tokens(text: str):
    toks = [skeleton(t) for t in re.split(r"[^\w]+", (text or "").lower()) if t]
    return [t for t in toks if t]


def _score(q_tokens, q_joined, name_tokens, name_joined, fuzzy):
    if q_joined and q_joined in name_joined:
        return 1.0
    total = 0.0
    for qt in q_tokens:
        best = 0.0
        for nt in name_tokens:
            if qt == nt:
                best = 1.0
                break
            if len(qt) >= 3 and (nt.startswith(qt) or qt in nt):
                best = max(best, 0.9)
            elif fuzzy and abs(len(qt) - len(nt)) <= 3 and len(qt) >= 3:
                sm = SequenceMatcher(None, qt, nt)
                if sm.real_quick_ratio() >= 0.6 and sm.quick_ratio() >= 0.6:
                    best = max(best, sm.ratio())
        total += best
    return total / len(q_tokens) if q_tokens else 0.0


def search_products(company, q, branch=None, limit=5):
    q_tokens = _tokens(q)
    if not q_tokens:
        return []
    q_joined = "".join(q_tokens)
    qs = Product.objects.filter(company=company).only("id", "name", "price", "quantity", "barcode", "unit", "article")
    if branch:
        qs = qs.filter(Q(branch_id=branch) | Q(branch__isnull=True))
    prepared = []
    for p in qs.iterator(chunk_size=2000):
        nt = _tokens(p.name)
        prepared.append((p, nt, "".join(nt)))
    for fuzzy in (False, True):
        scored = []
        for p, nt, nj in prepared:
            sc = _score(q_tokens, q_joined, nt, nj, fuzzy)
            if sc >= (0.6 if fuzzy else 0.85):
                scored.append((sc, p))
        if len(scored) >= limit or fuzzy:
            break
    scored.sort(key=lambda x: (-x[0], len(x[1].name), x[1].name))
    return scored[:limit]


class ProductVoiceSearchAPIView(AgentBase):
    """GET /api/main/products/search/?q= — top-5 с ценой и остатком; терпим к опечаткам и транслиту."""
    required_scope = ApiKey.Scope.CATALOG_READ

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        if not q:
            raise ValidationError({"q": "Обязательный параметр."})
        company = agent_company(request)
        show_stock = _can(request, ApiKey.Scope.STOCK_READ)
        rows = []
        for sc, p in search_products(company, q, branch=request.query_params.get("branch")):
            row = {
                "id": str(p.id), "name": p.name, "price": str(p.price), "unit": p.unit,
                "barcode": p.barcode, "score": round(sc, 2),
            }
            if show_stock:
                row["stock"] = str(p.quantity)
                row["in_stock"] = (p.quantity or 0) > 0
            rows.append(row)
        return Response({"query": q, "results": rows})


# ---------------------------------------------------------------- долг клиента
def client_debt_summary(company, client):
    """ТЗ Н-24 (3): {debt_total, since, next_due_date} — то же число, что в clients/debtors/."""
    from apps.main.kassa_views import _debtor_sales, _open_debts
    from apps.main.pos_utils import money

    total = Decimal("0")
    since = None
    if client is not None:
        for _cid, _sale, remaining, created_at in _open_debts([company.id], Client.objects.filter(id=client.id).values("id")):
            total += remaining
            if created_at and (since is None or created_at < since):
                since = created_at
    dues = []
    if client is not None:
        dues = [r["due_date"] for r in _debtor_sales([client.id], company.id).get(client.id, []) if r["due_date"]]
    return {
        "debt_total": str(money(total)),
        "since": since.date().isoformat() if since else None,
        "next_due_date": min(dues) if dues else None,
    }


# ---------------------------------------------------------------- черновик заказа
DRAFT_SOURCES = ("voice_ai", "telegram")


def _draft_view_base():
    from apps.main.showcase.views_public_design import PublicCompanyShowcaseOrderCreateAPIView
    return PublicCompanyShowcaseOrderCreateAPIView


class OrderDraftAPIView(_draft_view_base()):
    """
    POST /api/main/orders/draft/ — тот же заказ, что с витрины (резерв остатка, без оплаты),
    но от ключа/пользователя. Тело как у public/companies/{slug}/orders/ + source: voice_ai|telegram.
    Повтор с тем же Idempotency-Key возвращает тот же заказ.
    """
    authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES
    permission_classes = [permissions.IsAuthenticated, HasApiKeyScope]
    required_scope = ApiKey.Scope.ORDER_DRAFT

    def post(self, request):
        company = agent_company(request)
        data = request.data if isinstance(request.data, dict) else {}
        source = (data.get("source") or "voice_ai")
        if source not in DRAFT_SOURCES:
            raise ValidationError({"source": "Допустимо: voice_ai, telegram."})
        request._full_data = {**data, "source": source}
        resp = super().post(request, slug=company.slug)
        return resp


class OrderDraftStatusAPIView(AgentBase):
    """GET /api/main/orders/draft/status/?number=123 или ?phone=… — статус заказов агента."""
    required_scope = ApiKey.Scope.ORDER_DRAFT

    def get(self, request):
        company = agent_company(request)
        number = request.query_params.get("number")
        phone = phone_digits(request.query_params.get("phone"))
        if not number and not phone:
            raise ValidationError({"detail": "Нужен number или phone."})
        qs = ShowcaseOrder.objects.filter(company=company)
        if number:
            if not str(number).isdigit():
                raise ValidationError({"number": "Целое число."})
            qs = qs.filter(number=int(number))
        if phone:
            qs = qs.filter(customer_phone__contains=phone[-9:])
        rows = [
            {"number": o.number, "status": o.status, "total": str(o.total), "source": o.source,
             "created_at": o.created_at.isoformat()}
            for o in qs.order_by("-created_at")[:5]
        ]
        if not rows:
            return Response({"results": []}, status=status.HTTP_404_NOT_FOUND)
        return Response({"results": rows})


# ---------------------------------------------------------------- журнал звонков
class VoiceCallAPIView(AgentBase):
    """POST/GET /api/main/voice/calls/ — журнал звонков голосового агента."""
    required_scope = ApiKey.Scope.ORDER_DRAFT

    @staticmethod
    def _row(c):
        return {
            "id": str(c.id), "phone": c.phone, "client": str(c.client_id) if c.client_id else None,
            "duration_seconds": c.duration_seconds, "language": c.language, "outcome": c.outcome,
            "transcript": c.transcript, "recording_url": c.recording_url, "cost": str(c.cost),
            "callback_needed": c.callback_needed, "callback_done": c.callback_done,
            "created_at": c.created_at.isoformat(),
        }

    def post(self, request):
        company = agent_company(request)
        d = request.data if isinstance(request.data, dict) else {}
        key = (request.headers.get("Idempotency-Key") or d.get("idempotency_key") or "").strip()[:128] or None
        if key:
            prior = VoiceCall.objects.filter(company=company, idempotency_key=key).first()
            if prior:
                return Response(self._row(prior))
        try:
            duration = int(d.get("duration_seconds", d.get("duration", 0)) or 0)
            cost = Decimal(str(d.get("cost") or "0"))
        except Exception:
            raise ValidationError({"detail": "duration и cost — числа."})
        if duration < 0 or cost < 0:
            raise ValidationError({"detail": "Не может быть отрицательным."})
        phone = str(d.get("phone") or "")[:32]
        norm = normalize_phone_e164(phone)
        client = Client.objects.filter(company=company, phone_normalized=norm).first() if norm else None
        call = VoiceCall.objects.create(
            company=company, client=client, phone=phone, duration_seconds=duration,
            language=str(d.get("language") or "")[:8], outcome=str(d.get("outcome") or "")[:32],
            transcript=str(d.get("transcript") or ""), recording_url=str(d.get("recording_url") or "")[:500],
            cost=cost, callback_needed=bool(d.get("callback_needed")), idempotency_key=key,
        )
        return Response(self._row(call), status=status.HTTP_201_CREATED)

    def get(self, request):
        company = agent_company(request)
        qs = VoiceCall.objects.filter(company=company)
        qp = request.query_params
        if qp.get("callback_needed") in ("1", "true", "True"):
            qs = qs.filter(callback_needed=True, callback_done=False)
        if qp.get("phone"):
            qs = qs.filter(phone__contains=phone_digits(qp["phone"])[-9:])
        return Response({"results": [self._row(c) for c in qs[:200]]})
