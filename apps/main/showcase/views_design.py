"""
Редактор онлайн-витрины — эндпоинты программы владельца (ТЗ-BE-2026-05, п. 6.1–6.11, 6.12 (заказы), 6.14).

Все адреса: владелец или администратор компании (3.4) + услуга «Онлайн витрина» (3.5,
403 {code: "feature_disabled"}). Исключение — заказы витрины: их видит и ведёт любой сотрудник (касса).
Ошибки: {detail, code, field?} (3.6).
"""
from __future__ import annotations

import copy
from datetime import timedelta
from typing import Dict

from django.db import transaction
from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.main.models import (
    Product,
    ProductCategory,
    ShowcaseBanner,
    ShowcaseCategorySettings,
    ShowcaseDesign,
    ShowcaseDesignVersion,
    ShowcaseMedia,
    ShowcaseOrder,
    ShowcaseStockRequest,
    ShowcaseOrderItem,
    ShowcasePage,
    ShowcaseProductSettings,
    ShowcasePromoBlock,
    ShowcaseSlugRedirect,
    ShowcaseStats,
)
from apps.main.showcase import design_schema as ds
from apps.main.showcase import entities
from apps.main.showcase import services as svc
from apps.main.showcase.design_schema import ShowcaseFieldError
from apps.main.showcase.serializers_design import ShowcaseDesignVersionSerializer, ShowcaseOrderSerializer, ShowcaseStockRequestSerializer
from apps.main.showcase.services import (
    IsShowcaseEditorPermission,
    IsShowcaseStaffPermission,
    ShowcaseErrorMixin,
    get_user_company,
)
from apps.main.variant_utils import order_stock_lines, release_stock

# Обратная совместимость импортов (urls.py и старый код).
from apps.main.showcase.views_public_design import (  # noqa: F401
    PublicCompanyShowcaseDesignAPIView,
    PublicCompanyShowcaseOrderCreateAPIView,
    PublicCompanyShowcaseTrackAPIView,
)

_get_user_company = get_user_company
_is_owner_or_admin = svc.is_owner_or_admin


class EditorAPIView(ShowcaseErrorMixin, APIView):
    permission_classes = [IsShowcaseEditorPermission]

    def company(self):
        return get_user_company(self.request)


def _idem_key(request) -> str:
    key = request.headers.get("Idempotency-Key") or ""
    if not key:
        try:
            key = request.data.get("idempotency_key") or ""
        except Exception:
            key = ""
    return str(key).strip()[:128]


def _draft_response(company, design, warnings=None, extra=None):
    doc = svc.draft_document(company, design)
    data = {
        "draft": doc,
        "warnings": warnings if warnings is not None else ds.contrast_warnings(doc),
        "has_unpublished_changes": svc.has_unpublished_changes(company, design),
    }
    if extra:
        data.update(extra)
    return Response(data)


# ======================================================================
# 6.1 Справочники редактора
# ======================================================================


class ShowcaseEditorOptionsAPIView(EditorAPIView):
    def get(self, request):
        presets = []
        for p in ds.PRESETS:
            presets.append({**p, "preview": None})
        return Response({
            "presets": presets,
            "fonts": ds.FONTS,
            "section_types": list(ds.SECTION_TYPES),
            "limits": ds.get_limits(),
            "languages": list(ds.LANGUAGES),
            "sort_options": list(ds.DEFAULT_SORTS),
            "badges": ["hit", "sale", "new"],
            "media_kinds": list(ShowcaseMedia.Kind.values),
            "link_types": list(ds.LINK_TYPES),
            "variant_pickers": list(ds.VARIANT_PICKERS),  # ТЗ Н-33
            "defaults": ds.default_document(self.company().name or "", self.company().phones_howcase),
        })


# ======================================================================
# 6.2 Документ вида: чтение и черновик
# ======================================================================


class ShowcaseDesignAPIView(EditorAPIView):
    def get(self, request):
        company = self.company()
        design = svc.get_design(company)
        return Response({
            "draft": svc.draft_document(company, design),
            "published": svc.published_document(company, design),
            "version": design.version,
            "published_at": design.published_at.isoformat() if design.published_at else None,
            "updated_at": design.updated_at.isoformat() if design.updated_at else None,
            "has_unpublished_changes": svc.has_unpublished_changes(company, design),
            "storefront_url": svc.storefront_url(company.slug),
        })


