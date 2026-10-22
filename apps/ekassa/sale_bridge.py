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

    items = list(sale.items.all().order_by("id")) if hasattr(sale, "items") else []
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

    meta = sale.ekassa_fiscal or {}
    if meta.get("fd_number") is not None or meta.get("status") == "ok":
        return

    cfg = get_integration(sale.company)
    if cfg is None or not cfg.is_ready():
        return

    items = list(sale.items.all().order_by("id"))
    if not items:
        return

    goods = [_sale_item_to_good(it) for it in items]

    newid = meta.get("newid") or str(uuid.uuid4())
    _merge_ekassa_meta(sale_id, {"status": "pending", "newid": newid})

    cash_amount = sale.cash_payment_amount() if hasattr(sale, "cash_payment_amount") else Decimal("0.00")
    if not cash_amount and sale.payment_method == Sale.PaymentMethod.CASH:
        cash_amount = Decimal(str(sale.total or 0))

    body = {
        "fiscal_number": cfg.fiscal_number.strip(),
        "newid": newid,
        "operation": "INCOME",
        "cash": cash_amount >= Decimal(str(sale.total or 0)) and sale.noncash_payment_amount() <= 0,
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
        cli = EkassaHttpClient(cfg)
        resp = cli.request_json("POST", "/api/v2/receipt", json_body=body)
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
