"""
Публичные адреса витрины (без входа): вид (6.13), страницы, категории, заказ (6.12),
расчёт корзины, события статистики (6.14).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal
from typing import Dict, List, Optional
from urllib.parse import quote

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.integrations.events import emit_event
from core.api_compat import is_strict
from apps.main.models import (
    Product,
    ProductCategory,
    ProductVariant,
    ShowcaseOrder,
    ShowcaseOrderItem,
    ShowcaseStats,
    _money,
)
from apps.main.showcase import design_schema as ds
from apps.main.showcase import services as svc
from apps.main.showcase.design_schema import ShowcaseFieldError
from apps.main.showcase.serializers_design import ShowcaseOrderCreateSerializer
from apps.main.showcase.services import ShowcaseErrorMixin, resolve_public_company
from apps.main.variant_utils import InsufficientStock, name_with_variant, reserve_stock, variant_prices


class PublicAPIView(ShowcaseErrorMixin, APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []


def _client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or ""


def _abs(request, url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return request.build_absolute_uri(url) if request is not None and not url.startswith("http") else url


# ======================================================================
# Товары блоков акций (цены = расчёт кассы)
# ======================================================================


def _first_image(p, request):
    imgs = [i for i in p.images.all() if getattr(i, "image", None)]
    if not imgs:
        return None
    imgs.sort(key=lambda x: (not x.is_primary, x.created_at))
    return _abs(request, imgs[0].image.url)


def resolve_promo_items(company, block: dict, catalog: svc.PublicCatalog, request) -> List[dict]:
    source = block.get("source") or {}
    stype = source.get("type") or "promotions"
    ids = [str(x) for x in (source.get("ids") or []) if svc._is_uuid(x)]
    max_items = int(block.get("max_items") or 12)

    qs = Product.objects.filter(company=company).select_related("category").prefetch_related(
        "promotion_tiers", "images", Prefetch("variants", queryset=ProductVariant.objects.filter(is_active=True))
    )
    qs = svc.apply_catalog_visibility(qs, catalog)
    if catalog.hide_zero_price:
        qs = qs.filter(price__gt=0)
    manual_order = None
    if stype == "promotions":
        qs = qs.filter(stock=True, promotion_tiers__isnull=False)
        if ids:
            qs = qs.filter(Q(id__in=ids) | Q(promotion_tiers__id__in=ids))
        qs = qs.distinct().order_by("-created_at", "id")
    elif stype == "products":
        qs = qs.filter(id__in=ids)
        manual_order = {pid: i for i, pid in enumerate(ids)}
    elif stype == "category":
        cat_ids = set(ids)
        roots = ProductCategory.objects.filter(company=company, id__in=ids)
        try:
            cat_ids = {str(x) for x in ProductCategory.objects.get_queryset_descendants(roots, include_self=True).values_list("id", flat=True)}
        except Exception:
            pass
        qs = qs.filter(category_id__in=cat_ids).order_by("-created_at", "id")
    elif stype == "new":
        days = catalog.new_badge_days or 14
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=days)).order_by("-created_at", "id")
    elif stype == "on_sale":
        qs = qs.filter(Q(discount_percent__gt=0) | Q(stock=True, promotion_tiers__isnull=False)).distinct().order_by("-created_at", "id")
    else:
        return []

    products = list(qs[: max_items * 2 if manual_order is None else len(ids)])
    if manual_order is not None:
        products.sort(key=lambda p: manual_order.get(str(p.id), 10 ** 6))
    items = []
    for p in products[:max_items]:
        info = svc.product_promo_info(p, company)
        pid = str(p.id)
        items.append({
            "id": pid,
            "name": p.name,
            "image": _first_image(p, request),
            "category": str(p.category_id) if p.category_id else None,
            "category_title": p.category.name if p.category_id else None,
            "unit": p.unit,
            "is_weight": bool(p.is_weight),
            "has_variants": bool(list(p.variants.all())),
            "badge": catalog.badges.get(pid) or (f"-{info['discount_percent']}%" if info["discount_percent"] else None),
            **info,
        })
    return items


# ======================================================================
# 6.13 Публичная отдача вида
# ======================================================================


def _in_period(b: dict, now) -> bool:
    if not b.get("active", True):
        return False
    s, e = svc._parse_dt(b.get("starts_at")), svc._parse_dt(b.get("ends_at"))
    if s and s > now:
        return False
    if e and e < now:
        return False
    return True


def build_public_payload(company, snapshot: dict, doc: dict, version: int, request, catalog: svc.PublicCatalog,
                         preview: bool = False) -> dict:
    now = timezone.now()
    doc = dict(doc)
    cart = dict(doc.get("cart") or {})
    if not cart.get("whatsapp_phone") and company.phones_howcase:
        cart["whatsapp_phone"] = re.sub(r"\D", "", company.phones_howcase) or None
    doc["cart"] = cart

    banners = []
    for b in snapshot.get("banners") or []:
        b = svc._normalize_banner_dict(b)
        if _in_period(b, now):
            banners.append(b)
    banners.sort(key=lambda b: b.get("position") or 0)

    promos = []
    for pb in snapshot.get("promo_blocks") or []:
        pb = svc._normalize_promo_dict(pb)
        if not pb.get("active"):
            continue
        items = resolve_promo_items(company, pb, catalog, request)
        if not items:
            continue  # акция закончилась / нет товаров — блок скрывается сам
        pb["items"] = items
        promos.append(pb)
    promos.sort(key=lambda b: b.get("position") or 0)

    pages = [
        {"slug": p.get("slug"), "title": p.get("title") or {}, "show_in_footer": bool(p.get("show_in_footer", True)),
         "position": p.get("position") or 0}
        for p in sorted(snapshot.get("pages") or [], key=lambda p: p.get("position") or 0)
        if isinstance(p, dict)
    ]

    media_ids = ds.collect_media_ids(doc)
    for b in banners:
        media_ids.update(x for x in (b.get("image"), b.get("image_mobile")) if x)
    for c in catalog.categories.values():
        if c.get("image"):
            media_ids.add(c["image"])
    media = svc.media_map(company, media_ids)
    for b in banners:
        b["image_urls"] = (media.get(b.get("image")) or {}).get("urls") or {}
        b["image_mobile_urls"] = (media.get(b.get("image_mobile")) or {}).get("urls") or b["image_urls"]

    payload = {"version": version}
    for key in ds.DOCUMENT_SECTIONS:
        payload[key] = doc.get(key)
    payload.update({
        "banners": banners,
        "promo_blocks": promos,
        "pages": pages,
        "media": media,
        "company": {"id": str(company.id), "name": company.name, "slug": company.slug},
        "preview": preview,
    })
    return payload


class PublicCompanyShowcaseDesignAPIView(PublicAPIView):
    def get(self, request, slug: str):
        company = resolve_public_company(slug, request)
        token = request.query_params.get("preview")
        if token:
            svc.check_preview_token(company, token)
            design = svc.get_design(company)
            snap = svc.build_snapshot(company, design)
            doc = svc.draft_document(company, design)
            catalog = svc.get_public_catalog(company, token)
            payload = build_public_payload(company, snap, doc, design.version, request, catalog, preview=True)
            resp = Response(payload)
            resp["Cache-Control"] = "no-store"
            resp["X-Robots-Tag"] = "noindex"
            return resp

        design_row = svc.ShowcaseDesign.objects.filter(company=company).values_list("version", "published_at").first()
        version = design_row[0] if design_row else 1
        stamp = design_row[1].timestamp() if design_row and design_row[1] else 0
        cache_key = f"showcase:pubdesign:{company.id}:{version}:{stamp}:{company.slug}"
        cached = cache.get(cache_key)
        if cached is None:
            design = svc.get_design(company)
            pub = design.published if isinstance(design.published, dict) else {}
            doc = svc.published_document(company, design)
            catalog = svc.get_public_catalog(company)
            payload = build_public_payload(company, pub, doc, design.version, request, catalog)
            body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
            etag = f'"v{design.version}-{hashlib.md5(body.encode("utf-8")).hexdigest()[:12]}"'
            cached = {"payload": payload, "etag": etag}
            cache.set(cache_key, cached, svc.PUBLIC_DESIGN_CACHE_TTL)
        etag = cached["etag"]
        inm = request.headers.get("If-None-Match") or ""
        if inm and etag in [t.strip() for t in inm.split(",")]:
            resp = HttpResponse(status=status.HTTP_304_NOT_MODIFIED)
            resp["ETag"] = etag
            resp["Cache-Control"] = "no-cache"
            return resp
        resp = Response(cached["payload"])
        resp["ETag"] = etag
        # Браузер/CDN хранят, но всегда переспрашивают (ETag → 304): publish виден сразу.
        resp["Cache-Control"] = "no-cache"
        return resp


class PublicCompanyShowcasePageAPIView(PublicAPIView):
    """GET /public/companies/{slug}/showcase/pages/{page_slug}/ — опубликованная страница."""

    def get(self, request, slug: str, page_slug: str):
        company = resolve_public_company(slug, request)
        token = request.query_params.get("preview")
        if token:
            svc.check_preview_token(company, token)
            pages = [svc.page_to_dict(p) for p in svc.ShowcasePage.objects.filter(company=company)]
        else:
            design = svc.get_design(company)
            pages = (design.published or {}).get("pages") or []
        for p in pages:
            if p.get("slug") == page_slug:
                return Response({k: p.get(k) for k in ("slug", "title", "body", "show_in_footer", "position")})
        return Response({"detail": "Страница не найдена.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)


class PublicCompanyShowcaseCategoriesAPIView(PublicAPIView):
    """Видимые категории витрины в порядке владельца, со своим названием и картинкой."""

    def get(self, request, slug: str):
        company = resolve_public_company(slug, request)
        catalog = svc.get_public_catalog(company, request.query_params.get("preview"))
        prod_qs = svc.apply_catalog_visibility(Product.objects.filter(company=company), catalog)
        counts = dict(prod_qs.values("category_id").annotate(n=Count("id")).values_list("category_id", "n"))
        cats = ProductCategory.objects.filter(company=company).exclude(id__in=list(catalog.hidden_categories))
        media = svc.media_map(company, [c.get("image") for c in catalog.categories.values() if c.get("image")])
        out = []
        for c in cats:
            cfg = catalog.categories.get(str(c.id)) or {}
            image = cfg.get("image")
            out.append({
                "id": str(c.id),
                "name": c.name,
                "title": cfg.get("title_override") or {"ru": c.name},
                "parent": str(c.parent_id) if c.parent_id else None,
                "image": image,
                "image_urls": (media.get(image) or {}).get("urls") if image else None,
                "sort_order": cfg.get("sort_order"),
                "products_count": counts.get(c.id, 0),
            })
        out.sort(key=lambda r: (r["sort_order"] is None, r["sort_order"] or 0, r["name"].lower()))
        return Response(out)


# ======================================================================
# 6.12 Заказ с витрины / расчёт корзины
# ======================================================================


def _rate_limited(key: str, limit: int, window: int) -> bool:
    try:
        added = cache.add(key, 0, window)
        n = cache.incr(key)
    except Exception:
        return False
    return n > limit


def _order_rate() -> tuple:
    return tuple(getattr(settings, "SHOWCASE_ORDER_RATE", (10, 3600)))  # Б-34: 10 заказов в час с одного IP / номера


def _price_lines(company, items_input, catalog: svc.PublicCatalog, strict: bool = True):
    """Позиции заказа/корзины с ценой и акцией как в кассе."""
    product_ids = [str(item["product"]) for item in items_input]
    products = {
        str(p.id): p
        for p in Product.objects.filter(company=company, id__in=product_ids).prefetch_related(
            "promotion_tiers",
            Prefetch("variants", queryset=ProductVariant.objects.filter(is_active=True)),
        )
    }
    variant_ids = [item["variant"] for item in items_input if item.get("variant")]
    variants_by_id = {str(v.id): v for v in ProductVariant.objects.filter(company=company, id__in=variant_ids)}

    lines = []
    variant_asked: dict = {}
    items_total = Decimal("0.00")
    for idx, item in enumerate(items_input):
        pid = str(item["product"])
        vid = item.get("variant")
        prod = products.get(pid)
        if prod is None:
            raise ShowcaseFieldError(f"items[{idx}].product", f"Товар {pid} не найден или недоступен.", "product_not_found")
        if pid in catalog.hidden_products or (prod.category_id and str(prod.category_id) in catalog.hidden_categories):
            raise ShowcaseFieldError(f"items[{idx}].product", f"Товар «{prod.name}» скрыт с витрины.", "product_hidden")
        variant_obj = None
        if vid:
            variant_obj = variants_by_id.get(str(vid))
            if variant_obj is not None and variant_obj.product_id != prod.id:
                # ТЗ Н-27: вариант чужого товара
                raise ShowcaseFieldError(
                    f"items[{idx}].variant", f"Вариант {vid} не относится к товару «{prod.name}».",
                    "variant_mismatch", extra={"index": idx})
            if not variant_obj or not variant_obj.is_active:
                raise ShowcaseFieldError(
                    f"items[{idx}].variant", f"Вариант {vid} не найден для товара {prod.name}.",
                    "variant_not_found", extra={"index": idx})
        elif list(prod.variants.all()):
            # variant_required — для всех клиентов, как было на сервере и до Б-02 (меняем только 409 → 400)
            raise ShowcaseFieldError(
                f"items[{idx}].variant", f"Выберите размер и цвет для товара «{prod.name}».",
                "variant_required", extra={"index": idx})
        qty = Decimal(str(item["qty"]))
        from apps.main.tz26.hw_units.units import order_line_unit, product_allows_fraction, to_base_line

        ounit = order_line_unit(prod, item, idx, variant_obj)  # ТЗ 26.10 Б-53 п.8: единица продажи
        if not prod.is_weight and qty != qty.to_integral_value() and not product_allows_fraction(prod, ounit):
            raise ShowcaseFieldError(f"items[{idx}].qty", f"Товар «{prod.name}» продаётся только целыми штуками.")
        if variant_obj is not None:
            # ТЗ Н-27: хватает ли остатка (с учётом одинаковых вариантов в разных строках корзины)
            asked = variant_asked.get(variant_obj.id, Decimal("0")) + qty
            variant_asked[variant_obj.id] = asked
            have = max(Decimal(str(variant_obj.quantity or 0)), Decimal("0"))
            if asked > have:
                raise ShowcaseFieldError(
                    f"items[{idx}].variant",
                    f"Недостаточно остатка «{name_with_variant(prod.name, variant_obj)}»: доступно {have:g}.",
                    "variant_out_of_stock" if strict else "not_enough_stock",
                    status_code=status.HTTP_409_CONFLICT if strict else status.HTTP_400_BAD_REQUEST,
                    extra={"index": idx, "available": int(have) if have == have.to_integral_value() else str(have)})
        unit_price = variant_prices(variant_obj, prod)[0] if variant_obj is not None else Decimal(str(prod.price or 0))
        name_suffix = ""
        if ounit is not None:  # цена единицы; в строку и резерв идут базовые единицы
            from apps.main.tz26.hw_units.units import effective_unit_price

            unit_price = effective_unit_price(prod, ounit)
        gross, line_discount, net, tier = svc.kassa_line(prod, unit_price, qty, company)
        if ounit is not None:
            qty, unit_price, name_suffix = to_base_line(ounit, qty, unit_price)
        items_total += net
        lines.append({
            "product": prod,
            "variant": variant_obj,
            "product_name": name_with_variant(prod.name, variant_obj) + name_suffix,
            "qty": qty,
            "price": _money(unit_price),
            "gross": gross,
            "discount": line_discount,
            "total": net,
            "promotion": str(tier.id) if tier is not None else None,
        })
    return lines, _money(items_total)


def _delivery_fee(cart: dict, delivery_type: str, items_total: Decimal) -> Decimal:
    if delivery_type != "delivery":
        return Decimal("0.00")
    d = cart.get("delivery") or {}
    fee = Decimal(str(d.get("delivery_fee") or 0))
    free_from = d.get("free_from")
    if free_from is not None and items_total >= Decimal(str(free_from)):
        return Decimal("0.00")
    return _money(fee)


def _whatsapp_url(company, cart: dict, order, lines) -> Optional[str]:
    phone = re.sub(r"\D", "", cart.get("whatsapp_phone") or company.phones_howcase or "")
    if not phone:
        return None
    rows = [f"Заказ №{order.number} с витрины {company.name}"]
    for ln in lines:
        rows.append(f"• {ln['product_name']} × {ln['qty'].normalize():f} = {ln['total']} сом")
    if order.delivery_fee:
        rows.append(f"Доставка: {order.delivery_fee} сом")
    rows.append(f"Итого: {order.total} сом")
    if order.customer_name:
        rows.append(f"Имя: {order.customer_name}")
    rows.append(f"Телефон: {order.customer_phone}")
    if order.delivery_type == "delivery" and order.delivery_address:
        rows.append(f"Адрес: {order.delivery_address}")
    if order.comment:
        rows.append(f"Комментарий: {order.comment}")
    return f"https://wa.me/{phone}?text={quote(chr(10).join(rows))}"


def _order_response(order, whatsapp_url=None, replayed=False) -> dict:
    data = {
        "id": str(order.id),
        "number": order.number,
        "status": order.status,
        "total": str(order.total),
        "delivery_fee": str(order.delivery_fee),
        "source": order.source,
        "pickup_code": order.pickup_code,  # ТЗ Н-33
        "whatsapp_url": whatsapp_url,
        "created_at": order.created_at.isoformat(),
    }
    if replayed:
        data["replayed"] = True  # Б-34: повтор «Оформить» тем же ключом — тот же заказ
    return data


class PublicCompanyShowcaseCartQuoteAPIView(PublicAPIView):
    """POST /public/companies/{slug}/showcase/cart/quote/ — расчёт корзины как в кассе (без создания заказа)."""

    def post(self, request, slug: str):
        company = resolve_public_company(slug, request)
        items = request.data.get("items") if isinstance(request.data, dict) else None
        ser = ShowcaseOrderCreateSerializer(data={"customer": {"phone": "0"}, "items": items or []})
        ser.is_valid(raise_exception=True)
        catalog = svc.get_public_catalog(company)
        cart = svc.published_document(company)["cart"]
        lines, items_total = _price_lines(company, ser.validated_data["items"], catalog, strict=is_strict(request, company))
        dtype = ((request.data.get("delivery") or {}).get("type")) if isinstance(request.data.get("delivery"), dict) else "pickup"
        fee = _delivery_fee(cart, dtype or "pickup", items_total)
        min_total = cart.get("min_order_total")
        return Response({
            "items": [
                {"product": str(ln["product"].id), "variant": str(ln["variant"].id) if ln["variant"] else None,
                 "name": ln["product_name"], "qty": str(ln["qty"]), "price": str(ln["price"]),
                 "gross": str(ln["gross"]), "discount": str(ln["discount"]), "total": str(ln["total"]),
                 "promotion": ln["promotion"]}
                for ln in lines
            ],
            "items_total": str(items_total),
            "delivery_fee": str(fee),
            "total": str(_money(items_total + fee)),
            "min_order_total": min_total,
            "min_order_ok": min_total is None or items_total >= Decimal(str(min_total)),
        })


class PublicCompanyShowcaseOrderCreateAPIView(PublicAPIView):
    def post(self, request, slug: str):
        company = resolve_public_company(slug, request)
        idempotency_key = (request.headers.get("Idempotency-Key") or request.data.get("idempotency_key") or "").strip()[:128]
        doc = svc.published_document(company)
        cart = doc["cart"]

        if idempotency_key:
            existing = ShowcaseOrder.objects.filter(company=company, idempotency_key=idempotency_key).first()
            if existing:
                lines = [{"product_name": it.product_name, "qty": it.qty, "total": it.total} for it in existing.items.all()]
                return Response(_order_response(existing, _whatsapp_url(company, cart, existing, lines), replayed=True), status=status.HTTP_200_OK)

        # Защита от спама: скрытое поле-ловушка + лимит заказов с одного IP / номера.
        if isinstance(request.data, dict) and (request.data.get("website") or request.data.get("hp")):
            raise ShowcaseFieldError(None, "Заказ отклонён.", "spam")

        serializer = ShowcaseOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        customer = data["customer"]
        delivery = data.get("delivery") or {}
        fields = cart.get("fields") or {}

        limit, window = _order_rate()
        ip = _client_ip(request)
        phone_digits = re.sub(r"\D", "", customer.get("phone") or "")
        if _rate_limited(f"showcase:ordrate:ip:{company.id}:{ip}", limit, window) or (
            phone_digits and _rate_limited(f"showcase:ordrate:ph:{company.id}:{phone_digits}", limit, window)
        ):
            return Response(
                {"detail": "Слишком много заказов. Попробуйте позже.", "code": "throttled"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        if fields.get("phone", "required") == "required" and len(phone_digits) < 9:
            raise ShowcaseFieldError("customer.phone", "Укажите номер телефона.", "required")
        if fields.get("name") == "required" and not (customer.get("name") or "").strip():
            raise ShowcaseFieldError("customer.name", "Укажите имя.", "required")
        if fields.get("comment") == "required" and not (data.get("comment") or "").strip():
            raise ShowcaseFieldError("comment", "Укажите комментарий.", "required")

        dset = cart.get("delivery") or {}
        dtype = delivery.get("type") or ("pickup" if dset.get("pickup", True) else "delivery")
        if dtype == "pickup" and not dset.get("pickup", True):
            raise ShowcaseFieldError("delivery.type", "Самовывоз недоступен.", "invalid")
        if dtype == "delivery" and not dset.get("delivery"):
            raise ShowcaseFieldError("delivery.type", "Доставка недоступна.", "invalid")
        address = (delivery.get("address") or "").strip()
        if (dtype == "delivery" or fields.get("address") == "required") and not address:
            raise ShowcaseFieldError("delivery.address", "Укажите адрес доставки.", "required")

        catalog = svc.get_public_catalog(company)
        lines, items_total = _price_lines(company, data["items"], catalog, strict=is_strict(request, company))
        min_total = cart.get("min_order_total")
        if min_total is not None and items_total < Decimal(str(min_total)):
            raise ShowcaseFieldError("items", f"Минимальная сумма заказа — {min_total} сом.", "min_order_total")
        fee = _delivery_fee(cart, dtype, items_total)
        total = _money(items_total + fee)

        with transaction.atomic():
            last_num = (
                ShowcaseOrder.objects.filter(company=company).select_for_update().aggregate(m=Max("number"))["m"] or 0
            )
            order = ShowcaseOrder.objects.create(
                company=company,
                number=last_num + 1,
                status=ShowcaseOrder.Status.NEW,
                customer_name=(customer.get("name") or "").strip(),
                customer_phone=(customer.get("phone") or "").strip(),
                delivery_type=dtype,
                delivery_address=address,
                source=data.get("source") or "showcase",
                telegram_chat_id=(data.get("telegram_chat_id") or "") if data.get("source") == "telegram_bot" else "",
                comment=data.get("comment", ""),
                total=total,
                delivery_fee=fee,
                idempotency_key=idempotency_key or None,
            )
            ShowcaseOrderItem.objects.bulk_create([
                ShowcaseOrderItem(
                    order=order, product=ln["product"], variant=ln.get("variant"), product_name=ln["product_name"][:255],
                    qty=ln["qty"], price=ln["price"], discount=ln["discount"], total=ln["total"],
                )
                for ln in lines
            ])
            # Резерв остатка по варианту (как в кассе). Снимается при отмене заказа
            # или при выдаче через кассу (PATCH status=done + sale) — см. ShowcaseOrderDetailAPIView.
            try:
                reserve_stock(lines)
            except InsufficientStock as exc:
                # гонка: остаток разобрали между проверкой и резервом
                if is_strict(request, company):
                    raise ShowcaseFieldError("items", str(exc), "variant_out_of_stock", status_code=status.HTTP_409_CONFLICT)
                raise ShowcaseFieldError("items", str(exc), "not_enough_stock")
            order.stock_reserved = True
            order.save(update_fields=["stock_reserved"])

        # ТЗ Б-22: сообщение владельцу / в группу заказов (после коммита, не больше одного на заказ)
        from apps.main.telegram_bot.services import order_notify

        order_notify.notify_after_commit(order.id)

        whatsapp_url = _whatsapp_url(company, cart, order, lines)
        webhook_data = {
            "id": str(order.id),
            "number": order.number,
            "status": order.status,
            "total": str(order.total),
            "delivery_fee": str(order.delivery_fee),
            "source": order.source,
            "customer": {"name": order.customer_name, "phone": order.customer_phone},
            "delivery": {"type": order.delivery_type, "address": order.delivery_address},
            "comment": order.comment,
            "items": [
                {
                    "product": str(it.product_id) if it.product_id else None,
                    "variant": str(it.variant_id) if it.variant_id else None,
                    "product_name": it.product_name,
                    "qty": str(it.qty),
                    "price": str(it.price),
                    "discount": str(it.discount),
                    "total": str(it.total),
                }
                for it in order.items.all()
            ],
            "whatsapp_url": whatsapp_url,
            "created_at": order.created_at.isoformat(),
        }
        try:
            emit_event(company.id, "order.created", webhook_data)
        except Exception:
            pass

        try:  # Б-80: заказ из чата (source=whatsapp|instagram|web_chat + conversation_id) — ссылка в диалоге
            from apps.inbox.actions import link_public_order
            link_public_order(order, request.data)
        except Exception:
            pass
        return Response(_order_response(order, whatsapp_url), status=status.HTTP_201_CREATED)


class PublicCompanyShowcaseNotifyAPIView(PublicAPIView):
    """
    ТЗ Н-27. POST /public/companies/{slug}/showcase/{product_id}/notify/
    {"variant": "<uuid>", "phone": "+996…" | "telegram_chat_id": "…"} — «сообщить о поступлении».
    До 5 заявок в сутки на номер/chat id и на IP. Повтор той же заявки возвращает существующую (200).
    """

    def post(self, request, slug: str, product_id):
        from apps.main.models import ShowcaseStockRequest

        company = resolve_public_company(slug, request)
        data = request.data if isinstance(request.data, dict) else {}
        product = Product.objects.filter(company=company, id=product_id).first()
        if product is None:
            raise ShowcaseFieldError("product", "Товар не найден.", "product_not_found", status_code=status.HTTP_404_NOT_FOUND)
        catalog = svc.get_public_catalog(company)
        if str(product.id) in catalog.hidden_products or (
            product.category_id and str(product.category_id) in catalog.hidden_categories
        ):
            raise ShowcaseFieldError("product", "Товар скрыт с витрины.", "product_hidden", status_code=status.HTTP_404_NOT_FOUND)

        phone = re.sub(r"[^\d+]", "", str(data.get("phone") or ""))[:64]
        chat_id = str(data.get("telegram_chat_id") or "").strip()[:64]
        if len(re.sub(r"\D", "", phone)) < 9:
            phone = ""
        if not phone and not chat_id:
            raise ShowcaseFieldError("phone", "Укажите телефон или Telegram.", "required")

        vid = data.get("variant")
        variant = None
        if vid:
            try:
                variant = ProductVariant.objects.filter(id=vid, is_active=True).first()
            except Exception:
                variant = None
            if variant is None:
                raise ShowcaseFieldError("variant", "Вариант не найден.", "variant_not_found")
            if variant.product_id != product.id:
                raise ShowcaseFieldError("variant", "Вариант не относится к товару.", "variant_mismatch")
        elif product.variants.filter(is_active=True).exists():
            raise ShowcaseFieldError("variant", "Выберите размер и цвет.", "variant_required")

        in_stock = (variant.quantity if variant is not None else product.quantity) or 0
        if in_stock > 0:
            raise ShowcaseFieldError("variant", "Товар уже есть в наличии.", "already_in_stock")

        existing = ShowcaseStockRequest.objects.filter(
            company=company, product=product, variant=variant, status=ShowcaseStockRequest.Status.OPEN
        )
        existing = existing.filter(phone=phone) if phone else existing.filter(telegram_chat_id=chat_id)
        row = existing.first()
        if row is not None:
            return Response({"id": str(row.id), "status": row.status}, status=status.HTTP_200_OK)

        ip = _client_ip(request)
        contact = phone or chat_id
        if _rate_limited(f"showcase:notify:ip:{company.id}:{ip}", 5, 86400) or _rate_limited(
            f"showcase:notify:ct:{company.id}:{contact}", 5, 86400
        ):
            return Response(
                {"detail": "Слишком много заявок. Попробуйте завтра.", "code": "throttled"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        row = ShowcaseStockRequest.objects.create(
            company=company, product=product, variant=variant, phone=phone, telegram_chat_id=chat_id, client_ip=ip[:64]
        )
        return Response({"id": str(row.id), "status": row.status}, status=status.HTTP_201_CREATED)


# ======================================================================
# 6.14 События статистики
# ======================================================================

EVENT_TYPES = ("view", "product_view", "banner_click", "add_to_cart")


def _session_key(request, explicit: Optional[str]) -> str:
    if explicit:
        return str(explicit)[:64]
    raw = f"{_client_ip(request)}|{request.META.get('HTTP_USER_AGENT', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def track_event(company, event_type: str, obj_id: Optional[str], session: str) -> str:
    if event_type in ("product_view", "banner_click") and not (obj_id and svc._is_uuid(obj_id)):
        raise ShowcaseFieldError("id", "Укажите id товара или баннера.", "required")
    if event_type == "banner_click":
        key = f"showcase:bclick:{company.id}:{obj_id}:{session}"
        if not cache.add(key, 1, 86400):
            return "already_tracked"
    today = timezone.localdate()
    with transaction.atomic():
        stats, _ = ShowcaseStats.objects.select_for_update().get_or_create(
            company=company, date=today,
            defaults={"views": 0, "add_to_cart": 0, "product_views": {}, "banner_clicks": {}},
        )
        if event_type == "view":
            stats.views += 1
        elif event_type == "add_to_cart":
            stats.add_to_cart += 1
        elif event_type == "product_view":
            pv = dict(stats.product_views or {})
            pv[obj_id] = pv.get(obj_id, 0) + 1
            stats.product_views = pv
        elif event_type == "banner_click":
            bc = dict(stats.banner_clicks or {})
            bc[obj_id] = bc.get(obj_id, 0) + 1
            stats.banner_clicks = bc
        stats.save()
    return "ok"


class PublicCompanyShowcaseEventsAPIView(PublicAPIView):
    """POST {type: view|product_view|banner_click|add_to_cart, id?, session?}."""

    def post(self, request, slug: str):
        company = resolve_public_company(slug, request)
        data = request.data if isinstance(request.data, dict) else {}
        etype = data.get("type")
        if etype not in EVENT_TYPES:
            raise ShowcaseFieldError("type", f"Недопустимый тип события. Допустимые: {', '.join(EVENT_TYPES)}.")
        limit_key = f"showcase:evrate:{company.id}:{_client_ip(request)}"
        if _rate_limited(limit_key, 600, 60):
            return Response({"detail": "Слишком много событий.", "code": "throttled"}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        session = _session_key(request, data.get("session") or data.get("session_id") or request.headers.get("X-Session-Id"))
        obj_id = data.get("id")
        result = track_event(company, etype, str(obj_id) if obj_id else None, session)
        return Response({"status": result})


class PublicCompanyShowcaseTrackAPIView(PublicAPIView):
    """Старый адрес (ТЗ-BE-2026-03): {event, product_id?, banner_id?, session_id?}."""

    def post(self, request, slug: str):
        company = resolve_public_company(slug, request)
        data = request.data if isinstance(request.data, dict) else {}
        etype = data.get("event")
        if etype not in EVENT_TYPES:
            raise ShowcaseFieldError("event", f"Недопустимый тип события. Допустимые: {', '.join(EVENT_TYPES)}.")
        obj_id = data.get("banner_id") if etype == "banner_click" else data.get("product_id")
        session = _session_key(request, data.get("session_id"))
        return Response({"status": track_event(company, etype, str(obj_id) if obj_id else None, session)})