class ShowcaseDraftUpdateAPIView(EditorAPIView):
    parser_classes = [JSONParser]

    def patch(self, request):
        company = self.company()
        data = request.data
        if not isinstance(data, dict):
            raise ShowcaseFieldError(None, "Ожидается объект с настройками черновика.")
        with transaction.atomic():
            design = ShowcaseDesign.objects.select_for_update().get(pk=svc.get_design(company).pk)
            current = svc.draft_document(company, design)
            if ds.is_legacy_document(data):
                legacy_cat = ds.legacy_catalog(data)
                data = ds.legacy_patch_to_new(data, current)
            else:
                legacy_cat = {}
            new = ds.apply_patch(current, data, company)
            touched = data.get("categories") if isinstance(data.get("categories"), dict) else None
            svc.save_draft(company, design, new, touched)
            if legacy_cat:
                svc.apply_legacy_catalog_patch(company, legacy_cat)
        return _draft_response(company, design, ds.contrast_warnings(new))

    def put(self, request):
        company = self.company()
        with transaction.atomic():
            design = ShowcaseDesign.objects.select_for_update().get(pk=svc.get_design(company).pk)
            new = ds.replace_document(request.data, company)
            touched = request.data.get("categories") if isinstance(request.data.get("categories"), dict) else None
            svc.save_draft(company, design, new, touched)
        return _draft_response(company, design, ds.contrast_warnings(new))


class ShowcaseDraftResetAPIView(EditorAPIView):
    def post(self, request):
        company = self.company()
        section = (request.data or {}).get("section") or "all"
        if section != "all" and section not in ds.DOCUMENT_SECTIONS:
            raise ShowcaseFieldError("section", f"Недопустимый раздел. Допустимые: all, {', '.join(ds.DOCUMENT_SECTIONS)}.")
        defaults = ds.default_document(company.name or "", company.phones_howcase)
        with transaction.atomic():
            design = ShowcaseDesign.objects.select_for_update().get(pk=svc.get_design(company).pk)
            current = svc.draft_document(company, design)
            if section == "all":
                new = defaults
                touched = {"order": [], "hidden": []}
            else:
                new = copy.deepcopy(current)
                new[section] = defaults[section]
                touched = {"order": [], "hidden": []} if section == "categories" else None
            svc.save_draft(company, design, new, touched)
        return _draft_response(company, design)


class ShowcaseDraftApplyPresetAPIView(EditorAPIView):
    def post(self, request):
        company = self.company()
        code = (request.data or {}).get("preset")
        preset = ds.get_preset(code) if isinstance(code, str) else None
        if preset is None:
            raise ShowcaseFieldError("preset", f"Неизвестный пресет. Допустимые: {', '.join(ds.PRESET_CODES)}.")
        with transaction.atomic():
            design = ShowcaseDesign.objects.select_for_update().get(pk=svc.get_design(company).pk)
            new = svc.draft_document(company, design)
            theme = copy.deepcopy(preset["theme"])
            theme["preset"] = preset["code"]
            new["theme"] = theme
            svc.save_draft(company, design, new)
        return _draft_response(company, design)


class ShowcaseDraftProductOrderAPIView(EditorAPIView):
    """Старый адрес (ТЗ-BE-2026-03): PATCH {"order": [...]} = POST /showcase/products/order/."""

    def patch(self, request):
        company = self.company()
        order_list = (request.data or {}).get("order")
        if not isinstance(order_list, list):
            raise ShowcaseFieldError("order", "Ожидается список ID товаров.")
        order = svc.set_product_order(company, order_list)
        design = svc.get_design(company)
        return Response({"draft": svc.draft_document(company, design), "order": order})


# ======================================================================
# 6.3 Публикация, предпросмотр, версии
# ======================================================================


class ShowcasePreviewLinkAPIView(EditorAPIView):
    def post(self, request):
        return Response(svc.create_preview_link(self.company()))


class ShowcasePublishAPIView(EditorAPIView):
    def post(self, request):
        company = self.company()
        result = svc.publish(company, request.user, _idem_key(request))
        result.pop("idempotent_replay", None)
        return Response(result, status=status.HTTP_200_OK)


class ShowcaseDiscardAPIView(EditorAPIView):
    def post(self, request):
        company = self.company()
        design = svc.discard(company)
        return _draft_response(company, design, extra={"detail": "Черновик сброшен к опубликованной версии."})


class ShowcaseVersionsListAPIView(EditorAPIView):
    def get(self, request):
        company = self.company()
        keep = ds.get_limits()["versions"]
        versions = ShowcaseDesignVersion.objects.filter(company=company).select_related("author").order_by("-version")[:keep]
        return Response(ShowcaseDesignVersionSerializer(versions, many=True).data)


