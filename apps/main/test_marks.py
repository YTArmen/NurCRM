"""П0-4 К-18, К-19 (14.11.2026, k17_1411).

К-18 тестовые данные: is_test у товара, поставщика/клиента и чека.
  POST test-marks/ {"entity": "product"|"client"|"sale", "ids": [...], "is_test": true} — владелец/админ.
  products/bulk-update/ и обычный PATCH тоже принимают is_test. Отчёт analytics/summary/ не считает is_test.
К-19 закупочные цены: User.can_see_purchase_price = False -> в товарах, чеках, журнале и документах склада
  не отдаются purchase_price, markup_percent, cost_price, cogs, unit_cost, landed_price (null). NULL — как раньше.
"""
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

HIDE = ("purchase_price", "markup_percent", "cost_price", "cogs", "unit_cost", "landed_price", "purchase_price_snapshot",
        "total_purchase", "margin", "profit")


def hides_cost(request) -> bool:
    user = getattr(request, "user", None) if request is not None else None
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "can_see_purchase_price", None) is not False:
        return False
    from apps.main.pos_serializers import _is_owner_like

    return not _is_owner_like(user)


def strip_cost(data):
    if isinstance(data, dict):
        for k in HIDE:
            if k in data:
                data[k] = None
        for v in data.values():
            if isinstance(v, (list, dict)):
                strip_cost(v)
    elif isinstance(data, list):
        for v in data:
            strip_cost(v)
    return data


def wrap_cost(cls):
    orig = cls.to_representation

    def to_representation(self, obj):
        data = orig(self, obj)
        if hides_cost(self.context.get("request")):
            strip_cost(data)
        return data

    cls.to_representation = to_representation


class TestMarksAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.main.models import Client, Product, Sale
        from apps.main.pos_serializers import _is_owner_like

        if not _is_owner_like(request.user):
            return Response({"code": "permission_denied", "detail": "Размечать тестовые данные может владелец.",
                             "permission": "owner"}, status=403)
        data = request.data if hasattr(request.data, "get") else {}
        model = {"product": Product, "client": Client, "supplier": Client, "sale": Sale}.get(str(data.get("entity") or ""))
        ids = data.get("ids")
        if model is None or not isinstance(ids, list) or not ids or len(ids) > 5000:
            return Response({"code": "validation_error", "detail": "entity: product|client|sale, ids: 1–5000 id."}, status=400)
        company = getattr(request.user, "owned_company", None) or getattr(request.user, "company", None)
        val = data.get("is_test", True) in (True, "true", "1", 1)
        try:
            n = model.objects.filter(company=company, id__in=[str(x) for x in ids]).update(is_test=val)
        except Exception:  # noqa: BLE001 — неверный uuid
            return Response({"code": "validation_error", "detail": "Неверные id."}, status=400)
        return Response({"updated": n, "is_test": val})
