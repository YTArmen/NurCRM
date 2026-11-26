"""
Отправка оплаченной продажи (main.Sale) в eKassa после commit транзакции.

Вызывается из Sale.mark_paid() через schedule_after_commit (фон после commit) или синхронно
при запросе печати (GET receipt с wait_ekassa).
"""
from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal

from apps.ekassa.shift_guard import send_receipt
from apps.ekassa.client import EkassaHttpClient
from apps.ekassa.exceptions import EkassaAPIError
from apps.ekassa.services import get_integration

logger = logging.getLogger(__name__)


def _som_to_tyiyun_int(d: Decimal) -> int:
    return int((d * Decimal("100")).quantize(Decimal("1")))


def _merge_ekassa_meta(sale_id, patch: dict) -> None:
    from apps.main.models import Sale

    s = Sale.objects.filter(pk=sale_id).only("ekassa_fiscal").first()
    if not s:
        return
    m = dict(s.ekassa_fiscal or {})
    m.update(patch)
    Sale.objects.filter(pk=sale_id).update(ekassa_fiscal=m)


# ek1411: порядок позиций = порядок пробития. У SaleItem uuid-ключ, поэтому порядок держим
# в колонке ekassa_seq (bigint, DEFAULT nextval) — её ставит deploy SQL, в модели её нет.
def _ordered_items(sale) -> list:
    qs = sale.items.select_related("product").all()
    try:
        return list(qs.extra(select={"_ek_seq": "main_saleitem.ekassa_seq"}, order_by=["_ek_seq", "id"]))
    except Exception:  # колонки нет (локальная база) — как раньше
        return list(qs.order_by("id"))


def _num(v):
    """Ставка для eKassa: целое число процентов, если без дробной части."""
    try:
        d = Decimal(str(v))
    except Exception:
        return 0
    if d < 0:
        return 0
    return int(d) if d == d.to_integral_value() else float(d)


def _kkm_defaults(cfg) -> dict:
    """Ставки кассы по умолчанию из eKassa (get_pos_by_fiscal_number), кэш 6 ч."""
    from django.core.cache import cache

    key = f"ekassa:kkm_tax:{cfg.company_id}:{cfg.fiscal_number.strip()}"
    got = cache.get(key)
    if isinstance(got, dict):
        return got
    out = {}
    try:
        resp = EkassaHttpClient(cfg).request_json(
            "POST", "/api/get_pos_by_fiscal_number", json_body={"fiscal_number": cfg.fiscal_number.strip()})
        d = resp.get("data") or {}
        out = {
            "vat": _num(d.get("set_vat")) if str(d.get("is_vat") or "0") not in ("0", "False", "false") else 0,
            "st_cash": _num(d.get("set_st_c") or 0),
            "st_card": _num(d.get("set_st_b") or 0),
        }
        cache.set(key, out, 6 * 3600)
    except Exception as e:  # noqa: BLE001 — без ставок кассы чек всё равно уйдёт с настройками компании / 0
        logger.warning("eKassa get_pos_by_fiscal_number failed company=%s: %s", cfg.company_id, e)
        cache.set(key, {}, 300)
    return out


def _tax_rates(company, cfg, cash: bool) -> dict:
    """
    Ставки по умолчанию для чека: настройки компании (settings/fiscal/), иначе ставки кассы в eKassa, иначе 0.
    НДС товара (Product.vat_rate) важнее — см. _apply_taxes.
    """
    st_row = None
    try:
        from apps.main.models import CompanyFiscalSettings

        st_row = CompanyFiscalSettings.objects.filter(company=company).first()
    except Exception:  # noqa: BLE001
        st_row = None
    kkm = None
    eff = None
    if st_row is not None:
        from apps.main.fiscal_kassa import effective_taxes  # p0114: те же правила, что у кассы

        eff = effective_taxes(st_row)
    product_vat = True
    if st_row is not None and st_row.vat_payer is not None and (st_row.vat_payer or st_row.tax_regime):
        vat = _num(eff["vat_rate"])
        product_vat = eff["product_vat_allowed"]
    else:
        kkm = _kkm_defaults(cfg)
        vat = kkm.get("vat", 0)
    zero_nsp_regime = st_row is not None and st_row.tax_regime in ("single_tax", "patent")
    if st_row is not None and (st_row.nsp_rate is not None or st_row.nsp_rate_cashless is not None or zero_nsp_regime):
        st = _num(eff["nsp_cash"] if cash else eff["nsp_cashless"])
    else:
        kkm = kkm if kkm is not None else _kkm_defaults(cfg)
        st = kkm.get("st_cash" if cash else "st_card", 0)
    return {"vat": vat, "st": st, "product_vat": product_vat}