class ShowcaseVersionRestoreAPIView(EditorAPIView):
    def post(self, request, version: int):
        company = self.company()
        design = svc.restore_version(company, version)
        if design is None:
            raise NotFound(detail=f"Версия {version} не найдена.", code="not_found")
        return _draft_response(
            company, design,
            extra={"detail": f"Версия {version} восстановлена в черновик. Опубликуйте, чтобы витрина изменилась."},
        )


# ======================================================================
# 6.10 Картинки
# ======================================================================


class ShowcaseMediaListCreateAPIView(EditorAPIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        company = self.company()
        qs = ShowcaseMedia.objects.filter(company=company).order_by("-created_at")
        kind = request.query_params.get("kind")
        if kind:
            if kind not in ShowcaseMedia.Kind.values:
                raise ShowcaseFieldError("kind", f"Недопустимое значение. Допустимые: {', '.join(ShowcaseMedia.Kind.values)}.")
            qs = qs.filter(kind=kind)
        return Response([svc.media_payload(m) for m in qs[:500]])

    def post(self, request):
        company = self.company()
        key = _idem_key(request)
        if key:
            existing = ShowcaseMedia.objects.filter(company=company, idempotency_key=key).first()
            if existing:
                return Response(svc.media_payload(existing), status=status.HTTP_200_OK)
        upload = request.FILES.get("file")
        if not upload:
            raise ShowcaseFieldError("file", "Файл не передан.", "required")
        kind = (request.data.get("kind") or ShowcaseMedia.Kind.OTHER).strip()
        media = svc.process_media_upload(company, upload, kind, request, key)
        return Response(svc.media_payload(media), status=status.HTTP_201_CREATED)


class ShowcaseMediaDetailAPIView(EditorAPIView):
    def get(self, request, pk):
        media = get_object_or_404(ShowcaseMedia, id=pk, company=self.company())
        return Response(svc.media_payload(media))

    def delete(self, request, pk):
        company = self.company()
        media = get_object_or_404(ShowcaseMedia, id=pk, company=company)
        usage = svc.media_usage(company, str(media.id))
        if usage:
            detail = (
                "Картинка используется в опубликованной витрине. Замените её и опубликуйте, потом удалите."
                if usage == "published"
                else "Картинка используется в черновике витрины. Уберите её из настроек, потом удалите."
            )
            return Response({"detail": detail, "code": "media_in_use", "used_in": usage}, status=status.HTTP_409_CONFLICT)
        svc.delete_media_files(media)
        media.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ======================================================================
# 6.4 Товары на витрине
# ======================================================================


def _settings_sub(company, field):
    return Subquery(
        ShowcaseProductSettings.objects.filter(company=company, product=OuterRef("pk")).values(field)[:1]
    )


def _bool_param(value):
    if value is None or value == "":
        return None
    v = str(value).lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    raise ShowcaseFieldError(None, "Ожидается true или false.")


def _product_row(p, request) -> dict:
    img = None
    imgs = [i for i in p.images.all() if getattr(i, "image", None)]
    if imgs:
        imgs.sort(key=lambda x: (not x.is_primary, x.created_at))
        img = request.build_absolute_uri(imgs[0].image.url)
    return {
        "product": str(p.id),
        "name": p.name,
        "category": str(p.category_id) if p.category_id else None,
        "category_name": p.category.name if p.category_id else None,
        "price": str(p.price or 0),
        "image_url": img,
        "hidden": bool(p.s_hidden),
        "pinned": bool(p.s_pinned),
        "sort_order": p.s_sort,
        "badge": p.s_badge,
    }


class ShowcaseProductsListAPIView(EditorAPIView):
    def get(self, request):
        company = self.company()
        qp = request.query_params
        qs = (
            Product.objects.filter(company=company)
            .select_related("category")
            .prefetch_related("images")
            .annotate(
                s_hidden=Coalesce(_settings_sub(company, "hidden"), Value(False)),
                s_pinned=Coalesce(_settings_sub(company, "pinned"), Value(False)),
                s_sort=_settings_sub(company, "sort_order"),
                s_badge=_settings_sub(company, "badge"),
            )
        )
        search = (qp.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(barcode__icontains=search) | Q(article__icontains=search))
        if qp.get("category"):
            if not svc._is_uuid(qp["category"]):
                raise ShowcaseFieldError("category", "Ожидается идентификатор категории.")
            qs = qs.filter(category_id=qp["category"])
        hidden = _bool_param(qp.get("hidden"))
        if hidden is not None:
            qs = qs.filter(s_hidden=hidden)
        pinned = _bool_param(qp.get("pinned"))
        if pinned is not None:
            qs = qs.filter(s_pinned=pinned)
        qs = qs.order_by(F("s_pinned").desc(), F("s_sort").asc(nulls_last=True), F("created_at").desc(), "id")
        try:
            page = max(1, int(qp.get("page") or 1))
            page_size = min(200, max(1, int(qp.get("page_size") or 50)))
        except ValueError:
            raise ShowcaseFieldError("page", "Ожидается число.")
        total = qs.count()
        rows = [_product_row(p, request) for p in qs[(page - 1) * page_size: page * page_size]]
        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "next": page + 1 if page * page_size < total else None,
            "previous": page - 1 if page > 1 else None,
            "results": rows,
        })


