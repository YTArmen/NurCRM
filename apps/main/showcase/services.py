"""
Сервисный слой редактора витрины (ТЗ-BE-2026-05).

Хранение (ответ на вопрос 11.1): один JSON-документ вида (ShowcaseDesign.draft / .published) +
таблицы для списочных сущностей (баннеры, блоки акций, страницы, настройки товаров и категорий).
Таблицы — это черновое состояние. publish() складывает документ и снимок таблиц в
ShowcaseDesign.published (+ ShowcaseDesignVersion.snapshot), публичные адреса читают только снимок.
discard()/restore() возвращают и документ, и таблицы — витрина полностью «как в версии n».
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions, status
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response

from apps.main.models import (
    Product,
    ProductCategory,
    ShowcaseBanner,
    ShowcaseCategorySettings,
    ShowcaseDesign,
    ShowcaseDesignVersion,
    ShowcaseMedia,
    ShowcasePage,
    ShowcasePreviewToken,
    ShowcaseProductSettings,
    ShowcasePromoBlock,
    ShowcaseSlugRedirect,
    _cart_item_promotion,
    _money,
    cart_line_base,
    scale_amount_step,
)
from apps.main.showcase import design_schema as ds
from apps.main.showcase.design_schema import ShowcaseFieldError
from apps.users.models import Company

PREVIEW_TTL = timedelta(hours=24)
PUBLIC_DESIGN_CACHE_TTL = 30  # сек; ключ включает версию → publish виден сразу


def public_base_url() -> str:
    return getattr(settings, "SHOWCASE_PUBLIC_BASE_URL", "https://market.nurcrm.kg").rstrip("/")


def storefront_url(slug: str) -> str:
    return f"{public_base_url()}/catalog/{slug}"


# ======================================================================
# Ошибки, права, тариф
# ======================================================================


def _first_error(data, path=""):
    if isinstance(data, dict):
        for k, v in data.items():
            if k in ("code",):
                continue
            sub = path if k in ("non_field_errors", "detail") else (f"{path}.{k}" if path else str(k))
            return _first_error(v, sub)
    if isinstance(data, list):
        for i, v in enumerate(data):
            if isinstance(v, (dict, list)):
                return _first_error(v, f"{path}[{i}]" if isinstance(v, dict) else path)
            return path, str(v), getattr(v, "code", None)
        return path, "Ошибка данных.", None
    return path, str(data), getattr(data, "code", None)


def normalize_error_payload(data, status_code: int):
    """Любая ошибка → {detail, code, field?} (п. 3.6); исходные ошибки по полям — в errors."""
    if isinstance(data, dict) and "detail" in data:
        out = dict(data)
        out["detail"] = str(out["detail"])
        if "code" not in out or not out["code"]:
            out["code"] = getattr(data["detail"], "code", None) or "error"
        out["code"] = str(out["code"])
        return out
    field_path, msg, code = _first_error(data)
    out = {"detail": msg, "code": str(code or ("invalid" if status_code == 400 else "error"))}
    if field_path:
        out["field"] = field_path
    out["errors"] = data
    return out


class SlugMoved(APIException):
    status_code = status.HTTP_301_MOVED_PERMANENTLY
    default_code = "slug_moved"

    def __init__(self, new_slug: str):
        self.new_slug = new_slug
        super().__init__(detail="Адрес витрины изменён.", code="slug_moved")


class ShowcaseErrorMixin:
    """Единый формат ошибок {detail, code, field?} и 301 для старого slug."""

    def handle_exception(self, exc):
        if isinstance(exc, SlugMoved):
            request = self.request
            old = self.kwargs.get("slug")
            path = request.get_full_path().replace(f"/companies/{old}/", f"/companies/{exc.new_slug}/", 1)
            resp = Response(
                {
                    "detail": "Адрес витрины изменён.",
                    "code": "slug_moved",
                    "slug": exc.new_slug,
                    "url": storefront_url(exc.new_slug),
                },
                status=status.HTTP_301_MOVED_PERMANENTLY,
            )
            resp["Location"] = request.build_absolute_uri(path)
            return resp
        response = super().handle_exception(exc)
        if response is not None and response.status_code >= 400 and isinstance(response.data, (dict, list)):
            response.data = normalize_error_payload(response.data, response.status_code)
        return response


def get_user_company(request) -> Company:
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        raise PermissionDenied("Требуется авторизация.")
    company = getattr(user, "owned_company", None) or getattr(user, "company", None)
    if not company:
        raise PermissionDenied("У пользователя не найдена компания.")
    return company


def is_owner_or_admin(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    if getattr(user, "role", None) in ("owner", "admin"):
        return True
    try:
        return bool(getattr(user, "owned_company", None))
    except Exception:
        return False


def _showcase_addon_on(company):
    # websvc2011: услугу «Онлайн-витрина» можно включить и платной функцией online-showcase
    # (Маркетплейс → заявка, промокод, «Функции» в support) — не только флагом компании.
    try:
        from apps.users.models import CompanyAddon
        a = CompanyAddon.objects.filter(company=company, code="online-showcase").first()
        return bool(a and a.is_effective())
    except Exception:
        return False


def check_feature(company):
    if not getattr(company, "can_view_showcase", False) and not _showcase_addon_on(company):
        raise PermissionDenied(
            detail="Редактор витрины входит в услугу «Онлайн витрина». Подключите её в тарифе.",
            code="feature_disabled",
        )


class IsShowcaseEditorPermission(permissions.BasePermission):
    """Владелец или администратор компании (3.4) + услуга «Онлайн витрина» (3.5)."""

    message = "Менять вид витрины может только владелец или администратор компании."
    code = "permission_denied"

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False
        if not is_owner_or_admin(user):
            return False
        company = getattr(user, "owned_company", None) or getattr(user, "company", None)
        if company is not None:
            check_feature(company)
        return True


class IsShowcaseStaffPermission(permissions.BasePermission):
    """Заказы витрины: любой сотрудник компании (касса и программа владельца)."""

    message = "Доступно только сотрудникам компании."
    code = "permission_denied"

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated)


def resolve_public_company(slug: str, request=None, *, redirect_get: bool = True) -> Company:
    """
    Компания по slug. Старый slug (смена ≤ 90 дней назад): для GET — 301 на новый адрес
    (тело {code: "slug_moved", slug, url}), для POST — заказ/событие принимаются как есть.
    """
    from rest_framework.exceptions import NotFound

    company = Company.objects.filter(slug=slug).first()
    if company is not None:
        return company
    redirect = (
        ShowcaseSlugRedirect.objects.filter(old_slug__iexact=slug, expires_at__gt=timezone.now())
        .select_related("company")
        .first()
    )
    if redirect is None:
        raise NotFound("Компания не найдена")
    if redirect_get and (request is None or request.method in ("GET", "HEAD")):
        raise SlugMoved(redirect.company.slug)
    return redirect.company


# ======================================================================
# Документ, апгрейд старого формата
# ======================================================================


def _i18n(json_value, legacy: str = "") -> dict:
    if isinstance(json_value, dict) and json_value:
        return dict(json_value)
    if legacy:
        return {"ru": legacy}
    return {}


def _ru(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("ru") or next((v for v in value.values() if v), "") or "")
    return str(value or "")


def _dt_iso(v):
    return v.isoformat() if v else None


def _parse_dt(v):
    if not v:
        return None
    if hasattr(v, "isoformat"):
        return v
    try:
        return parse_datetime(str(v))
    except (ValueError, TypeError):
        return None


def get_design(company: Company) -> ShowcaseDesign:
    """ShowcaseDesign компании (создаётся с документом по умолчанию); апгрейд старого формата."""
    design = ShowcaseDesign.objects.filter(company=company).first()
    if design is None:
        doc = ds.default_document(company.name or "", company.phones_howcase)
        published = copy.deepcopy(doc)
        published.update({"banners": [], "promo_blocks": [], "pages": [], "catalog": {"products": {}, "categories": {}}})
        design, _ = ShowcaseDesign.objects.get_or_create(
            company=company, defaults={"draft": doc, "published": published, "version": 1}
        )
    if ds.is_legacy_document(design.draft) or ds.is_legacy_document(design.published):
        design = _upgrade_legacy(company, design)
    return design


@transaction.atomic
def _upgrade_legacy(company: Company, design: ShowcaseDesign) -> ShowcaseDesign:
    design = ShowcaseDesign.objects.select_for_update().get(pk=design.pk)
    if not (ds.is_legacy_document(design.draft) or ds.is_legacy_document(design.published)):
        return design
    legacy_draft_cat = ds.legacy_catalog(design.draft)
    legacy_pub_cat = ds.legacy_catalog(design.published)
    if legacy_draft_cat and not ShowcaseProductSettings.objects.filter(company=company).exists():
        _import_legacy_catalog(company, legacy_draft_cat)
    old_pub = design.published if isinstance(design.published, dict) else {}
    pub = ds.normalize_document(old_pub, company)
    pub["banners"] = [_normalize_banner_dict(b) for b in (old_pub.get("banners") or []) if isinstance(b, dict)]
    pub["promo_blocks"] = [_normalize_promo_dict(b) for b in (old_pub.get("promo_blocks") or []) if isinstance(b, dict)]
    pub["pages"] = []
    pub["catalog"] = _catalog_from_legacy(legacy_pub_cat)
    _set_published_product_state(company, pub["catalog"].get("products") or {})
    design.published = pub
    design.draft = ds.normalize_document(design.draft, company)
    design.save(update_fields=["draft", "published", "updated_at"])
    return design


def _import_legacy_catalog(company, cat: dict):
    products = set(
        str(x) for x in Product.objects.filter(
            company=company,
            id__in=[x for x in set(cat.get("hidden_products", []) + cat.get("pinned_products", []) + cat.get("product_order", [])) if _is_uuid(x)],
        ).values_list("id", flat=True)
    )
    rows: Dict[str, dict] = {}
    for pid in cat.get("hidden_products", []):
        if pid in products:
            rows.setdefault(pid, {})["hidden"] = True
    for pid in cat.get("pinned_products", []):
        if pid in products:
            rows.setdefault(pid, {})["pinned"] = True
    for i, pid in enumerate(cat.get("product_order", [])):
        if pid in products:
            rows.setdefault(pid, {})["sort_order"] = i + 1
    ShowcaseProductSettings.objects.bulk_create(
        [ShowcaseProductSettings(company=company, product_id=pid, **vals) for pid, vals in rows.items()],
        ignore_conflicts=True,
    )
    cats = set(
        str(x) for x in ProductCategory.objects.filter(
            company=company,
            id__in=[x for x in set(cat.get("hidden_categories", []) + cat.get("category_order", [])) if _is_uuid(x)],
        ).values_list("id", flat=True)
    )
    crow: Dict[str, dict] = {}
    for cid in cat.get("hidden_categories", []):
        if cid in cats:
            crow.setdefault(cid, {})["hidden"] = True
    for i, cid in enumerate(cat.get("category_order", [])):
        if cid in cats:
            crow.setdefault(cid, {})["sort_order"] = i + 1
    ShowcaseCategorySettings.objects.bulk_create(
        [ShowcaseCategorySettings(company=company, category_id=cid, **vals) for cid, vals in crow.items()],
        ignore_conflicts=True,
    )


def _set_published_product_state(company, prods: dict):
    valid = {
        str(x) for x in Product.objects.filter(company=company, id__in=[k for k in prods if _is_uuid(k)]).values_list("id", flat=True)
    }
    ShowcaseProductSettings.objects.filter(company=company).update(
        published_hidden=False, published_pinned=False, published_sort_order=None, published_badge=None
    )
    for pid, v in prods.items():
        if pid not in valid:
            continue
        vals = {
            "published_hidden": bool(v.get("hidden")), "published_pinned": bool(v.get("pinned")),
            "published_sort_order": v.get("sort_order"), "published_badge": v.get("badge") or None,
        }
        updated = ShowcaseProductSettings.objects.filter(company=company, product_id=pid).update(**vals)
        if not updated:
            ShowcaseProductSettings.objects.create(company=company, product_id=pid, **vals)


def _catalog_from_legacy(cat: dict) -> dict:
    products: Dict[str, dict] = {}
    for pid in cat.get("hidden_products", []):
        products.setdefault(pid, {})["hidden"] = True
    for pid in cat.get("pinned_products", []):
        products.setdefault(pid, {})["pinned"] = True
    for i, pid in enumerate(cat.get("product_order", [])):
        products.setdefault(pid, {})["sort_order"] = i + 1
    categories: Dict[str, dict] = {}
    for cid in cat.get("hidden_categories", []):
        categories.setdefault(cid, {})["hidden"] = True
    for i, cid in enumerate(cat.get("category_order", [])):
        categories.setdefault(cid, {})["sort_order"] = i + 1
    return {"products": products, "categories": categories}


def _is_uuid(v) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except (ValueError, TypeError):
        return False


def draft_document(company: Company, design: Optional[ShowcaseDesign] = None) -> dict:
    """Черновик для редактора: categories.order/hidden — из таблицы настроек категорий."""
    design = design or get_design(company)
    doc = ds.normalize_document(design.draft, company)
    cats = list(ShowcaseCategorySettings.objects.filter(company=company).values("category_id", "hidden", "sort_order"))
    doc["categories"]["hidden"] = [str(c["category_id"]) for c in cats if c["hidden"]]
    doc["categories"]["order"] = [
        str(c["category_id"]) for c in sorted((c for c in cats if c["sort_order"] is not None), key=lambda c: c["sort_order"])
    ]
    return doc


def published_document(company: Company, design: Optional[ShowcaseDesign] = None) -> dict:
    design = design or get_design(company)
    pub = design.published if isinstance(design.published, dict) else {}
    doc = ds.normalize_document({k: v for k, v in pub.items() if k in ds.DOCUMENT_SECTIONS}, company)
    cat = (pub.get("catalog") or {}).get("categories") or {}
    doc["categories"]["hidden"] = [cid for cid, c in cat.items() if c.get("hidden")]
    doc["categories"]["order"] = [
        cid for cid, c in sorted(((k, v) for k, v in cat.items() if v.get("sort_order") is not None), key=lambda kv: kv[1]["sort_order"])
    ]
    return doc


def get_shop_knowledge(company: Company, lang: str = "ru") -> str:
    """ТЗ 01.11: опубликованный текст design `shop.info.knowledge` для ИИ-ассистента. Нет языка — берём ru."""
    try:
        doc = published_document(company)
    except Exception:
        return ""
    texts = (((doc.get("shop") or {}).get("info") or {}).get("knowledge")) or {}
    if not isinstance(texts, dict):
        return ""
    return (texts.get(lang) or texts.get("ru") or "").strip()


def apply_category_lists_from_doc(company: Company, doc: dict, touched_categories: dict):
    """PATCH categories.order / categories.hidden в документе → таблица настроек категорий."""
    valid = {
        str(x) for x in ProductCategory.objects.filter(company=company).values_list("id", flat=True)
    }
    if "hidden" in touched_categories:
        hidden = [c for c in doc["categories"]["hidden"] if c in valid]
        ShowcaseCategorySettings.objects.filter(company=company).exclude(category_id__in=hidden).update(hidden=False)
        for cid in hidden:
            ShowcaseCategorySettings.objects.update_or_create(company=company, category_id=cid, defaults={"hidden": True})
    if "order" in touched_categories:
        set_category_order(company, [c for c in doc["categories"]["order"] if c in valid])


def save_draft(company: Company, design: ShowcaseDesign, doc: dict, touched_categories: Optional[dict] = None, user=None):
    if touched_categories:
        apply_category_lists_from_doc(company, doc, touched_categories)
    stored = copy.deepcopy(doc)
    stored["categories"]["order"] = []
    stored["categories"]["hidden"] = []
    design.draft = stored
    design.save(update_fields=["draft", "updated_at"])


# ======================================================================
# Сериализация списочных сущностей (снимок)
# ======================================================================


def banner_to_dict(b: ShowcaseBanner) -> dict:
    return {
        "id": str(b.id),
        "title": _i18n(b.title_i18n, b.title),
        "subtitle": _i18n(b.subtitle_i18n, b.subtitle),
        "button_text": _i18n(b.button_text),
        "image": str(b.image_id) if b.image_id else None,
        "image_mobile": str(b.image_mobile_id) if b.image_mobile_id else None,
        "link": b.link or None,
        "place": b.place,
        "inline_after_row": b.inline_after_row,
        "starts_at": _dt_iso(b.starts_at),
        "ends_at": _dt_iso(b.ends_at),
        "active": bool(b.active),
        "position": b.position,
    }


def _normalize_banner_dict(d: dict) -> dict:
    title = d.get("title")
    subtitle = d.get("subtitle")
    return {
        "id": str(d.get("id")),
        "title": title if isinstance(title, dict) else _i18n(None, title or ""),
        "subtitle": subtitle if isinstance(subtitle, dict) else _i18n(None, subtitle or ""),
        "button_text": d.get("button_text") if isinstance(d.get("button_text"), dict) else _i18n(None, d.get("button_text") or ""),
        "image": str(d["image"]) if d.get("image") else None,
        "image_mobile": str(d["image_mobile"]) if d.get("image_mobile") else None,
        "link": d.get("link") or None,
        "place": d.get("place") or "hero",
        "inline_after_row": d.get("inline_after_row"),
        "starts_at": d.get("starts_at"),
        "ends_at": d.get("ends_at"),
        "active": bool(d.get("active", True)),
        "position": int(d.get("position") or 0),
    }


def promo_to_dict(pb: ShowcasePromoBlock) -> dict:
    return {
        "id": str(pb.id),
        "title": _i18n(pb.title_i18n, pb.title),
        "source": pb.source or {"type": "promotions", "ids": []},
        "style": pb.style,
        "show_timer": bool(pb.show_timer),
        "max_items": pb.max_items,
        "position": pb.position,
        "active": bool(pb.active),
        "background": pb.background,
        "title_color": pb.title_color,
    }


def _normalize_promo_dict(d: dict) -> dict:
    title = d.get("title")
    return {
        "id": str(d.get("id")),
        "title": title if isinstance(title, dict) else _i18n(None, title or ""),
        "source": d.get("source") or {"type": "promotions", "ids": []},
        "style": d.get("style") or "carousel",
        "show_timer": bool(d.get("show_timer", True)),
        "max_items": int(d.get("max_items") or 12),
        "position": int(d.get("position") or 0),
        "active": bool(d.get("active", True)),
        "background": d.get("background"),
        "title_color": d.get("title_color"),
    }


def page_to_dict(p: ShowcasePage) -> dict:
    return {
        "id": str(p.id),
        "slug": p.slug,
        "title": p.title or {},
        "body": p.body or {},
        "show_in_footer": bool(p.show_in_footer),
        "position": p.position,
    }


def build_catalog(company: Company) -> dict:
    products = {}
    for row in ShowcaseProductSettings.objects.filter(company=company).values(
        "product_id", "hidden", "pinned", "sort_order", "badge"
    ):
        if not (row["hidden"] or row["pinned"] or row["sort_order"] is not None or row["badge"]):
            continue
        products[str(row["product_id"])] = {
            "hidden": row["hidden"], "pinned": row["pinned"], "sort_order": row["sort_order"], "badge": row["badge"],
        }
    categories = {}
    for row in ShowcaseCategorySettings.objects.filter(company=company).values(
        "category_id", "hidden", "sort_order", "image_id", "title_override"
    ):
        if not (row["hidden"] or row["sort_order"] is not None or row["image_id"] or row["title_override"]):
            continue
        categories[str(row["category_id"])] = {
            "hidden": row["hidden"],
            "sort_order": row["sort_order"],
            "image": str(row["image_id"]) if row["image_id"] else None,
            "title_override": row["title_override"] or None,
        }
    return {"products": products, "categories": categories}


def build_snapshot(company: Company, design: ShowcaseDesign) -> dict:
    snap = ds.normalize_document(design.draft, company)
    snap.pop("version", None)
    snap["banners"] = [banner_to_dict(b) for b in ShowcaseBanner.objects.filter(company=company).order_by("position", "created_at")]
    snap["promo_blocks"] = [promo_to_dict(b) for b in ShowcasePromoBlock.objects.filter(company=company).order_by("position", "created_at")]
    snap["pages"] = [page_to_dict(p) for p in ShowcasePage.objects.filter(company=company).order_by("position", "created_at")]
    snap["catalog"] = build_catalog(company)
    return snap


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _comparable(snap: dict) -> dict:
    out = {k: v for k, v in (snap or {}).items() if k != "version"}
    if isinstance(out.get("categories"), dict):
        out["categories"] = {k: v for k, v in out["categories"].items() if k not in ("order", "hidden")}
    for key in ("banners", "promo_blocks", "pages"):
        out.setdefault(key, [])
    out.setdefault("catalog", {"products": {}, "categories": {}})
    return out


def has_unpublished_changes(company: Company, design: ShowcaseDesign) -> bool:
    current = _comparable(build_snapshot(company, design))
    pub = design.published if isinstance(design.published, dict) else {}
    pub_doc = ds.normalize_document({k: v for k, v in pub.items() if k in ds.DOCUMENT_SECTIONS}, company)
    pub_full = dict(pub_doc)
    for key in ("banners", "promo_blocks", "pages", "catalog"):
        if key in pub:
            pub_full[key] = pub[key]
    return _canon(current) != _canon(_comparable(pub_full))


def _existing_media(company, ids: Iterable) -> set:
    ids = [i for i in ids if i and _is_uuid(i)]
    if not ids:
        return set()
    return {str(x) for x in ShowcaseMedia.objects.filter(company=company, id__in=ids).values_list("id", flat=True)}


@transaction.atomic
def restore_tables_from_snapshot(company: Company, snap: dict):
    """Таблицы черновика = снимок (discard / restore версии)."""
    snap = snap or {}
    banners = [_normalize_banner_dict(b) for b in snap.get("banners") or [] if isinstance(b, dict)]
    promos = [_normalize_promo_dict(b) for b in snap.get("promo_blocks") or [] if isinstance(b, dict)]
    pages = [p for p in snap.get("pages") or [] if isinstance(p, dict)]
    media_ok = _existing_media(company, [b["image"] for b in banners] + [b["image_mobile"] for b in banners])

    keep = [b["id"] for b in banners if _is_uuid(b["id"])]
    ShowcaseBanner.objects.filter(company=company).exclude(id__in=keep).delete()
    for b in banners:
        if not _is_uuid(b["id"]):
            continue
        ShowcaseBanner.objects.update_or_create(
            id=b["id"], company=company,
            defaults={
                "title": _ru(b["title"])[:255], "title_i18n": b["title"],
                "subtitle": _ru(b["subtitle"])[:255], "subtitle_i18n": b["subtitle"],
                "button_text": b["button_text"],
                "image_id": b["image"] if b["image"] in media_ok else None,
                "image_mobile_id": b["image_mobile"] if b["image_mobile"] in media_ok else None,
                "link": b["link"] or {}, "place": b["place"], "inline_after_row": b["inline_after_row"],
                "starts_at": _parse_dt(b["starts_at"]), "ends_at": _parse_dt(b["ends_at"]),
                "active": b["active"], "position": b["position"],
            },
        )

    keep = [b["id"] for b in promos if _is_uuid(b["id"])]
    ShowcasePromoBlock.objects.filter(company=company).exclude(id__in=keep).delete()
    for b in promos:
        if not _is_uuid(b["id"]):
            continue
        ShowcasePromoBlock.objects.update_or_create(
            id=b["id"], company=company,
            defaults={
                "title": _ru(b["title"])[:255], "title_i18n": b["title"], "source": b["source"],
                "style": b["style"], "show_timer": b["show_timer"], "max_items": b["max_items"],
                "position": b["position"], "active": b["active"],
                "background": b["background"], "title_color": b["title_color"],
            },
        )

    keep = [p.get("id") for p in pages if _is_uuid(p.get("id"))]
    ShowcasePage.objects.filter(company=company).exclude(id__in=keep).delete()
    for p in pages:
        if not _is_uuid(p.get("id")):
            continue
        ShowcasePage.objects.filter(company=company, slug=p.get("slug")).exclude(id=p["id"]).delete()
        ShowcasePage.objects.update_or_create(
            id=p["id"], company=company,
            defaults={
                "slug": p.get("slug") or "page", "title": p.get("title") or {}, "body": p.get("body") or {},
                "show_in_footer": bool(p.get("show_in_footer", True)), "position": int(p.get("position") or 0),
            },
        )

    catalog = snap.get("catalog") or {}
    prods = catalog.get("products") or {}
    valid_p = {
        str(x) for x in Product.objects.filter(company=company, id__in=[k for k in prods if _is_uuid(k)]).values_list("id", flat=True)
    }
    # Только черновые поля; опубликованные (published_*) не трогаем — витрина меняется после publish.
    rows = {str(r.product_id): r for r in ShowcaseProductSettings.objects.filter(company=company)}
    to_update, to_create = [], []
    for pid, row in rows.items():
        v = prods.get(pid) or {}
        row.hidden, row.pinned = bool(v.get("hidden")), bool(v.get("pinned"))
        row.sort_order, row.badge = v.get("sort_order"), v.get("badge") or None
        to_update.append(row)
    for pid, v in prods.items():
        if pid in valid_p and pid not in rows:
            to_create.append(ShowcaseProductSettings(
                company=company, product_id=pid, hidden=bool(v.get("hidden")), pinned=bool(v.get("pinned")),
                sort_order=v.get("sort_order"), badge=v.get("badge") or None,
            ))
    if to_update:
        ShowcaseProductSettings.objects.bulk_update(to_update, ["hidden", "pinned", "sort_order", "badge"], batch_size=500)
    if to_create:
        ShowcaseProductSettings.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
    cats = catalog.get("categories") or {}
    valid_c = {
        str(x) for x in ProductCategory.objects.filter(company=company, id__in=[k for k in cats if _is_uuid(k)]).values_list("id", flat=True)
    }
    media_ok = _existing_media(company, [v.get("image") for v in cats.values()])
    ShowcaseCategorySettings.objects.filter(company=company).delete()
    ShowcaseCategorySettings.objects.bulk_create([
        ShowcaseCategorySettings(
            company=company, category_id=cid, hidden=bool(v.get("hidden")), sort_order=v.get("sort_order"),
            image_id=v.get("image") if v.get("image") in media_ok else None, title_override=v.get("title_override") or None,
        )
        for cid, v in cats.items() if cid in valid_c
    ])


def _doc_from_snapshot(company, snap: dict) -> dict:
    doc = ds.normalize_document({k: v for k, v in (snap or {}).items() if k in ds.DOCUMENT_SECTIONS}, company)
    doc["categories"]["order"] = []
    doc["categories"]["hidden"] = []
    return doc


def discard(company: Company) -> ShowcaseDesign:
    with transaction.atomic():
        design = ShowcaseDesign.objects.select_for_update().get(pk=get_design(company).pk)
        pub = design.published if isinstance(design.published, dict) else {}
        design.draft = _doc_from_snapshot(company, pub)
        design.save(update_fields=["draft", "updated_at"])
        restore_tables_from_snapshot(company, pub)
    return design


def restore_version(company: Company, version: int) -> Optional[ShowcaseDesign]:
    ver = ShowcaseDesignVersion.objects.filter(company=company, version=version).first()
    if ver is None:
        return None
    snap = ver.snapshot or {}
    if ds.is_legacy_document(snap):
        legacy = dict(snap)
        snap = ds.normalize_document(legacy, company)
        snap["banners"] = legacy.get("banners") or []
        snap["promo_blocks"] = legacy.get("promo_blocks") or []
        snap["catalog"] = _catalog_from_legacy(ds.legacy_catalog(legacy))
    with transaction.atomic():
        design = ShowcaseDesign.objects.select_for_update().get(pk=get_design(company).pk)
        design.draft = _doc_from_snapshot(company, snap)
        design.save(update_fields=["draft", "updated_at"])
        restore_tables_from_snapshot(company, snap)
    return design


def publish(company: Company, user, idempotency_key: str = "") -> dict:
    idem_cache_key = f"showcase_publish:{company.id}:{idempotency_key}" if idempotency_key else None
    if idempotency_key:
        prev = ShowcaseDesignVersion.objects.filter(company=company, idempotency_key=idempotency_key).first()
        if prev is not None:
            return {"version": prev.version, "published_at": prev.published_at.isoformat(), "idempotent_replay": True}
        cached = cache.get(idem_cache_key)
        if cached:
            return dict(cached, idempotent_replay=True)
    get_design(company)
    with transaction.atomic():
        design = ShowcaseDesign.objects.select_for_update().get(company=company)
        if idempotency_key:
            prev = ShowcaseDesignVersion.objects.filter(company=company, idempotency_key=idempotency_key).first()
            if prev is not None:
                return {"version": prev.version, "published_at": prev.published_at.isoformat(), "idempotent_replay": True}
        snap = build_snapshot(company, design)
        now = timezone.now()
        last = ShowcaseDesignVersion.objects.filter(company=company).order_by("-version").values_list("version", flat=True).first()
        new_version = max(design.version, last or 0) + 1
        ShowcaseProductSettings.objects.filter(company=company).update(
            published_hidden=F("hidden"), published_pinned=F("pinned"),
            published_sort_order=F("sort_order"), published_badge=F("badge"),
        )
        design.published = snap
        design.version = new_version
        design.published_at = now
        design.save(update_fields=["published", "version", "published_at", "updated_at"])
        ShowcaseDesignVersion.objects.create(
            company=company, version=new_version, snapshot=copy.deepcopy(snap),
            author=user if getattr(user, "is_authenticated", False) else None, published_at=now,
            idempotency_key=idempotency_key or None,
        )
        keep = ds.get_limits()["versions"]
        excess = list(
            ShowcaseDesignVersion.objects.filter(company=company).order_by("-version").values_list("id", flat=True)[keep:]
        )
        if excess:
            ShowcaseDesignVersion.objects.filter(id__in=excess).delete()
    result = {"version": new_version, "published_at": now.isoformat()}
    if idem_cache_key:
        cache.set(idem_cache_key, result, 86400 * 7)
    invalidate_public_cache(company)
    return result


def invalidate_public_cache(company: Company):
    with _CATALOG_LOCK:
        for key in [k for k in _CATALOG_CACHE if k[0] == str(company.id)]:
            _CATALOG_CACHE.pop(key, None)


# ======================================================================
# Предпросмотр
# ======================================================================


def create_preview_link(company: Company) -> dict:
    token = secrets.token_urlsafe(24)
    expires = timezone.now() + PREVIEW_TTL
    ShowcasePreviewToken.objects.create(token=token, company=company, expires_at=expires)
    ShowcasePreviewToken.objects.filter(expires_at__lt=timezone.now() - timedelta(days=7)).delete()
    return {"url": f"{storefront_url(company.slug)}?preview={token}", "token": token, "expires_at": expires.isoformat()}


class PreviewError(APIException):
    pass


def check_preview_token(company: Company, token: str):
    row = ShowcasePreviewToken.objects.filter(token=token, company=company).first()
    if row is None:
        exc = PreviewError(detail={"detail": "Ссылка предпросмотра недействительна.", "code": "invalid_preview_token"})
        exc.status_code = status.HTTP_404_NOT_FOUND
        raise exc
    if row.expires_at <= timezone.now():
        exc = PreviewError(detail={"detail": "Срок действия ссылки предпросмотра истёк (24 ч). Создайте новую.", "code": "preview_expired"})
        exc.status_code = status.HTTP_410_GONE
        raise exc
    return row


# ======================================================================
# Каталог для публичных адресов (скрытые/закреплённые/порядок/бейджи)
# ======================================================================


@dataclass
class PublicCatalog:
    hidden_products: set = field(default_factory=set)
    hidden_categories: set = field(default_factory=set)
    pinned: List[str] = field(default_factory=list)
    order: Dict[str, int] = field(default_factory=dict)
    badges: Dict[str, str] = field(default_factory=dict)
    categories: Dict[str, dict] = field(default_factory=dict)
    new_badge_days: int = 14
    default_sort: str = "default"
    hide_out_of_stock: bool = False
    hide_zero_price: bool = False
    preview: bool = False

    @property
    def field_prefix(self) -> str:
        """Поля ShowcaseProductSettings: черновые (предпросмотр) или опубликованные."""
        return "" if self.preview else "published_"


_CATALOG_CACHE: Dict[Tuple[str, Any], PublicCatalog] = {}
_CATALOG_LOCK = threading.Lock()


def _catalog_obj(company, doc: dict, catalog: dict) -> PublicCatalog:
    prods = catalog.get("products") or {}
    cats = catalog.get("categories") or {}
    hidden_cats = {cid for cid, v in cats.items() if v.get("hidden") and _is_uuid(cid)}
    if hidden_cats:
        roots = ProductCategory.objects.filter(company=company, id__in=hidden_cats)
        try:
            hidden_cats = {str(x) for x in ProductCategory.objects.get_queryset_descendants(roots, include_self=True).values_list("id", flat=True)}
        except Exception:
            pass
    products_cfg = doc.get("products") or {}
    card_cfg = doc.get("card") or {}
    pinned = [pid for pid, v in prods.items() if v.get("pinned")]
    return PublicCatalog(
        hidden_products={pid for pid, v in prods.items() if v.get("hidden")},
        hidden_categories=hidden_cats,
        pinned=pinned,
        order={pid: v["sort_order"] for pid, v in prods.items() if v.get("sort_order") is not None},
        badges={pid: v["badge"] for pid, v in prods.items() if v.get("badge")},
        categories=cats,
        new_badge_days=int(card_cfg.get("new_badge_days", 14) if card_cfg.get("new_badge_days") is not None else 14),
        default_sort=products_cfg.get("default_sort") or "default",
        hide_out_of_stock=bool(products_cfg.get("hide_out_of_stock")),
        hide_zero_price=bool(products_cfg.get("hide_zero_price")),
    )


def get_public_catalog(company: Company, preview_token: Optional[str] = None) -> PublicCatalog:
    if preview_token:
        check_preview_token(company, preview_token)
        design = get_design(company)
        obj = _catalog_obj(company, ds.normalize_document(design.draft, company), build_catalog(company))
        obj.preview = True
        return obj
    row = ShowcaseDesign.objects.filter(company=company).values_list("id", "version", "published_at").first()
    if row is None:
        doc = ds.default_document(company.name or "")
        return _catalog_obj(company, doc, {})
    key = (str(company.id), (row[1], row[2]))
    with _CATALOG_LOCK:
        hit = _CATALOG_CACHE.get(key)
    if hit is not None:
        return hit
    design = get_design(company)
    pub = design.published if isinstance(design.published, dict) else {}
    doc = ds.normalize_document({k: v for k, v in pub.items() if k in ds.DOCUMENT_SECTIONS}, company)
    obj = _catalog_obj(company, doc, pub.get("catalog") or _catalog_from_legacy(ds.legacy_catalog(pub)))
    with _CATALOG_LOCK:
        if len(_CATALOG_CACHE) > 1000:
            _CATALOG_CACHE.clear()
        _CATALOG_CACHE[(str(company.id), (design.version, design.published_at))] = obj
    return obj


def apply_catalog_visibility(qs, catalog: PublicCatalog):
    qs = qs.exclude(**{f"showcase_settings__{catalog.field_prefix}hidden": True})
    if catalog.hidden_categories:
        qs = qs.exclude(category_id__in=list(catalog.hidden_categories))
    return qs


# ======================================================================
# Цены как в кассе (Cart.recalc: ступени акции Product.stock + ProductPromotionTier)
# ======================================================================


def kassa_line(product, unit_price, qty, company) -> Tuple[Decimal, Decimal, Decimal, Any]:
    """(сумма строки, скидка по акции, к оплате, ступень) — так же, как касса считает строку чека."""
    unit_price = Decimal(str(unit_price or 0))
    qty = Decimal(str(qty or 0))
    item = SimpleNamespace(unit_price=unit_price, quantity=qty, product=product)
    base = cart_line_base(item, scale_amount_step(company))
    promo, tier = _cart_item_promotion(product, unit_price, qty)
    disc = min(max(Decimal(str(promo or 0)), Decimal("0")), base)
    return _money(base), _money(disc), _money(base - disc), (tier if disc > 0 else None)


def product_promo_info(product, company) -> dict:
    price = _money(Decimal(str(product.price or 0)))
    _, disc, net, tier = kassa_line(product, price, Decimal("1"), company)
    tiers = sorted(product.promotion_tiers.all(), key=lambda t: (t.min_amount or 0, t.position)) if product.stock else []
    best = max((t.discount_percent or Decimal("0") for t in tiers), default=Decimal("0"))
    return {
        "price": str(price),
        "final_price": str(net),
        "old_price": str(price) if net < price else None,
        "discount_percent": str(best.normalize()) if best > 0 else None,
        "best_price": str(_money(price * (Decimal("100") - best) / Decimal("100"))) if best > 0 else str(price),
        "tiers": [
            {
                "min_amount": str(t.min_amount),
                "discount_percent": str(t.discount_percent),
                "promo_quantity": t.promo_quantity,
            }
            for t in tiers
        ],
        "ends_at": None,  # у серверных акций кассы нет даты окончания
    }


# ======================================================================
# Медиа
# ======================================================================

MEDIA_WIDTHS = (1920, 1080, 640, 320)
SQUARE_SIZES = (512, 192, 32)
MAX_PIXELS = 40_000_000


class MediaError(ShowcaseFieldError):
    pass


def media_payload(media: ShowcaseMedia) -> dict:
    return {
        "id": str(media.id),
        "kind": media.kind,
        "urls": media.urls or {},
        "width": media.width,
        "height": media.height,
        "size": media.size,
        "content_type": media.content_type,
        "created_at": _dt_iso(media.created_at),
    }


def process_media_upload(company: Company, upload, kind: str, request, idempotency_key: str = "") -> ShowcaseMedia:
    from PIL import Image, ImageOps, UnidentifiedImageError

    limit_mb = ds.get_limits()["media_mb"]
    if kind not in ShowcaseMedia.Kind.values:
        raise MediaError("kind", f"Недопустимое значение. Допустимые: {', '.join(ShowcaseMedia.Kind.values)}.")
    if upload.size > limit_mb * 1024 * 1024:
        raise MediaError("file", f"Файл больше {limit_mb} МБ. Уменьшите картинку и загрузите снова.", "file_too_large")

    media_id = uuid.uuid4()
    base_path = f"showcase_media/{company.id}/{media_id.hex}"

    def url_of(name):
        url = default_storage.url(name)
        return request.build_absolute_uri(url) if request is not None and not url.startswith("http") else url

    upload.seek(0)
    head = upload.read(2048)
    upload.seek(0)
    name_low = (getattr(upload, "name", "") or "").lower()
    looks_svg = name_low.endswith(".svg") or b"<svg" in head.lower() or (getattr(upload, "content_type", "") or "") == "image/svg+xml"
    if looks_svg:
        from apps.main.showcase.sanitize import SvgError, sanitize_svg, svg_size

        if kind != ShowcaseMedia.Kind.LOGO:
            raise MediaError("file", "SVG можно загрузить только для логотипа (kind=logo).", "invalid_image_format")
        try:
            clean = sanitize_svg(upload.read())
        except SvgError as exc:
            raise MediaError("file", str(exc), "invalid_image")
        w, h = svg_size(clean)
        saved = default_storage.save(f"{base_path}.svg", ContentFile(clean))
        u = url_of(saved)
        urls = {"original": u, **{str(x): u for x in MEDIA_WIDTHS}, **{f"square_{x}": u for x in SQUARE_SIZES}}
        return ShowcaseMedia.objects.create(
            id=media_id, company=company, kind=kind, file=saved, urls=urls, width=w, height=h,
            size=len(clean), content_type="image/svg+xml", idempotency_key=idempotency_key or None,
        )

    try:
        img = Image.open(upload)
        fmt = (img.format or "").upper()
        if fmt not in ("JPEG", "PNG", "WEBP"):
            raise MediaError("file", "Поддерживаются JPG, PNG и WebP (SVG — только для логотипа).", "invalid_image_format")
        if img.size[0] * img.size[1] > MAX_PIXELS:
            raise MediaError("file", "Слишком большое разрешение картинки (больше 40 Мп).", "image_too_large")
        img.load()
        img = ImageOps.exif_transpose(img)
    except MediaError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError, SyntaxError):
        raise MediaError("file", "Файл не является картинкой JPG, PNG или WebP.", "invalid_image")

    width, height = img.size
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if ("A" in img.mode or "transparency" in img.info) else "RGB")

    urls: Dict[str, str] = {}
    upload.seek(0)
    ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[fmt]
    saved_main = default_storage.save(f"{base_path}.{ext}", upload)
    urls["original"] = url_of(saved_main)

    for w in MEDIA_WIDTHS:
        if width > w:
            h = max(1, int(round(height * w / float(width))))
            resized = img.resize((w, h), Image.Resampling.LANCZOS)
        else:
            resized = img
        buf = io.BytesIO()
        resized.save(buf, format="WEBP", quality=85, method=4)
        saved = default_storage.save(f"{base_path}_{w}.webp", ContentFile(buf.getvalue()))
        urls[str(w)] = url_of(saved)

    if kind in (ShowcaseMedia.Kind.LOGO, ShowcaseMedia.Kind.FAVICON):
        side = max(width, height)
        square = Image.new("RGBA", (side, side), (255, 255, 255, 0))
        square.paste(img.convert("RGBA"), ((side - width) // 2, (side - height) // 2))
        for s in SQUARE_SIZES:
            buf = io.BytesIO()
            square.resize((s, s), Image.Resampling.LANCZOS).save(buf, format="WEBP", quality=90, method=4)
            saved = default_storage.save(f"{base_path}_sq{s}.webp", ContentFile(buf.getvalue()))
            urls[f"square_{s}"] = url_of(saved)

    return ShowcaseMedia.objects.create(
        id=media_id, company=company, kind=kind, file=saved_main, urls=urls, width=width, height=height,
        size=upload.size, content_type=f"image/{'jpeg' if ext == 'jpg' else ext}",
        idempotency_key=idempotency_key or None,
    )


def delete_media_files(media: ShowcaseMedia):
    base_path = f"showcase_media/{media.company_id}/{media.id.hex}"
    names = [f"{base_path}_{w}.webp" for w in MEDIA_WIDTHS + (1920,)] + [f"{base_path}_sq{s}.webp" for s in SQUARE_SIZES]
    try:
        if media.file:
            media.file.delete(save=False)
        for n in set(names):
            if default_storage.exists(n):
                default_storage.delete(n)
    except Exception:
        pass


def media_usage(company: Company, media_id: str) -> Optional[str]:
    """'published' | 'draft' | None — где используется картинка."""
    design = ShowcaseDesign.objects.filter(company=company).first()
    if design is not None and media_id in ds.collect_media_ids(design.published):
        return "published"
    if design is not None and media_id in ds.collect_media_ids(design.draft):
        return "draft"
    if ShowcaseBanner.objects.filter(company=company).filter(Q(image_id=media_id) | Q(image_mobile_id=media_id)).exists():
        return "draft"
    if ShowcaseCategorySettings.objects.filter(company=company, image_id=media_id).exists():
        return "draft"
    return None


def media_map(company: Company, ids: Iterable[str]) -> Dict[str, dict]:
    ids = [i for i in set(ids) if _is_uuid(i)]
    if not ids:
        return {}
    return {
        str(m.id): {"urls": m.urls or {}, "width": m.width, "height": m.height, "kind": m.kind}
        for m in ShowcaseMedia.objects.filter(company=company, id__in=ids)
    }


# ======================================================================
# Ручной порядок товаров / категорий
# ======================================================================


def _ordered_valid(model, company, ids) -> List[str]:
    clean = []
    for x in ids:
        if _is_uuid(x):
            s = str(uuid.UUID(str(x)))
            if s not in clean:
                clean.append(s)
    valid = {str(v) for v in model.objects.filter(company=company, id__in=clean).values_list("id", flat=True)}
    return [x for x in clean if x in valid]


@transaction.atomic
def set_product_order(company: Company, ids: List[str]) -> List[str]:
    """Ручной порядок: перечисленные товары 1..N, остальные (и новые) — в конце."""
    order = _ordered_valid(Product, company, ids)
    ShowcaseProductSettings.objects.filter(company=company).exclude(product_id__in=order).update(sort_order=None)
    existing = {str(r.product_id): r for r in ShowcaseProductSettings.objects.filter(company=company, product_id__in=order)}
    to_update, to_create = [], []
    for i, pid in enumerate(order, start=1):
        row = existing.get(pid)
        if row is None:
            to_create.append(ShowcaseProductSettings(company=company, product_id=pid, sort_order=i))
        elif row.sort_order != i:
            row.sort_order = i
            to_update.append(row)
    if to_update:
        ShowcaseProductSettings.objects.bulk_update(to_update, ["sort_order"], batch_size=500)
    if to_create:
        ShowcaseProductSettings.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
    return order


@transaction.atomic
def set_category_order(company: Company, ids: List[str]) -> List[str]:
    order = _ordered_valid(ProductCategory, company, ids)
    ShowcaseCategorySettings.objects.filter(company=company).exclude(category_id__in=order).update(sort_order=None)
    existing = {str(r.category_id): r for r in ShowcaseCategorySettings.objects.filter(company=company, category_id__in=order)}
    to_update, to_create = [], []
    for i, cid in enumerate(order, start=1):
        row = existing.get(cid)
        if row is None:
            to_create.append(ShowcaseCategorySettings(company=company, category_id=cid, sort_order=i))
        elif row.sort_order != i:
            row.sort_order = i
            to_update.append(row)
    if to_update:
        ShowcaseCategorySettings.objects.bulk_update(to_update, ["sort_order"])
    if to_create:
        ShowcaseCategorySettings.objects.bulk_create(to_create, ignore_conflicts=True)
    return order


@transaction.atomic
def apply_legacy_catalog_patch(company: Company, cat: dict):
    """Старый PATCH layout.hidden_products / pinned_products / product_order / hidden_categories / category_order."""
    if "hidden_products" in cat:
        ids = _ordered_valid(Product, company, cat["hidden_products"])
        ShowcaseProductSettings.objects.filter(company=company).exclude(product_id__in=ids).update(hidden=False)
        for pid in ids:
            ShowcaseProductSettings.objects.update_or_create(company=company, product_id=pid, defaults={"hidden": True})
    if "pinned_products" in cat:
        ids = _ordered_valid(Product, company, cat["pinned_products"])
        ShowcaseProductSettings.objects.filter(company=company).exclude(product_id__in=ids).update(pinned=False)
        for pid in ids:
            ShowcaseProductSettings.objects.update_or_create(company=company, product_id=pid, defaults={"pinned": True})
    if "product_order" in cat:
        set_product_order(company, cat["product_order"])
    if "hidden_categories" in cat:
        ids = _ordered_valid(ProductCategory, company, cat["hidden_categories"])
        ShowcaseCategorySettings.objects.filter(company=company).exclude(category_id__in=ids).update(hidden=False)
        for cid in ids:
            ShowcaseCategorySettings.objects.update_or_create(company=company, category_id=cid, defaults={"hidden": True})
    if "category_order" in cat:
        set_category_order(company, cat["category_order"])