def _apply_taxes(good: dict, item, rates: dict) -> dict:
    prod = getattr(item, "product", None)
    pv = getattr(prod, "vat_rate", None) if prod is not None else None
    good["vat"] = _num(pv) if (pv is not None and rates.get("product_vat", True)) else rates.get("vat", 0)
    good["st"] = rates.get("st", 0)
    return good


def _sale_item_to_good(item) -> dict:
    from apps.main.models import Product

    prod = item.product
    unit = "шт."
    calc = 0
    code = "0"
    if prod:
        unit = (prod.unit or "шт.")[:32]
        calc = 1 if prod.kind == Product.Kind.SERVICE else 0
        code = (prod.barcode or prod.code or str(prod.id).replace("-", ""))[:64] or "0"
    qty = Decimal(str(item.quantity or 0))
    line_total = item.line_total
    if qty and qty != 0:
        eff = (line_total / qty).quantize(Decimal("0.01"))
    else:
        eff = Decimal("0")
    price_ty = _som_to_tyiyun_int(eff)
    return {
        "item_id": str(item.id),
        "calcItemAttributeCode": calc,
        "name": (item.name_snapshot or "Позиция")[:255],
        "sgtin": str(code),
        "price": price_ty,
        "quantity": float(qty),
        "unit": unit or "шт.",
        "st": 0,
        "vat": 0,
    }


def enrich_ekassa_fiscal_with_item_ids(ekassa_fiscal: dict, sale) -> dict:
    """
    Гарантирует наличие стабильного item_id / line_id / sale_item_id у каждого элемента тега 1059
    (позиции фискального чека), чтобы фронт мог безошибочно сопоставить построчные скидки с позициями.
    """
    if not isinstance(ekassa_fiscal, dict):
        return ekassa_fiscal
    fields = ekassa_fiscal.get("fields")
    if not isinstance(fields, dict):
        return ekassa_fiscal

    tag_1059 = fields.get("1059")
    if not isinstance(tag_1059, list) or not tag_1059:
        return ekassa_fiscal

    ekassa_copy = dict(ekassa_fiscal)
    fields_copy = dict(fields)
    new_1059 = []

    items = _ordered_items(sale) if hasattr(sale, "items") else []
    for idx, pos in enumerate(tag_1059):
        if isinstance(pos, dict):
            pos_copy = dict(pos)
            if idx < len(items):
                item_id = str(items[idx].id)
                pos_copy.setdefault("item_id", item_id)
                pos_copy.setdefault("line_id", item_id)
                pos_copy.setdefault("sale_item_id", item_id)
                pos_copy.setdefault("id", item_id)
            new_1059.append(pos_copy)
        else:
            new_1059.append(pos)

    fields_copy["1059"] = new_1059
    ekassa_copy["fields"] = fields_copy
    return ekassa_copy