def _check_pinned_limit(company, adding: int):
    limit = ds.get_limits()["pinned_products"]
    if ShowcaseProductSettings.objects.filter(company=company, pinned=True).count() + adding > limit:
        raise ShowcaseFieldError("pinned", f"Закрепить можно не больше {limit} товаров.", "limit_exceeded")


class ShowcaseProductDetailAPIView(EditorAPIView):
    def patch(self, request, product_id):
        company = self.company()
        product = get_object_or_404(Product, id=product_id, company=company)
        data = entities.clean_product_settings(request.data, company)
        with transaction.atomic():
            row, _ = ShowcaseProductSettings.objects.select_for_update().get_or_create(company=company, product=product)
            if data.get("pinned") and not row.pinned:
                _check_pinned_limit(company, 1)
            for k, v in data.items():
                setattr(row, k, v)
            row.save()
        return Response({
            "product": str(product.id), "name": product.name, "hidden": row.hidden, "pinned": row.pinned,
            "sort_order": row.sort_order, "badge": row.badge,
        })


class ShowcaseProductsOrderAPIView(EditorAPIView):
    def post(self, request):
        company = self.company()
        order_list = (request.data or {}).get("order")
        if not isinstance(order_list, list):
            raise ShowcaseFieldError("order", "Ожидается список ID товаров.")
        if len(order_list) > 20000:
            raise ShowcaseFieldError("order", "Слишком длинный список.", "limit_exceeded")
        order = svc.set_product_order(company, order_list)
        return Response({"order": order, "count": len(order)})


class ShowcaseProductsBulkAPIView(EditorAPIView):
    def post(self, request):
        company = self.company()
        data = dict(request.data or {})
        ids = data.pop("ids", None)
        if not isinstance(ids, list) or not ids:
            raise ShowcaseFieldError("ids", "Ожидается непустой список ID товаров.", "required")
        if len(ids) > 5000:
            raise ShowcaseFieldError("ids", "Не больше 5000 товаров за раз.", "limit_exceeded")
        changes = entities.clean_product_settings(data, company)
        changes.pop("sort_order", None)
        if not changes:
            raise ShowcaseFieldError(None, "Укажите, что изменить: hidden, pinned или badge.", "required")
        valid = svc._ordered_valid(Product, company, ids)
        with transaction.atomic():
            if changes.get("pinned"):
                already = set(
                    str(x) for x in ShowcaseProductSettings.objects.filter(company=company, pinned=True, product_id__in=valid)
                    .values_list("product_id", flat=True)
                )
                _check_pinned_limit(company, len([v for v in valid if v not in already]))
            existing = {str(r.product_id): r for r in ShowcaseProductSettings.objects.filter(company=company, product_id__in=valid)}
            to_create = []
            for pid in valid:
                row = existing.get(pid)
                if row is None:
                    to_create.append(ShowcaseProductSettings(company=company, product_id=pid, **changes))
                else:
                    for k, v in changes.items():
                        setattr(row, k, v)
            if existing:
                ShowcaseProductSettings.objects.bulk_update(list(existing.values()), list(changes.keys()), batch_size=500)
            if to_create:
                ShowcaseProductSettings.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
        return Response({"updated": len(valid), "ids": valid, **changes})


# ======================================================================
# 6.5 Категории на витрине
# ======================================================================


def _category_row(c, settings_row, media) -> dict:
    image_id = str(settings_row.image_id) if settings_row and settings_row.image_id else None
    return {
        "category": str(c.id),
        "name": c.name,
        "parent": str(c.parent_id) if c.parent_id else None,
        "hidden": bool(settings_row.hidden) if settings_row else False,
        "sort_order": settings_row.sort_order if settings_row else None,
        "image": image_id,
        "image_urls": (media.get(image_id) or {}).get("urls") if image_id else None,
        "title_override": (settings_row.title_override or None) if settings_row else None,
        "products_count": c.products_count,
    }


