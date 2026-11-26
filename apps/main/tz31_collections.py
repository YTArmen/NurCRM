"""ТЗ кассы 05.11 п.37 / Б-93: коллекции и лукбук витрины, сезон.

Сотрудники (владелец/админ, как правка вида витрины): /api/main/showcase/collections/ и /{id}/.
Публично: .../public/companies/<slug>/showcase/collections/ и /collections/<collection_slug>/
(товары — тем же сериализатором, что и список витрины; порядок задаёт владелец).
"""
import re
import uuid

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import Product, ShowcaseCollection, ShowcaseCollectionItem
from apps.main.showcase import design_schema as ds
from apps.main.showcase import entities, services as svc
from apps.main.showcase.design_schema import Bool, I18n, Int, Media, _err
from apps.main.showcase.entities import _DateTime
from apps.main.showcase.services import ShowcaseErrorMixin, resolve_public_company
from apps.main.showcase.views_design import EditorAPIView
from apps.main.showcase.views_public import PublicCompanyShowcaseAPIView

LANGS = ("ru", "ky", "en", "tr", "uz", "kk")  # ТЗ: ru/ky/en/tr/uz (kk оставлен, как в редакторе витрины)


class _I18n(I18n):
    def check(self, value, path, ctx):
        if isinstance(value, str):
            value = {"ru": value}
        if not isinstance(value, dict):
            _err(path, 'Ожидается текст по языкам: {"ru": "...", "ky": "..."}.')
        out = {}
        for lang, txt in value.items():
            if lang not in LANGS:
                _err(f"{path}.{lang}", f"Неизвестный язык. Допустимые: {', '.join(LANGS)}.")
            out[lang] = ds.clean_text(txt, f"{path}.{lang}", self.max_len)
        return out


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_PRODUCTS = 500
MAX_COLLECTIONS = 50

SPEC = {
    "title": _I18n(120),
    "description": _I18n(2000),
    "cover": Media(),
    "starts_at": _DateTime(),
    "ends_at": _DateTime(),
    "is_active": Bool(),
    "position": Int(0, 100000),
}
_READ_ONLY = {"id", "created_at", "updated_at", "cover_urls", "products_count", "product_ids"}


def public_collections_qs(company):
    now = timezone.now()
    return (
        ShowcaseCollection.objects.filter(company=company, is_active=True)
        .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now), Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .order_by("position", "created_at")
    )


def _clean(data, company, partial, instance=None) -> dict:
    if not isinstance(data, dict):
        _err(None, "Ожидается объект.")
    ctx = ds.Ctx(company)
    out = {}
    for key, value in data.items():
        if key in _READ_ONLY:
            continue
        if key == "slug":
            if not isinstance(value, str) or not SLUG_RE.match(value.strip().lower()):
                _err("slug", "Адрес: латиница, цифры и «-», до 64 символов.")
            out["slug"] = value.strip().lower()
        elif key == "season":
            if value is None:
                value = ""
            if not isinstance(value, str) or len(value.strip()) > 16:
                _err("season", "Сезон — короткая строка до 16 символов, например SS26.")
            out["season"] = ds.clean_text(value, "season", 16) if value.strip() else ""
        elif key == "products":
            out["products"] = _clean_products(value, company)
        elif key in SPEC:
            out[key] = SPEC[key].patch(getattr(instance, key, None) if key in ("title", "description") else None, value, key, ctx)
        else:
            _err(key, "Неизвестное поле.", "unknown_field")
    if not partial and not (out.get("title") or {}).get("ru"):
        _err("title", "Нужно название (ru).", "required")
    if "slug" in out:
        qs = ShowcaseCollection.objects.filter(company=company, slug=out["slug"])
        if instance is not None:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            _err("slug", "Такой адрес уже занят.", "slug_taken")
    starts = out.get("starts_at", getattr(instance, "starts_at", None))
    ends = out.get("ends_at", getattr(instance, "ends_at", None))
    if starts and ends and ends <= starts:
        _err("ends_at", "Конец показа должен быть позже начала.")
    return out


def _clean_products(value, company):
    if not isinstance(value, list):
        _err("products", "Ожидается список id товаров по порядку.")
    if len(value) > MAX_PRODUCTS:
        _err("products", f"Не больше {MAX_PRODUCTS} товаров.", "limit_exceeded")
    ids = []
    for i, x in enumerate(value):
        try:
            u = str(uuid.UUID(str(x)))
        except (ValueError, AttributeError, TypeError):
            _err(f"products[{i}]", "Ожидается UUID товара.")
        if u not in ids:
            ids.append(u)
    found = {str(x) for x in Product.objects.filter(company=company, id__in=ids).values_list("id", flat=True)}
    missing = [x for x in ids if x not in found]
    if missing:
        _err("products", f"Товар не найден в вашей компании: {missing[0]}", "product_not_found")
    return ids