def try_fiscalize_pos_sale(sale_id) -> None:
    from apps.main.models import Sale

    sale = (
        Sale.objects.filter(pk=sale_id)
        .select_related("company", "client")
        .prefetch_related("items__product", "payments")
        .first()
    )
    if not sale:
        return

    if sale.status != Sale.Status.PAID:
        return
    if (getattr(sale, "fiscal_status", None) or "none") != "none":  # П0-1: чек пробила касса
        return

    meta = sale.ekassa_fiscal or {}
    if meta.get("fd_number") is not None or meta.get("status") == "ok":
        return

    cfg = get_integration(sale.company)
    if cfg is None or not cfg.is_ready():
        return

    items = _ordered_items(sale)
    if not items:
        return

    newid = meta.get("newid") or str(uuid.uuid4())
    _merge_ekassa_meta(sale_id, {"status": "pending", "newid": newid})

    cash_amount = sale.cash_payment_amount() if hasattr(sale, "cash_payment_amount") else Decimal("0.00")
    if not cash_amount and sale.payment_method == Sale.PaymentMethod.CASH:
        cash_amount = Decimal(str(sale.total or 0))

    is_cash = cash_amount >= Decimal(str(sale.total or 0)) and sale.noncash_payment_amount() <= 0
    rates = _tax_rates(sale.company, cfg, is_cash)
    goods = [_apply_taxes(_sale_item_to_good(it), it, rates) for it in items]
    body = {
        "fiscal_number": cfg.fiscal_number.strip(),
        "newid": newid,
        "operation": "INCOME",
        "cash": is_cash,
        "goods": goods,
        # Готовый фискальный чек от eKassa (все реквизиты ГНС), 32 символа в строке — под ленту 58/80 мм.
        "txt": True,
        "length": 32,
    }

    # В goods цена строки уже нетто (line_total / qty), т.е. построчные скидки в неё зашиты.
    # Фискально скидкой отдаём только скидку на чек, иначе строчная вычитается второй раз.
    line_disc_sum = sum((Decimal(str(it.line_discount or 0)) for it in items), Decimal("0"))
    order_disc = Decimal(str(sale.discount_total or 0)) - line_disc_sum
    disc_ty = _som_to_tyiyun_int(order_disc) if order_disc > 0 else 0
    if disc_ty > 0:
        body["discount"] = str(disc_ty)

    if cash_amount > 0:
        received = sale.cash_received if sale.cash_received else cash_amount
        body["received"] = str(_som_to_tyiyun_int(Decimal(str(received))))

    client = sale.client
    if client is not None:
        email = (getattr(client, "email", None) or "").strip()
        if email:
            body["customerContact"] = email

    try:
        resp = send_receipt(cfg, body)  # sg1811: сторож смены eKassa (старая/закрытая смена)
    except EkassaAPIError as e:
        logger.warning("eKassa receipt failed sale_id=%s: %s", sale_id, e, exc_info=False)
        err_payload = e.payload
        if isinstance(err_payload, dict) and len(str(err_payload)) > 8000:
            err_payload = {"detail": str(err_payload.get("message", ""))[:2000]}
        _merge_ekassa_meta(
            sale_id,
            {
                "status": "error",
                "newid": newid,
                "message": str(e),
                "ekassa_payload": err_payload,
            },
        )
        return
    except Exception as e:
        logger.exception("eKassa receipt unexpected error sale_id=%s", sale_id)
        _merge_ekassa_meta(
            sale_id,
            {"status": "error", "newid": newid, "message": str(e)},
        )
        return

    data = resp.get("data") or {}
    fields = data.get("fields") or {}
    fd = fields.get("1040")
    fd_int = None
    if fd is not None:
        try:
            fd_int = int(fd)
        except (TypeError, ValueError):
            fd_int = None

    # Гарантируем стабильный item_id в позициях 1059
    tag_1059 = fields.get("1059")
    if isinstance(tag_1059, list):
        for idx, pos in enumerate(tag_1059):
            if isinstance(pos, dict) and idx < len(items):
                item_id = str(items[idx].id)
                pos.setdefault("item_id", item_id)
                pos.setdefault("line_id", item_id)
                pos.setdefault("sale_item_id", item_id)
                pos.setdefault("id", item_id)

    # Ключевые реквизиты для печати (см. PDF «Интеграция_1.14», раздел «Отправка чека»):
    # 1037 — РН ККМ, 1041 — ФМ, 1040 — ФД, 1077 — ФПД; link — ссылка для проверки (QR)
    kkm_reg_number = fields.get("1037")
    fm_number = fields.get("1041")
    fpd = fields.get("1077")
    link = data.get("link")

    _merge_ekassa_meta(
        sale_id,
        {
            "status": "ok",
            "newid": newid,
            "fd_number": fd_int,
            "ekassa_receipt_id": data.get("id"),
            "fields": fields,
            "kkm_reg_number": kkm_reg_number,
            "fm_number": fm_number,
            "fpd": fpd,
            "link": link,
            "receipt_txt": data.get("txt") or None,
            "message": resp.get("message"),
        },
    )