class ShowcaseCategoriesListAPIView(EditorAPIView):
    def get(self, request):
        company = self.company()
        cats = list(
            ProductCategory.objects.filter(company=company).annotate(products_count=Count("product", distinct=True))
        )
        rows = {str(r.category_id): r for r in ShowcaseCategorySettings.objects.filter(company=company)}
        media = svc.media_map(company, [str(r.image_id) for r in rows.values() if r.image_id])
        out = [_category_row(c, rows.get(str(c.id)), media) for c in cats]
        out.sort(key=lambda r: (r["sort_order"] is None, r["sort_order"] or 0, r["name"].lower()))
        return Response(out)


class ShowcaseCategoryDetailAPIView(EditorAPIView):
    def patch(self, request, category_id):
        company = self.company()
        cat = get_object_or_404(
            ProductCategory.objects.annotate(products_count=Count("product", distinct=True)), id=category_id, company=company
        )
        data = entities.clean_category_settings(request.data, company)
        row, _ = ShowcaseCategorySettings.objects.get_or_create(company=company, category=cat)
        for k, v in data.items():
            if k == "image":
                row.image_id = v
            else:
                setattr(row, k, v)
        row.save()
        media = svc.media_map(company, [str(row.image_id)] if row.image_id else [])
        return Response(_category_row(cat, row, media))


class ShowcaseCategoriesOrderAPIView(EditorAPIView):
    def post(self, request):
        company = self.company()
        order_list = (request.data or {}).get("order")
        if not isinstance(order_list, list):
            raise ShowcaseFieldError("order", "Ожидается список ID категорий.")
        order = svc.set_category_order(company, order_list)
        return Response({"order": order, "count": len(order)})


# ======================================================================
# 6.6 Баннеры (черновое состояние; публично — после publish)
# ======================================================================


def _banner_out(b: ShowcaseBanner, media: Dict[str, dict]) -> dict:
    d = svc.banner_to_dict(b)
    d["image_urls"] = (media.get(d["image"]) or {}).get("urls") or {}
    d["image_mobile_urls"] = (media.get(d["image_mobile"]) or {}).get("urls") or d["image_urls"]
    d["created_at"] = b.created_at.isoformat() if b.created_at else None
    d["updated_at"] = b.updated_at.isoformat() if b.updated_at else None
    return d


def _apply_banner(b: ShowcaseBanner, data: dict):
    for key, value in data.items():
        if key == "title":
            b.title_i18n, b.title = value, svc._ru(value)[:255]
        elif key == "subtitle":
            b.subtitle_i18n, b.subtitle = value, svc._ru(value)[:255]
        elif key == "image":
            b.image_id = value
        elif key == "image_mobile":
            b.image_mobile_id = value
        elif key == "link":
            b.link = value or {}
        else:
            setattr(b, key, value)


class ShowcaseBannerListCreateAPIView(EditorAPIView):
    def get(self, request):
        company = self.company()
        banners = list(ShowcaseBanner.objects.filter(company=company).order_by("position", "created_at"))
        media = svc.media_map(company, [str(x) for b in banners for x in (b.image_id, b.image_mobile_id) if x])
        return Response([_banner_out(b, media) for b in banners])

    def post(self, request):
        company = self.company()
        data = entities.clean_banner(request.data, company)
        limit = ds.get_limits()["banners"]
        if ShowcaseBanner.objects.filter(company=company).count() >= limit:
            raise ShowcaseFieldError(None, f"Не больше {limit} баннеров.", "limit_exceeded")
        b = ShowcaseBanner(company=company)
        if "position" not in data:
            last = ShowcaseBanner.objects.filter(company=company).order_by("-position").values_list("position", flat=True).first()
            b.position = (last + 1) if last is not None else 0
        _apply_banner(b, data)
        b.save()
        media = svc.media_map(company, [str(x) for x in (b.image_id, b.image_mobile_id) if x])
        return Response(_banner_out(b, media), status=status.HTTP_201_CREATED)


class ShowcaseBannerDetailAPIView(EditorAPIView):
    def _get(self, pk):
        return get_object_or_404(ShowcaseBanner, id=pk, company=self.company())

    def get(self, request, pk):
        b = self._get(pk)
        return Response(_banner_out(b, svc.media_map(b.company, [str(x) for x in (b.image_id, b.image_mobile_id) if x])))

    def patch(self, request, pk):
        b = self._get(pk)
        data = entities.clean_banner(request.data, b.company, partial=True, instance=b)
        _apply_banner(b, data)
        b.save()
        return Response(_banner_out(b, svc.media_map(b.company, [str(x) for x in (b.image_id, b.image_mobile_id) if x])))

    put = patch

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _reorder(model, company, order_list, label):
    if not isinstance(order_list, list):
        raise ShowcaseFieldError("order", f"Ожидается список ID {label}.")
    objs = {str(o.id): o for o in model.objects.filter(company=company)}
    updates = []
    for pos, oid in enumerate(order_list):
        o = objs.get(str(oid))
        if o is not None:
            o.position = pos
            updates.append(o)
    if updates:
        model.objects.bulk_update(updates, ["position"])
    return len(updates)