_TR = dict(zip("абвгдезийклмнопрстуфхыэ", "abvgdezijklmnoprstufhye"))
_TR.update({"ё": "e", "ж": "zh", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ь": "", "ю": "yu", "я": "ya",
            "ө": "o", "ү": "u", "ң": "n"})


def _auto_slug(company, title):
    ru = "".join(_TR.get(ch, ch) for ch in ((title or {}).get("ru") or "").lower())
    base = slugify(ru)[:40] or "collection"
    slug, n = base, 2
    while ShowcaseCollection.objects.filter(company=company, slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _apply(c, data):
    products = data.pop("products", None)
    for key, value in data.items():
        if key == "cover":
            c.cover_id = value
        else:
            setattr(c, key, value)
    c.save()
    if products is not None:
        ShowcaseCollectionItem.objects.filter(collection=c).delete()
        ShowcaseCollectionItem.objects.bulk_create(
            [ShowcaseCollectionItem(collection=c, product_id=pid, position=i) for i, pid in enumerate(products)]
        )


def _iso(d):
    return d.isoformat() if d else None


def _i18n(v):
    v = v if isinstance(v, dict) else {}
    return {lang: v[lang] for lang in LANGS if v.get(lang)}


def _media_urls(c, media):
    return (media.get(str(c.cover_id)) or {}).get("urls") or {} if c.cover_id else {}


def staff_out(c, media):
    ids = [str(x) for x in c.items.order_by("position", "id").values_list("product_id", flat=True)]
    return {
        "id": str(c.id), "slug": c.slug, "title": _i18n(c.title), "description": _i18n(c.description),
        "season": c.season, "cover": str(c.cover_id) if c.cover_id else None, "cover_urls": _media_urls(c, media),
        "product_ids": ids, "products_count": len(ids), "is_active": c.is_active, "position": c.position,
        "starts_at": _iso(c.starts_at), "ends_at": _iso(c.ends_at),
        "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at),
    }


def public_out(c, media, products_count=None):
    d = {
        "id": str(c.id), "slug": c.slug, "title": _i18n(c.title), "description": _i18n(c.description),
        "season": c.season, "cover_urls": _media_urls(c, media),
    }
    if products_count is not None:
        d["products_count"] = products_count
    return d


class StaffCollectionListCreateAPIView(EditorAPIView):
    def get(self, request):
        company = self.company()
        rows = list(ShowcaseCollection.objects.filter(company=company).select_related("cover"))
        media = svc.media_map(company, [str(c.cover_id) for c in rows if c.cover_id])
        return Response([staff_out(c, media) for c in rows])

    @transaction.atomic
    def post(self, request):
        company = self.company()
        data = _clean(request.data, company, partial=False)
        if ShowcaseCollection.objects.filter(company=company).count() >= MAX_COLLECTIONS:
            _err(None, f"Не больше {MAX_COLLECTIONS} коллекций.", "limit_exceeded")
        data.setdefault("slug", _auto_slug(company, data.get("title")))
        if "position" not in data:
            last = ShowcaseCollection.objects.filter(company=company).order_by("-position").values_list("position", flat=True).first()
            data["position"] = (last + 1) if last is not None else 0
        c = ShowcaseCollection(company=company)
        _apply(c, data)
        return Response(staff_out(c, svc.media_map(company, [str(c.cover_id)] if c.cover_id else [])), status=status.HTTP_201_CREATED)


class StaffCollectionDetailAPIView(EditorAPIView):
    def _get(self, pk):
        return get_object_or_404(ShowcaseCollection, id=pk, company=self.company())

    def _out(self, c):
        return Response(staff_out(c, svc.media_map(c.company, [str(c.cover_id)] if c.cover_id else [])))

    def get(self, request, pk):
        return self._out(self._get(pk))

    @transaction.atomic
    def patch(self, request, pk):
        c = self._get(pk)
        _apply(c, _clean(request.data, c.company, partial=True, instance=c))
        return self._out(c)

    put = patch

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PublicCollectionListAPIView(ShowcaseErrorMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, slug):
        company = resolve_public_company(slug, request)
        rows = list(public_collections_qs(company).annotate(n=Count("items")))
        media = svc.media_map(company, [str(c.cover_id) for c in rows if c.cover_id])
        return Response([public_out(c, media, c.n) for c in rows])


class PublicCollectionDetailAPIView(PublicCompanyShowcaseAPIView):
    """Коллекция + её товары (пагинация и фильтры — как у списка витрины)."""

    def list(self, request, *args, **kwargs):
        resp = super().list(request, *args, **kwargs)
        c = self.get_collection()
        media = svc.media_map(c.company, [str(c.cover_id)] if c.cover_id else [])
        if isinstance(resp.data, dict):
            resp.data = {"collection": public_out(c, media), **resp.data}
        else:
            resp.data = {"collection": public_out(c, media), "results": resp.data}
        return resp
