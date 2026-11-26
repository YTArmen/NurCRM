"""П2-3 (15.11.2026, p23_1511): склад, третья очередь — история цен, переоценка, ABC/XYZ.

GET  products/<id>/price-history/?page=   — смены цены продажи (журнал действий price_change + переоценки), новые сверху.
POST price-revisions/ + Idempotency-Key — переоценка (владелец/админ или can_edit_products):
     {"comment": "", "lines": [{"product": id, "price": "120.00", "wholesale_price"?: "100"}]}
     или правило {"rule": {"markup_percent": 30, "category"?: id, "brand"?: id, "products"?: [id], "round_to"?: 1|5|10}}
     — цена = закупочная × (1 + наценка/100), округление вверх до round_to (по умолчанию 1 сом); товары без закупочной пропускаются.
     Ответ: {id, number, changed, skipped, lines[...]}. Повтор ключа -> тот же ответ (replayed=true).
GET  price-revisions/, price-revisions/<id>/
GET  analytics/stock-abcxyz/?days=90&page=&page_size=&class=AX
     ABC — по выручке за days (A ≤80%, B ≤95%, C остальное), XYZ — по стабильности недельных продаж
     (коэф. вариации: X <0,25, Y <0,5, Z — больше или нет продаж), days_of_supply = остаток / среднедневные продажи,
     dead_stock = остаток > 0 и нет продаж за days.
"""
import math
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F, Max, Sum
from django.db.models.functions import TruncWeek
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

Q2 = Decimal("0.01")