class ShowcaseBannerReorderAPIView(EditorAPIView):
    def post(self, request):
        n = _reorder(ShowcaseBanner, self.company(), (request.data or {}).get("order"), "баннеров")
        return Response({"status": "ok", "reordered": n})


# ======================================================================
# 6.7 Блоки акций
# ======================================================================


def _apply_promo(pb: ShowcasePromoBlock, data: dict):
    for key, value in data.items():
        if key == "title":
            pb.title_i18n, pb.title = value, svc._ru(value)[:255]
        else:
            setattr(pb, key, value)


class ShowcasePromoBlockListCreateAPIView(EditorAPIView):
    def get(self, request):
        return Response([
            svc.promo_to_dict(pb)
            for pb in ShowcasePromoBlock.objects.filter(company=self.company()).order_by("position", "created_at")
        ])

    def post(self, request):
        company = self.company()
        data = entities.clean_promo(request.data, company)
        limit = ds.get_limits()["promo_blocks"]
        if ShowcasePromoBlock.objects.filter(company=company).count() >= limit:
            raise ShowcaseFieldError(None, f"Не больше {limit} блоков акций.", "limit_exceeded")
        pb = ShowcasePromoBlock(company=company, source={"type": "promotions", "ids": []})
        _apply_promo(pb, data)
        pb.save()
        return Response(svc.promo_to_dict(pb), status=status.HTTP_201_CREATED)


class ShowcasePromoBlockDetailAPIView(EditorAPIView):
    def _get(self, pk):
        return get_object_or_404(ShowcasePromoBlock, id=pk, company=self.company())

    def get(self, request, pk):
        pb = self._get(pk)
        data = svc.promo_to_dict(pb)
        from apps.main.showcase.views_public_design import resolve_promo_items

        data["items"] = resolve_promo_items(pb.company, data, svc.get_public_catalog(pb.company), request)
        return Response(data)

    def patch(self, request, pk):
        pb = self._get(pk)
        _apply_promo(pb, entities.clean_promo(request.data, pb.company, partial=True))
        pb.save()
        return Response(svc.promo_to_dict(pb))

    put = patch

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ShowcasePromoBlockReorderAPIView(EditorAPIView):
    def post(self, request):
        n = _reorder(ShowcasePromoBlock, self.company(), (request.data or {}).get("order"), "блоков")
        return Response({"status": "ok", "reordered": n})


# ======================================================================
# 6.9 Текстовые страницы
# ======================================================================


class ShowcasePageListCreateAPIView(EditorAPIView):
    def get(self, request):
        return Response([
            svc.page_to_dict(p) for p in ShowcasePage.objects.filter(company=self.company()).order_by("position", "created_at")
        ])

    def post(self, request):
        company = self.company()
        data = entities.clean_page(request.data, company)
        limit = ds.get_limits()["pages"]
        if ShowcasePage.objects.filter(company=company).count() >= limit:
            raise ShowcaseFieldError(None, f"Не больше {limit} страниц.", "limit_exceeded")
        if ShowcasePage.objects.filter(company=company, slug=data["slug"]).exists():
            raise ShowcaseFieldError("slug", "Страница с таким адресом уже есть.", "unique")
        page = ShowcasePage.objects.create(company=company, **data)
        return Response(svc.page_to_dict(page), status=status.HTTP_201_CREATED)


class ShowcasePageDetailAPIView(EditorAPIView):
    def _get(self, pk):
        return get_object_or_404(ShowcasePage, id=pk, company=self.company())

    def get(self, request, pk):
        return Response(svc.page_to_dict(self._get(pk)))

    def patch(self, request, pk):
        page = self._get(pk)
        data = entities.clean_page(request.data, page.company, partial=True)
        if "slug" in data and ShowcasePage.objects.filter(company=page.company, slug=data["slug"]).exclude(id=page.id).exists():
            raise ShowcaseFieldError("slug", "Страница с таким адресом уже есть.", "unique")
        for k, v in data.items():
            setattr(page, k, v)
        page.save()
        return Response(svc.page_to_dict(page))

    put = patch

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ======================================================================
# 6.11 Адрес витрины
# ======================================================================