def wait_for_pos_sale_ekassa(sale_id, *, timeout_sec: float = 45.0, poll_sec: float = 0.2) -> None:
    """
    Ждёт появления терминального состояния фискализации в Sale.ekassa_fiscal (фон после mark_paid
    или повторный вызов try_fiscalize по таймауту).
    """
    from apps.main.models import Sale

    sale = Sale.objects.filter(pk=sale_id).select_related("company").first()
    if not sale:
        return
    if sale.status != Sale.Status.PAID or sale.payment_method == Sale.PaymentMethod.DEBT:
        return
    cfg = get_integration(sale.company)
    if cfg is None or not cfg.is_ready():
        return

    deadline = time.monotonic() + float(timeout_sec)
    while time.monotonic() < deadline:
        row = Sale.objects.filter(pk=sale_id).only("ekassa_fiscal", "status", "payment_method").first()
        if not row or row.status != Sale.Status.PAID or row.payment_method == Sale.PaymentMethod.DEBT:
            return
        meta = row.ekassa_fiscal or {}
        if meta.get("fd_number") is not None or meta.get("status") == "ok":
            return
        if meta.get("status") == "error":
            return
        time.sleep(float(poll_sec))

    try_fiscalize_pos_sale(sale_id)


# ---------------------------------------------------------------------------
# Возвраты (INCOME_RETURN) и повторная отправка
# ---------------------------------------------------------------------------

def _merge_return_meta(sale_id, return_id, patch: dict) -> None:
    """Данные фискализации возврата лежат в Sale.ekassa_fiscal["returns"][<id возврата>] (без миграции)."""
    from apps.main.models import Sale

    s = Sale.objects.filter(pk=sale_id).only("ekassa_fiscal").first()
    if not s:
        return
    m = dict(s.ekassa_fiscal or {})
    rets = dict(m.get("returns") or {})
    cur = dict(rets.get(str(return_id)) or {})
    cur.update(patch)
    rets[str(return_id)] = cur
    m["returns"] = rets
    Sale.objects.filter(pk=sale_id).update(ekassa_fiscal=m)


def _return_line_to_good(line: dict, item) -> dict | None:
    qty = Decimal(str(line.get("qty") or 0))
    if qty <= 0:
        return None
    good = _sale_item_to_good(item)
    total = Decimal(str(line.get("total") or line.get("amount") or 0))
    # цена за единицу с учётом скидок (как в чеке продажи)
    good["price"] = _som_to_tyiyun_int((total / qty).quantize(Decimal("0.01")))
    good["quantity"] = float(qty)
    return good