def _company(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def _err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def _page(qp, default=50, cap=500):
    try:
        return max(1, int(qp.get("page") or 1)), min(cap, max(1, int(qp.get("page_size") or default)))
    except ValueError:
        return 1, default


class ProductPriceHistoryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        from apps.construction.models import AuditLog
        from apps.main.models import Product

        company = _company(request.user)
        p = get_object_or_404(Product, pk=pk, company=company)
        page, size = _page(request.query_params)
        qs = (AuditLog.objects.filter(company=company, object_type="product", object_id=str(p.pk), action="price_change")
              .select_related("user").order_by("-created_at"))
        total = qs.count()
        rows = []
        for r in qs[(page - 1) * size: page * size]:
            b, a = r.before or {}, r.after or {}
            rows.append({"at": r.created_at.isoformat(), "user_id": str(r.user_id) if r.user_id else None,
                         "user_name": ((f"{r.user.first_name} {r.user.last_name}".strip() or r.user.email) if r.user else None),
                         "old_price": b.get("price"), "new_price": a.get("price"),
                         "old_wholesale_price": b.get("wholesale_price"), "new_wholesale_price": a.get("wholesale_price"),
                         "source": (a.get("source") or "edit")})
        return Response({"product_id": str(p.pk), "name": p.name, "price": format(p.price or 0, "f"),
                         "count": total, "page": page, "page_size": size, "results": rows})


def _rev_payload(rev):
    return {"id": str(rev.id), "number": rev.number, "comment": rev.comment, "rule": rev.rule,
            "effective_at": rev.effective_at.isoformat(), "created_at": rev.created_at.isoformat(),
            "created_by": str(rev.created_by_id) if rev.created_by_id else None,
            "lines": [{"product": str(ln.product_id), "old_price": format(ln.old_price, "f") if ln.old_price is not None else None,
                       "new_price": format(ln.new_price, "f"),
                       "old_wholesale_price": format(ln.old_wholesale, "f") if ln.old_wholesale is not None else None,
                       "new_wholesale_price": format(ln.new_wholesale, "f") if ln.new_wholesale is not None else None}
                      for ln in rev.lines.all()]}


def _dec(v, field, errs):
    try:
        d = Decimal(str(v).replace(",", "."))
        if d < 0:
            raise InvalidOperation
        return d
    except (InvalidOperation, ValueError, TypeError):
        errs[field] = ["Неотрицательное число."]
        return None


class PriceRevisionAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk=None):
        from apps.main.models import PriceRevision

        company = _company(request.user)
        if pk:
            return Response(_rev_payload(get_object_or_404(PriceRevision.objects.prefetch_related("lines"), pk=pk, company=company)))
        page, size = _page(request.query_params)
        qs = PriceRevision.objects.filter(company=company).prefetch_related("lines")
        return Response({"count": qs.count(), "page": page, "page_size": size,
                         "results": [_rev_payload(r) for r in qs[(page - 1) * size: page * size]]})

    def post(self, request, pk=None):
        from apps.main.models import PriceRevision, Product
        from apps.main.pos_serializers import _is_owner_like

        user = request.user
        if not (_is_owner_like(user) or getattr(user, "can_edit_products", None)):
            return _err("permission_denied", "Нет права менять цены.", 403, permission="can_edit_products")
        company = _company(user)
        data = request.data if hasattr(request.data, "get") else {}
        key = (request.headers.get("Idempotency-Key") or str(data.get("idempotency_key") or "")).strip()[:128]
        if key:
            prior = PriceRevision.objects.filter(company=company, idempotency_key=key).prefetch_related("lines").first()
            if prior is not None:
                return Response({**_rev_payload(prior), "replayed": True})
        errs, plan, skipped = {}, {}, []
        rule = data.get("rule") if isinstance(data.get("rule"), dict) else None
        if rule:
            mk = _dec(rule.get("markup_percent"), "rule.markup_percent", errs)
            try:
                step = Decimal(str(rule.get("round_to") or 1))
                if step <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                errs["rule.round_to"] = ["Больше нуля."]
            if errs:
                return _err("validation_error", "Проверьте правило.", fields=errs)
            from django.core.exceptions import ValidationError as DjValidationError

            if not (rule.get("category") or rule.get("brand") or rule.get("products")) and not data.get("confirm_all"):
                return _err("scope_required", "Правило без category/brand/products меняет ВСЕ товары: передайте confirm_all=true.")
            try:
                qs = Product.objects.filter(company=company).exclude(kind="service")
                if rule.get("category"):
                    qs = qs.filter(category_id=rule["category"])
                if rule.get("brand"):
                    qs = qs.filter(brand_id=rule["brand"])
                if isinstance(rule.get("products"), list):
                    qs = qs.filter(id__in=[str(x) for x in rule["products"]])
                picked = list(qs.only("id", "purchase_price", "price")[:20001])
            except (DjValidationError, ValueError, TypeError):
                return _err("validation_error", "Неверный id в правиле (category, brand или products).")
            truncated = len(picked) > 20000
            for p in picked[:20000]:
                if not p.purchase_price or p.purchase_price <= 0:
                    skipped.append(str(p.id))
                    continue
                raw = Decimal(str(p.purchase_price)) * (1 + mk / 100)
                price = ((raw / step).to_integral_value(rounding=ROUND_CEILING) * step).quantize(Q2)
                plan[str(p.id)] = (price, None)
        else:
            lines = data.get("lines")
            if not isinstance(lines, list) or not lines:
                return _err("validation_error", "Нужны lines или rule.", fields={"lines": ["Хотя бы одна строка."]})
            for i, ln in enumerate(lines):
                if not isinstance(ln, dict) or not ln.get("product"):
                    errs[f"lines[{i}].product"] = ["Обязательно."]
                    continue
                price = _dec(ln.get("price"), f"lines[{i}].price", errs)
                whs = _dec(ln["wholesale_price"], f"lines[{i}].wholesale_price", errs) if ln.get("wholesale_price") not in (None, "") else None
                if price is not None:
                    plan[str(ln["product"])] = (price.quantize(Q2), whs)
            if errs:
                return _err("validation_error", "Проверьте строки.", fields=errs)
        self._truncated = bool(rule) and locals().get("truncated", False)
        if not plan:
            return _err("nothing_to_change", "Нет товаров для переоценки.", skipped=len(skipped))
        try:
            with transaction.atomic():
                return self._apply(request, company, data, key, rule, plan, skipped)
        except IntegrityError:
            prior = PriceRevision.objects.filter(company=company, idempotency_key=key).first() if key else None
            if prior is not None:
                return Response({**_rev_payload(prior), "replayed": True})
            raise

    def _apply(self, request, company, data, key, rule, plan, skipped):
        from apps.construction.audit import log_request
        from apps.main.models import PriceRevision, PriceRevisionLine, Product

        try:
            prods = {str(p.id): p for p in Product.objects.select_for_update().filter(company=company, id__in=list(plan))}
        except Exception:  # noqa: BLE001
            prods = {}
        missing = [pid for pid in plan if pid not in prods]
        if missing:
            return _err("validation_error", "Товары не найдены.", fields={"products": missing[:50]})
        num = (PriceRevision.objects.filter(company=company).aggregate(m=Max("number"))["m"] or 0) + 1
        rev = PriceRevision.objects.create(company=company, number=num, comment=str(data.get("comment") or "")[:2000],
                                           rule=rule, effective_at=timezone.now(), idempotency_key=key, created_by=request.user)
        changed = 0
        for pid, (price, whs) in plan.items():
            p = prods[pid]
            old_p, old_w = p.price, p.wholesale_price
            if Decimal(str(old_p or 0)).quantize(Q2) == price and (whs is None or Decimal(str(old_w or 0)) == whs):
                continue
            PriceRevisionLine.objects.create(revision=rev, product=p, old_price=old_p, new_price=price,
                                             old_wholesale=old_w, new_wholesale=whs)
            p.price = price
            fields = ["price"]
            if whs is not None:
                p.wholesale_price = whs
                fields.append("wholesale_price")
            if p.purchase_price and Decimal(str(p.purchase_price)) > 0 and hasattr(p, "markup_percent"):
                p.markup_percent = ((price - Decimal(str(p.purchase_price))) / Decimal(str(p.purchase_price)) * 100).quantize(Decimal("0.0001"))
                fields.append("markup_percent")
            p.save(update_fields=fields)
            changed += 1
            try:
                log_request(request, "price_change", "product", p.pk, p.name, company=company,
                            before={"price": str(old_p), "wholesale_price": str(old_w)},
                            after={"price": str(price), "wholesale_price": str(whs if whs is not None else old_w),
                                   "source": f"revision:{rev.number}"})
            except Exception:  # noqa: BLE001
                pass
        from apps.main.cache_utils import invalidate_cache_pattern

        invalidate_cache_pattern(f"products:list:{company.id}:")
        return Response({**_rev_payload(rev), "changed": changed, "skipped": len(skipped),
                         "truncated": getattr(self, "_truncated", False), "replayed": False}, status=201)


class StockAbcXyzAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.main.models import Product, SaleItem

        company = _company(request.user)
        qp = request.query_params
        try:
            days = min(365, max(28, int(qp.get("days") or 90)))
        except ValueError:
            days = 90
        page, size = _page(qp, 100, 1000)
        since = timezone.now() - timedelta(days=days)
        base = SaleItem.objects.filter(sale__company=company, sale__created_at__gte=since,
                                       sale__status__in=("paid", "debt", "partially_returned"), product__isnull=False)
        base = base.exclude(sale__is_test=True)
        rev = {r["product_id"]: (Decimal(r["rev"] or 0), Decimal(r["q"] or 0))
               for r in base.values("product_id").annotate(rev=Sum(F("unit_price") * F("quantity")), q=Sum("quantity"))}
        weeks = days // 7
        weekly = {}
        for r in base.annotate(w=TruncWeek("sale__created_at")).values("product_id", "w").annotate(q=Sum("quantity")):
            weekly.setdefault(r["product_id"], []).append(float(r["q"] or 0))
        total = sum(v[0] for v in rev.values()) or Decimal("1")
        order = sorted(rev.items(), key=lambda kv: kv[1][0], reverse=True)
        abc, acc = {}, Decimal("0")
        for pid, (r, _) in order:
            share = acc / total * 100  # доля выручки ДО этого товара: самый крупный всегда A
            abc[pid] = "A" if share < 80 else ("B" if share < 95 else "C")
            acc += r
        prods = Product.objects.filter(company=company).exclude(kind="service").exclude(is_test=True)
        out = []
        for p in prods.only("id", "name", "quantity").iterator(chunk_size=2000):
            r, q = rev.get(p.id, (Decimal("0"), Decimal("0")))
            w = weekly.get(p.id, [])
            w = w + [0.0] * max(0, weeks - len(w))
            mean = sum(w) / len(w) if w else 0.0
            cv = (math.sqrt(sum((x - mean) ** 2 for x in w) / len(w)) / mean) if mean > 0 else None
            xyz = "Z" if cv is None else ("X" if cv < 0.25 else ("Y" if cv < 0.5 else "Z"))
            per_day = q / days if q else Decimal("0")
            stock = Decimal(str(p.quantity or 0))
            out.append({"product_id": str(p.id), "name": p.name, "abc": abc.get(p.id, "C"), "xyz": xyz,
                        "class": abc.get(p.id, "C") + xyz, "revenue": format(r.quantize(Q2), "f"), "sold_qty": format(q, "f"),
                        "variation": round(cv, 3) if cv is not None else None, "quantity": format(stock, "f"),
                        "days_of_supply": (float((stock / per_day).quantize(Decimal("0.1"))) if per_day > 0 else None),
                        "dead_stock": bool(stock > 0 and q == 0)})
        want = (qp.get("class") or "").upper()
        if want:
            out = [x for x in out if x["class"].startswith(want) or x["class"] == want]
        if qp.get("dead") in ("1", "true"):
            out = [x for x in out if x["dead_stock"]]
        out.sort(key=lambda x: (x["class"], -float(x["revenue"])))
        counts = {}
        for x in out:
            counts[x["class"]] = counts.get(x["class"], 0) + 1
        return Response({"days": days, "count": len(out), "classes": counts, "page": page, "page_size": size,
                         "results": out[(page - 1) * size: page * size]})