class ShowcaseSlugCheckAPIView(EditorAPIView):
    def get(self, request):
        from apps.users.serializers import normalize_slug, slug_format_error, slug_taken_by_other

        company = self.company()
        raw = request.query_params.get("slug")
        if not raw:
            raise ShowcaseFieldError("slug", "Укажите slug.", "required")
        slug = normalize_slug(raw)
        err = slug_format_error(slug)
        if err:
            return Response({"available": False, "slug": slug, "reason": "invalid_format", "detail": str(err)})
        if slug == (company.slug or "").lower():
            return Response({"available": True, "slug": slug, "current": True})
        if slug_taken_by_other(slug, exclude_pk=company.pk):
            return Response({"available": False, "slug": slug, "reason": "taken"})
        if ShowcaseSlugRedirect.objects.filter(old_slug__iexact=slug, expires_at__gt=timezone.now()).exclude(company=company).exists():
            return Response({"available": False, "slug": slug, "reason": "reserved"})
        return Response({"available": True, "slug": slug})


# ======================================================================
# 6.12 Заказы с витрины (касса и программа владельца)
# ======================================================================


class ShowcaseOrderListAPIView(ShowcaseErrorMixin, generics.ListAPIView):
    serializer_class = ShowcaseOrderSerializer
    permission_classes = [IsShowcaseStaffPermission]
    pagination_class = None

    def get_queryset(self):
        company = _get_user_company(self.request)
        qs = ShowcaseOrder.objects.filter(company=company).prefetch_related(
            Prefetch("items", queryset=ShowcaseOrderItem.objects.select_related("variant"))
        )
        status_param = self.request.query_params.get("status")
        if status_param:
            statuses = [s for s in status_param.split(",") if s]
            bad = [s for s in statuses if s not in ShowcaseOrder.Status.values]
            if bad:
                raise ShowcaseFieldError("status", f"Недопустимый статус. Допустимые: {', '.join(ShowcaseOrder.Status.values)}")
            qs = qs.filter(status__in=statuses)

        source_param = self.request.query_params.get("source")
        if source_param:
            qs = qs.filter(source=source_param)

        date_from = self.request.query_params.get("date_from")
        if date_from:
            df = parse_date(date_from)
            if df:
                qs = qs.filter(created_at__date__gte=df)

        date_to = self.request.query_params.get("date_to")
        if date_to:
            dt = parse_date(date_to)
            if dt:
                qs = qs.filter(created_at__date__lte=dt)

        return qs.order_by("-created_at")