def try_fiscalize_sale_return(sale_id, return_id) -> None:
    """
    Чек возврата в eKassa (INCOME_RETURN + originFdNumber). Только если исходная продажа уже
    фискализирована и интеграция компании включена. Ошибка не ломает возврат в CRM — статус
    остаётся в Sale.ekassa_fiscal["returns"], повтор — POST /api/ekassa/retry/.
    """
    from apps.main.models import Sale, SaleReturn

    sale = Sale.objects.filter(pk=sale_id).select_related("company").prefetch_related("items__product").first()
    ret = SaleReturn.objects.filter(pk=return_id, sale_id=sale_id).first()
    if not sale or not ret:
        return
    meta = sale.ekassa_fiscal or {}
    if ret.status == "voided":
        # ek1411: аннулированный до отправки возврат больше не пытаемся пробить (retry его пропустит)
        cur = (meta.get("returns") or {}).get(str(return_id))
        if cur and cur.get("status") != "ok":
            _merge_return_meta(sale_id, return_id, {"status": "voided"})
        return
    origin = meta.get("fd_number")
    if meta.get("status") != "ok" or origin is None:
        return  # исходный чек не пробивали через eKassa — возвращать там нечего
    done = (meta.get("returns") or {}).get(str(return_id)) or {}
    if done.get("status") == "ok":
        return
    cfg = get_integration(sale.company)
    if cfg is None or not cfg.is_ready():
        return

    is_cash = Decimal(str(ret.refund_noncash or 0)) <= 0
    rates = _tax_rates(sale.company, cfg, is_cash)
    ordered = _ordered_items(sale)
    rank = {str(i.id): n for n, i in enumerate(ordered)}
    items = {str(i.id): i for i in ordered}
    goods = []
    lines = sorted(ret.returned_items or [], key=lambda ln: rank.get(str(ln.get("sale_item")), 10 ** 9))
    for line in lines:
        it = items.get(str(line.get("sale_item")))
        g = _return_line_to_good(line, it) if it is not None else None
        if g:
            goods.append(_apply_taxes(g, it, rates))
    if not goods:
        return

    newid = done.get("newid") or str(uuid.uuid4())
    _merge_return_meta(sale_id, return_id, {"status": "pending", "newid": newid, "origin_fd_number": origin})
    body = {
        "fiscal_number": cfg.fiscal_number.strip(),
        "newid": newid,
        "operation": "INCOME_RETURN",
        "originFdNumber": int(origin),
        "cash": is_cash,
        "goods": goods,
        "txt": True,
        "length": 32,
    }
    try:
        resp = send_receipt(cfg, body)  # sg1811
    except EkassaAPIError as e:
        logger.warning("eKassa return failed sale=%s return=%s: %s", sale_id, return_id, e)
        payload = e.payload
        if isinstance(payload, dict) and len(str(payload)) > 8000:
            payload = {"detail": str(payload.get("message", ""))[:2000]}
        _merge_return_meta(sale_id, return_id, {"status": "error", "message": str(e), "ekassa_payload": payload})
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("eKassa return unexpected error sale=%s return=%s", sale_id, return_id)
        _merge_return_meta(sale_id, return_id, {"status": "error", "message": str(e)})
        return

    data = resp.get("data") or {}
    fields = data.get("fields") or {}
    try:
        fd_int = int(fields.get("1040"))
    except (TypeError, ValueError):
        fd_int = None
    _merge_return_meta(
        sale_id,
        return_id,
        {
            "status": "ok",
            "fd_number": fd_int,
            "ekassa_receipt_id": data.get("id"),
            "kkm_reg_number": fields.get("1037"),
            "fm_number": fields.get("1041"),
            "fpd": fields.get("1077"),
            "link": data.get("link"),
            "receipt_txt": data.get("txt") or None,
            "message": resp.get("message"),
        },
    )


def retry_failed_for_company(company, *, limit: int = 50) -> dict:
    """
    Повторно отправляет чеки продаж и возвратов, которые не дошли до eKassa (status error/pending).
    Продажи — сначала (возврату нужен номер ФД продажи). Возвращает счётчики.
    """
    from apps.main.models import Sale

    cfg = get_integration(company)
    if cfg is None or not cfg.is_ready():
        return {"enabled": False, "sales": 0, "returns": 0, "failed": 0}
    sales_done = returns_done = failed = 0
    qs = (
        Sale.objects.filter(company=company, status__in=[Sale.Status.PAID, "partially_returned", "canceled"])
        .exclude(ekassa_fiscal__isnull=True)
        .order_by("-created_at")[:500]
    )
    for sale in qs:
        meta = sale.ekassa_fiscal or {}
        if meta.get("status") in ("error", "pending") and sales_done + returns_done < limit:
            try_fiscalize_pos_sale(sale.pk)
            sale.refresh_from_db(fields=["ekassa_fiscal"])
            if (sale.ekassa_fiscal or {}).get("status") == "ok":
                sales_done += 1
            else:
                failed += 1
        meta = sale.ekassa_fiscal or {}
        for rid, rmeta in list((meta.get("returns") or {}).items()):
            if rmeta.get("status") in ("error", "pending") and sales_done + returns_done < limit:
                try_fiscalize_sale_return(sale.pk, rid)
                sale.refresh_from_db(fields=["ekassa_fiscal"])
                if ((sale.ekassa_fiscal or {}).get("returns") or {}).get(rid, {}).get("status") == "ok":
                    returns_done += 1
                else:
                    failed += 1
    return {"enabled": True, "sales": sales_done, "returns": returns_done, "failed": failed}
