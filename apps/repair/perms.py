"""Права ремонта и trade-in (решение владельца 15.11, п.7)."""
from rest_framework.response import Response

ALL = ("repair.view", "repair.manage", "repair.refund", "trade_in.manage", "trade_in.view_documents")
ADDON = {"repair": "kassa-repair", "trade_in": "kassa-trade_in"}


def company_of(user):
    return getattr(user, "owned_company", None) or getattr(user, "company", None)


def err(code, detail, http=400, **extra):
    return Response({"code": code, "detail": detail, **extra}, status=http)


def privileged(user):
    from apps.users.permissions_flags import is_privileged

    return is_privileged(user)


def perms_of(user):
    """Владелец/админ — всё. Есть запись — она. Нет записи — кассиру (can_use_pos) ремонт смотреть/оформлять
    и trade-in оформлять; возврат предоплаты и паспорт — только по явной выдаче."""
    if privileged(user):
        return set(ALL)
    from apps.repair.models import RepairStaffPerms

    rec = RepairStaffPerms.objects.filter(user=user).first()
    if rec is not None:
        return set(p for p in (rec.perms or []) if p in ALL)
    if getattr(user, "can_use_pos", True) is not False:
        return {"repair.view", "repair.manage", "trade_in.manage"}
    return set()


def addon_on(company, feature):
    from apps.users.models import CompanyAddon

    a = CompanyAddon.objects.filter(company=company, code=ADDON[feature]).first()
    return bool(a and a.is_effective())


def guard(request, feature, perm):
    """None — можно; иначе Response 403."""
    company = company_of(request.user)
    if company is None:
        return err("no_company", "Нет компании.", 403)
    if not addon_on(company, feature):
        return err("feature_off", "Функция не подключена. Включить можно в «Маркетплейсе» или через поддержку.", 403,
                   feature=feature, addon=ADDON[feature])
    if perm and perm not in perms_of(request.user):
        return err("permission_denied", "Нет права на это действие.", 403, permission=perm, required=perm)
    return None


def pin_ok(request):
    from apps.users.pin import HEADER, token_is_valid

    tok = request.META.get(HEADER, "")
    return bool(tok and token_is_valid(tok, request.user))