class ShowcaseOrderDetailAPIView(ShowcaseErrorMixin, generics.RetrieveUpdateAPIView):
    serializer_class = ShowcaseOrderSerializer
    permission_classes = [IsShowcaseStaffPermission]

    def get_queryset(self):
        company = _get_user_company(self.request)
        return ShowcaseOrder.objects.filter(company=company).prefetch_related(
            Prefetch("items", queryset=ShowcaseOrderItem.objects.select_related("variant"))
        )

    def put(self, request, *args, **kwargs):
        return self.patch(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        """
        PATCH {"status": "...", "sale": "<uuid продажи на кассе>"?}

        Остаток резервируется (списывается) при создании заказа. Чтобы не было двойного списания:
          • canceled — резерв возвращается на остаток;
          • done + sale (заказ пробит на кассе, касса списала сама) — резерв возвращается,
            итого товар списан один раз — продажей;
          • done без sale (выдан без кассы) — резерв остаётся окончательным списанием;
            позже можно прислать {"sale": ...} — резерв будет снят.
        Из done/canceled в другие статусы перевести нельзя.
        """
        from apps.main.models import Sale

        new_status = request.data.get("status")
        sale_id = request.data.get("sale")
        if new_status and new_status not in ShowcaseOrder.Status.values:
            raise ValidationError(
                {"status": f"Недопустимый статус. Допустимые: {', '.join(ShowcaseOrder.Status.values)}"}
            )

        final_statuses = (ShowcaseOrder.Status.DONE, ShowcaseOrder.Status.CANCELED)
        with transaction.atomic():
            order = get_object_or_404(
                ShowcaseOrder.objects.select_for_update(), pk=self.get_object().pk
            )
            update_fields = ["updated_at"]

            if new_status and new_status != order.status:
                if order.status in final_statuses:
                    raise ValidationError(
                        {"status": f"Заказ уже в статусе «{order.get_status_display()}», изменить нельзя."}
                    )
                order.status = new_status
                update_fields.append("status")

            if "pickup_code" in request.data:  # ТЗ Н-33: код выдачи пишет владелец
                code = str(request.data.get("pickup_code") or "").strip()
                if code and not (code.isdigit() and 4 <= len(code) <= 6):
                    raise ValidationError({"pickup_code": "Код выдачи — 4–6 цифр."})
                if code != order.pickup_code:
                    order.pickup_code = code
                    update_fields.append("pickup_code")

            if sale_id:
                if order.status == ShowcaseOrder.Status.CANCELED:
                    raise ValidationError({"sale": "Нельзя привязать продажу к отменённому заказу."})
                if order.sale_id and str(order.sale_id) != str(sale_id):
                    raise ValidationError({"sale": "К заказу уже привязана другая продажа."})
                sale = Sale.objects.filter(pk=sale_id, company_id=order.company_id).first()
                if sale is None:
                    raise ValidationError({"sale": "Продажа не найдена."})
                order.sale = sale
                update_fields.append("sale")

            release = order.stock_reserved and (
                order.status == ShowcaseOrder.Status.CANCELED or order.sale_id is not None
            )
            if release:
                release_stock(order_stock_lines(order))
                order.stock_reserved = False
                update_fields.append("stock_reserved")

            order.save(update_fields=update_fields)

        order = self.get_queryset().get(pk=order.pk)
        return Response(ShowcaseOrderSerializer(order).data)


# ======================================================================
# 6.14 Статистика
# ======================================================================


class ShowcaseStatsAPIView(EditorAPIView):
    def get(self, request):
        company = self.company()
        today = timezone.localdate()
        df_raw, dt_raw = request.query_params.get("date_from"), request.query_params.get("date_to")
        date_from = parse_date(df_raw) if df_raw else today - timedelta(days=30)
        date_to = parse_date(dt_raw) if dt_raw else today
        if date_from is None:
            raise ShowcaseFieldError("date_from", "Дата в формате ГГГГ-ММ-ДД.")
        if date_to is None:
            raise ShowcaseFieldError("date_to", "Дата в формате ГГГГ-ММ-ДД.")

        total_views = total_atc = 0
        pv: Dict[str, int] = {}
        bc: Dict[str, int] = {}
        daily = []
        for st in ShowcaseStats.objects.filter(company=company, date__gte=date_from, date__lte=date_to).order_by("date"):
            total_views += st.views
            total_atc += st.add_to_cart
            for pid, n in (st.product_views or {}).items():
                pv[pid] = pv.get(pid, 0) + n
            for bid, n in (st.banner_clicks or {}).items():
                bc[bid] = bc.get(bid, 0) + n
            daily.append({"date": st.date.isoformat(), "views": st.views, "add_to_cart": st.add_to_cart})

        orders_qs = ShowcaseOrder.objects.filter(
            company=company, created_at__date__gte=date_from, created_at__date__lte=date_to
        ).exclude(source="telegram")
        orders_count = orders_qs.count()

        names = {str(p.id): p.name for p in Product.objects.filter(company=company, id__in=[k for k in pv if svc._is_uuid(k)])}
        product_views = [
            {"id": pid, "name": names.get(pid, ""), "views": n}
            for pid, n in sorted(pv.items(), key=lambda x: -x[1])[:50]
        ]
        titles = {
            str(b.id): svc._i18n(b.title_i18n, b.title)
            for b in ShowcaseBanner.objects.filter(company=company, id__in=[k for k in bc if svc._is_uuid(k)])
        }
        banner_clicks = [
            {"id": bid, "title": titles.get(bid, {}), "clicks": n}
            for bid, n in sorted(bc.items(), key=lambda x: -x[1])
        ]
        return Response({
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "views": total_views,
            "add_to_cart": total_atc,
            "orders": orders_count,
            "product_views": product_views,
            "banner_clicks": banner_clicks,
            "daily": daily,
        })


class ShowcaseStockRequestListAPIView(ShowcaseErrorMixin, generics.ListAPIView):
    """
    ТЗ Н-27. GET main/showcase/stock-requests/?status=open|ready|notified|canceled
    Заявки «сообщить о поступлении» (по умолчанию все). Пагинация count/next/previous/results.
    """

    serializer_class = ShowcaseStockRequestSerializer
    permission_classes = [IsShowcaseStaffPermission]

    def get_queryset(self):
        company = _get_user_company(self.request)
        qs = ShowcaseStockRequest.objects.filter(company=company).select_related("product", "variant")
        status_param = self.request.query_params.get("status")
        if status_param:
            statuses = [x.strip() for x in status_param.split(",") if x.strip()]
            bad = [x for x in statuses if x not in ShowcaseStockRequest.Status.values]
            if bad:
                raise ShowcaseFieldError("status", f"Недопустимый статус. Допустимые: {', '.join(ShowcaseStockRequest.Status.values)}")
            qs = qs.filter(status__in=statuses)
        return qs.order_by("-created_at")
