import html
import json
import logging
import re
import uuid
from decimal import Decimal
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Max, Prefetch, Q, Sum
from django.utils import timezone

from apps.main.telegram_bot.services import telegram_api, ai_service

logger = logging.getLogger("telegram_bot.customer")

def extract_order_json(text: str) -> tuple:
    """
    Извлекает JSON из служебной строки ЗАКАЗ: {...} с корректной обработкой вложенных скобок.
    Возвращает (order_dict, clean_text_without_order_json).
    """
    marker = "ЗАКАЗ:"
    if marker not in text:
        return None, text

    idx = text.find(marker)
    prefix = text[:idx].strip()
    after = text[idx + len(marker):].strip()

    start_brace = after.find("{")
    if start_brace == -1:
        return None, text

    depth = 0
    end_brace = -1
    for i, ch in enumerate(after[start_brace:], start=start_brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_brace = i
                break

    if end_brace == -1:
        return None, text

    json_str = after[start_brace:end_brace + 1]
    remaining = (prefix + "\n" + after[end_brace + 1:]).strip()

    try:
        data = json.loads(json_str)
        return data, remaining
    except Exception as exc:
        logger.warning("extract_order_json failed: %s (raw: %s)", exc, json_str)
        return None, text


def is_valid_store_info(val: str) -> bool:
    """ТЗ-09 п. 1.6: Отсекает пустые и тестовые поля магазина ('1234', 'не указан' и т.п.)."""
    if not val:
        return False
    clean = str(val).strip().lower()
    if clean in ("1234", "123", "test", "тест", "не указан", "нет", "none", "null", "адрес уточняйте у менеджера"):
        return False
    if len(clean) < 3:
        return False
    return True


def get_customer_main_menu_keyboard(cart_count: int = 0, lang: str = "ru"):
    """ТЗ-09 п. 2.1: Постоянное меню покупателя внизу экрана."""
    is_ky = (lang == "ky")
    cart_label = f"🧺 Себет ({cart_count})" if is_ky else f"🧺 Корзина ({cart_count})" if cart_count > 0 else ("🧺 Себет" if is_ky else "🧺 Корзина")
    return {
        "keyboard": [
            [
                {"text": "🛍 Каталог"},
                {"text": "🔎 Товар издөө" if is_ky else "🔎 Найти товар"},
            ],
            [
                {"text": cart_label},
                {"text": "📦 Менин буйрутмаларым" if is_ky else "📦 Мои заказы"},
            ],
            [
                {"text": "📍 Дарек жана убакыт" if is_ky else "📍 Адрес и время"},
                {"text": "📞 Байланышуу" if is_ky else "📞 Связаться"},
            ],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def build_scenarios_context(company) -> str:
    """ТЗ-11 п. 1.5: Добавляет сценарии магазина в системную подсказку ИИ-консультанта."""
    from apps.main.telegram_bot.models import TelegramBotScenario

    scenarios = TelegramBotScenario.objects.filter(
        company=company,
        is_active=True,
        audience__in=[TelegramBotScenario.Audience.CUSTOMERS, TelegramBotScenario.Audience.ALL],
    ).order_by("-priority", "title")[:30]

    if not scenarios:
        return ""

    lines = ["ОТВЕТЫ МАГАЗИНА (используй их, если вопрос о том же):"]
    total_len = len(lines[0])
    for sc in scenarios:
        clean_reply = re.sub(r"<[^>]+>", "", sc.reply_text or "").strip()
        line = f"• {sc.title}: {clean_reply}"
        if total_len + len(line) + 1 > 4000:
            break
        lines.append(line)
        total_len += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


def check_rate_limit(company_id, chat_id: str, limit_per_hour: int) -> bool:
    """Проверяет лимит сообщений в час через Redis."""
    cache_key = f"tg_cust_rate:{company_id}:{chat_id}"
    try:
        cnt = cache.get(cache_key)
        if cnt is None:
            cache.set(cache_key, 1, timeout=3600)
            return True
        if int(cnt) >= limit_per_hour:
            return False
        cache.incr(cache_key)
        return True
    except Exception:
        return True


def get_customer_debt(company, chat_id: str) -> str:
    """Вычисляет текущий долг покупателя по привязанному telegram_chat_id."""
    from apps.main.models import Client, Sale

    clients = list(Client.objects.filter(company=company, telegram_chat_id=str(chat_id)))
    if not clients:
        return (
            "Ваш Telegram не привязан к профилю клиента.\n"
            "Чтобы узнавать свой долг, перейдите по персональной ссылке от магазина или сообщите свой Telegram кассиру."
        )

    total_debt = Decimal("0.00")
    for client in clients:
        debt_sum = (
            Sale.objects.filter(
                company=company,
                client=client,
            ).aggregate(s=Sum("debt_remaining"))["s"]
            or Decimal("0.00")
        )
        total_debt += debt_sum

    company_name = getattr(company, "name", "нашем магазине")
    if total_debt > Decimal("0.00"):
        return f"Ваш текущий долг в магазине «{company_name}»: <b>{format_amount(total_debt)} сом</b>."
    else:
        return f"В магазине «{company_name}» у вас нет задолженности. Спасибо, что вы с нами!"


def link_client_by_start(company, chat_id: str, client_id_str: str) -> str:
    """Привязывает Client к telegram_chat_id при команде /start <client_id>."""
    from apps.main.models import Client

    clean_id = client_id_str.strip()
    client = None
    try:
        client = Client.objects.filter(company=company, id=clean_id).first()
    except Exception:
        pass

    if not client:
        # Попробуем по номеру телефона
        client = Client.objects.filter(company=company, phone__icontains=clean_id).first()

    if client:
        client.telegram_chat_id = str(chat_id)
        client.save(update_fields=["telegram_chat_id"])
        company_name = getattr(company, "name", "")
        return f"✅ Вы успешно подписались на уведомления магазина {company_name}! Теперь вы можете проверять долг командой /dolg."

    return "Добро пожаловать! Чем мы можем вам помочь? Напишите интересующий вас товар."


CATALOG_CACHE_TTL = 180  # 3 минуты на компанию (ТЗ ч.7, 4.1.5)
CATALOG_MAX_PRODUCTS = 3000
CUSTOMER_HISTORY_TTL = 86400
CUSTOMER_HISTORY_LEN = 8


def _catalog_cache_key(company_id) -> str:
    return f"tg_catalog_data:{company_id}"


def get_catalog_data(company) -> list:
    """
    Данные каталога для ИИ-консультанта с вариантами (размер, цвет, есть/нет, акционная цена).
    Один запрос товаров + один запрос вариантов (prefetch), кэш на компанию 180 сек.
    Точных остатков в данных нет — только признак наличия.
    """
    from apps.main.models import Product, ProductVariant
    from apps.main.variant_utils import sort_variants, variant_prices

    key = _catalog_cache_key(company.id)
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    qs = (
        Product.objects.filter(company=company)
        .exclude(status=Product.Status.ARCHIVED)
        .only("id", "name", "price", "quantity", "kind", "updated_at")
        .prefetch_related(Prefetch("variants", queryset=ProductVariant.objects.filter(is_active=True)))
        .order_by("-updated_at")[:CATALOG_MAX_PRODUCTS]
    )
    data = []
    for p in qs:
        variants = []
        for v in sort_variants(p.variants.all()):
            price, old_price = variant_prices(v, p)
            variants.append({
                "id": str(v.id),
                "size": v.size or "",
                "color": v.color or "",
                "in_stock": (v.quantity or 0) > 0,
                "price": str(price),
                "old_price": str(old_price) if old_price is not None else None,
            })
        in_stock = (p.quantity or 0) > 0 or p.kind == Product.Kind.SERVICE
        if variants:
            in_stock = any(v["in_stock"] for v in variants)
        data.append({
            "id": str(p.id),
            "name": p.name,
            "name_l": (p.name or "").lower(),
            "price": str(p.price or 0),
            "in_stock": in_stock,
            "variants": variants,
        })
    try:
        cache.set(key, data, timeout=CATALOG_CACHE_TTL)
    except Exception:
        pass
    return data


def _match_catalog(data: list, user_text: str, limit: int) -> list:
    words = [w for w in re.split(r"[^\w]+", (user_text or "").lower()) if len(w) >= 2][:6]
    if not words:
        return []
    name_words = [w for w in words if len(w) >= 3]
    scored = []
    for item in data:
        score = 0
        for w in name_words:
            if w in item["name_l"]:
                score += 2
        if score == 0:
            continue
        for v in item["variants"]:
            if any(w == v["size"].lower() or (len(w) >= 3 and w in v["color"].lower()) for w in words):
                score += 1
                break
        scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], not x[1]["in_stock"]))
    return [it for _, it in scored[:limit]]


from apps.main.telegram_bot.services.photo_service import format_amount, format_price_display, format_qty

def _fmt_price(val) -> str:
    return format_price_display(val)


def _catalog_line(item: dict) -> str:
    """Формат строки каталога для ИИ по ТЗ ч. 10 п. 3.1."""
    from apps.main.variant_utils import size_sort_key

    status_stock = "есть в наличии" if item["in_stock"] else "нет в наличии"
    price_str = format_price_display(item["price"])
    if not item["variants"]:
        return f"- [id={item['id']}] {item['name']}: {price_str}, {status_stock}."

    sizes_dict = {}
    sales_promos = []

    for v in item["variants"]:
        s = (v.get("size") or "").strip() or "Стандарт"
        c = (v.get("color") or "").strip()
        in_st = v.get("in_stock", False)
        if s not in sizes_dict:
            sizes_dict[s] = {"in_stock": [], "out_of_stock": []}
        if c:
            if in_st:
                sizes_dict[s]["in_stock"].append(c.lower())
            else:
                sizes_dict[s]["out_of_stock"].append(c.lower())
        else:
            if in_st:
                sizes_dict[s]["in_stock"].append("в наличии")
            else:
                sizes_dict[s]["out_of_stock"].append("нет в наличии")

        if v.get("old_price"):
            sales_promos.append(f"{s.lower()} {c.lower()} — {format_price_display(v['price'])} (обычная {format_price_display(v['old_price'])})".strip())

    sorted_sizes = sorted(sizes_dict.keys(), key=size_sort_key)
    size_parts = []
    for s in sorted_sizes:
        in_c = sizes_dict[s]["in_stock"]
        out_c = sizes_dict[s]["out_of_stock"]
        if in_c:
            part = f"{s}: {', '.join(in_c)}"
            if out_c and len(out_c) <= 3:
                part += f" (нет: {', '.join(out_c)})"
            size_parts.append(part)
        else:
            size_parts.append(f"{s}: нет в наличии")

    sizes_str = "; ".join(size_parts)
    line = f"- [id={item['id']}] {item['name']}: {price_str}, {status_stock}. Размеры и цвета — {sizes_str}."
    if sales_promos:
        line += f" Акция: {'; '.join(sales_promos)}."
    return line


SIZE_COLOR_KEYWORDS = (
    "размер", "размеры", "цвет", "цвета", "расцветка",
    "өлчөм", "түс", "түстөр", "size", "color", "beden",
    "renk", "o'lcham", "rang"
)


def build_catalog_context(company, user_text: str = "", recent_user_texts: list = None) -> tuple:
    """
    Сжатый каталог для ИИ покупателя с вариантами (размер, цвет, наличие «есть/нет», акционная цена).
    Возвращает (строка_каталога, список_найденных_товаров).
    ТЗ ч. 10 п. 3.1: если товар не назван в тексте, берётся из последних 3 сообщений покупателя.
    Если сфера «Одежда» или вопрос о размерах — даёт до 5 товаров в наличии с вариантами.
    """
    data = get_catalog_data(company)
    matched = _match_catalog(data, user_text, limit=10)

    # 1. Если товар не назван в текущем сообщении — ищем в последних 3 сообщениях
    if not matched and recent_user_texts:
        for prev_text in reversed(recent_user_texts[-3:]):
            matched = _match_catalog(data, prev_text, limit=5)
            if matched:
                break

    # 2. Если всё ещё не назван, а в вопросе слова о размере/цвете или сфера «Одежда»
    if not matched:
        is_clothing = (
            getattr(company, "market_sphere", "") == "clothing"
            or "clothing" in getattr(company, "market_spheres", [])
        )
        has_size_kw = any(kw in (user_text or "").lower() for kw in SIZE_COLOR_KEYWORDS)
        if is_clothing or has_size_kw:
            with_vars = [it for it in data if it["in_stock"] and it["variants"]][:5]
            matched = with_vars

    seen = {it["id"] for it in matched}
    general = [it for it in data if it["id"] not in seen and it["in_stock"]][: max(0, 30 - len(matched))]
    all_items = matched + general
    lines = [_catalog_line(it) for it in all_items]
    return ("\n".join(lines) or "Каталог формируется.", matched)


def fallback_catalog_search(company, settings, user_text: str) -> str:
    """Ответ по каталогу без ИИ, если ИИ недоступен или отключён."""
    phone = settings.owner_phone or getattr(company, "phone", "") or ""
    if not is_valid_store_info(phone):
        phone = ""

    try:
        matches = _match_catalog(get_catalog_data(company), user_text, limit=6)
    except Exception as exc:
        logger.warning("fallback catalog search failed: %s", exc)
        matches = []
    if matches:
        res = ["🔍 <b>Вот что мы нашли в нашем магазине:</b>"]
        for it in matches:
            status_stock = "в наличии" if it["in_stock"] else "нет в наличии"
            res.append(f"• <b>{html.escape(it['name'])}</b> — {format_price_display(it['price'])} ({status_stock})")
            in_stock_vars = [v for v in it["variants"] if v["in_stock"]]
            if in_stock_vars:
                v_lines = []
                for v in in_stock_vars:
                    desc = f"{v['size']} {v['color']}".strip() or "стандарт"
                    v_lines.append(f"{html.escape(desc)} — {format_price_display(v['price'])}")
                res.append(f"    <i>В наличии: {', '.join(v_lines)}</i>")
            elif it["variants"]:
                res.append("    <i>Все размеры сейчас закончились.</i>")
        if phone:
            res.append(f"\n📞 Для заказа или уточнения деталей звоните: {phone}")
        return "\n".join(res)

    company_name = getattr(company, "name", "наш магазин")
    base_msg = f"Здравствуйте! Вас приветствует магазин «{company_name}». Напишите, какой товар вас интересует."
    if phone:
        base_msg += f"\nПо всем вопросам вы также можете связаться с нами по номеру: {phone}."
    return base_msg


def _variants_hint(prod) -> str:
    from apps.main.variant_utils import active_variants, variant_label

    labels = [variant_label(v) for v in active_variants(prod) if (v.quantity or 0) > 0]
    return ", ".join(l for l in labels if l) or "сейчас нет размеров в наличии"


def _resolve_order_line(company, it: dict):
    """
    Находит товар и вариант для строки заказа из бота.
    Возвращает (product, variant, None) или (None, None, текст_вопроса_покупателю).
    """
    from apps.main.models import Product, ProductVariant

    title = str(it.get("title") or "").strip()
    variant_id = str(it.get("variant_id") or "").strip()
    size_hint = str(it.get("size") or "").strip()
    color_hint = str(it.get("color") or "").strip()

    variant = None
    prod = None
    if variant_id:
        try:
            variant = (
                ProductVariant.objects.filter(company=company, id=variant_id, is_active=True)
                .select_related("product")
                .first()
            )
        except (ValueError, DjangoValidationError):
            variant = None
        if variant is not None:
            prod = variant.product

    if prod is None:
        if not title:
            return None, None, "Уточните, пожалуйста, какой товар вы хотите заказать."
        base = Product.objects.filter(company=company).exclude(status=Product.Status.ARCHIVED)
        prod = base.filter(name__iexact=title).first() or base.filter(name__icontains=title).first()
        if prod is None:
            return None, None, (
                f"Не нашёл товар «{html.escape(title)}» в каталоге. Уточните, пожалуйста, название."
            )

    if variant is None:
        vqs = prod.variants.filter(is_active=True)
        if vqs.exists():
            cand = vqs
            if size_hint:
                cand = cand.filter(size__iexact=size_hint)
            if color_hint:
                cand = cand.filter(color__icontains=color_hint)
            cand = list(cand) if (size_hint or color_hint) else []
            in_stock = [v for v in cand if (v.quantity or 0) > 0]
            if len(in_stock) == 1 and (size_hint and color_hint or len(cand) == 1):
                variant = in_stock[0]
            elif len(cand) == 1 and size_hint and color_hint:
                variant = cand[0]
            else:
                return None, None, (
                    f"Уточните, пожалуйста, размер и цвет для «{html.escape(prod.name)}». "
                    f"В наличии: {html.escape(_variants_hint(prod))}."
                )

    if variant is not None and variant.product_id != prod.id:
        prod = variant.product
    return prod, variant, None


def create_order_from_ai_json(company, settings, order_data: dict, chat_id: str) -> tuple:
    """
    Создаёт заказ витрины (ShowcaseOrder) из служебной строки ЗАКАЗ: {...}.
    Возвращает (ShowcaseOrder или None, текст_для_покупателя).
    Если для товара с вариантами не указан размер/цвет — заказ НЕ создаётся, бот переспрашивает.
    Остаток резервируется так же, как в заказе с витрины (variant_utils.reserve_stock).
    """
    from apps.main.models import ShowcaseOrder, ShowcaseOrderItem
    from apps.main.variant_utils import InsufficientStock, name_with_variant, reserve_stock, variant_prices

    name = str(order_data.get("name") or "Покупатель Telegram").strip()
    phone = str(order_data.get("phone") or "").strip()
    comment = str(order_data.get("comment") or "").strip()
    items_raw = order_data.get("items") or []

    if not items_raw:
        return None, ""

    # 1. Разбор строк (вне транзакции): товар + вариант обязательны
    items_to_create = []
    total_amount = Decimal("0.00")
    for it in items_raw:
        if not isinstance(it, dict):
            continue
        try:
            qty = Decimal(str(it.get("qty") or 1))
        except Exception:
            qty = Decimal("1")
        if qty <= Decimal("0.00"):
            qty = Decimal("1")

        prod, variant, question = _resolve_order_line(company, it)
        if question:
            return None, question

        if variant is not None:
            price = variant_prices(variant, prod)[0]
        else:
            price = Decimal(str(prod.price or 0))
        line_total = (price * qty).quantize(Decimal("0.01"))
        total_amount += line_total
        items_to_create.append({
            "product": prod,
            "variant": variant,
            "product_name": name_with_variant(prod.name, variant),
            "qty": qty,
            "price": price,
            "discount": Decimal("0.00"),
            "total": line_total,
        })

    if not items_to_create:
        return None, ""

    # 2. Создание заказа и резерв остатка
    try:
        with transaction.atomic():
            max_num = ShowcaseOrder.objects.filter(company=company).aggregate(m=Max("number"))["m"] or 0
            order = ShowcaseOrder.objects.create(
                company=company,
                number=max_num + 1,
                status=ShowcaseOrder.Status.NEW,
                customer_name=name,
                customer_phone=phone,
                delivery_type=ShowcaseOrder.DeliveryType.PICKUP,
                source="telegram",
                telegram_chat_id=str(chat_id),
                comment=f"[Telegram-бот chat_id={chat_id}] {comment}".strip(),
                total=total_amount,
            )
            ShowcaseOrderItem.objects.bulk_create([
                ShowcaseOrderItem(
                    order=order,
                    product=d["product"],
                    variant=d["variant"],
                    product_name=d["product_name"],
                    qty=d["qty"],
                    price=d["price"],
                    discount=d["discount"],
                    total=d["total"],
                )
                for d in items_to_create
            ])
            reserve_stock(items_to_create)
            order.stock_reserved = True
            order.save(update_fields=["stock_reserved"])
    except InsufficientStock as exc:
        return None, f"К сожалению, {html.escape(str(exc))} Выберите, пожалуйста, другой размер или цвет."
    except Exception as exc:
        logger.exception("Failed to auto-create ShowcaseOrder from Telegram: %s", exc)
        if settings.owner_chat_id:
            alert = (
                f"⚠️ Покупатель {name} ({phone}) пытался оформить заказ в Telegram-боте, "
                f"но произошла ошибка создания: {exc}. Пожалуйста, перезвоните покупателю!"
            )
            telegram_api.send_message(settings.token, settings.owner_chat_id, alert)
        return None, "Не удалось оформить заказ автоматически. Менеджер свяжется с вами в ближайшее время."

    # 3. Оповещение владельца / группы заказов (ТЗ Б-22)
    try:
        from apps.main.telegram_bot.services import order_notify
        order_notify.notify_new_order(order.id)
    except Exception as exc:
        logger.warning("Order notification failed: %s", exc)

    reply_for_customer = (
        f"✅ <b>Ваш заказ №{order.number} успешно оформлен!</b>\n"
        f"Итоговая сумма: <b>{order.total} сом</b>.\n"
        f"Тип получения: <b>Самовывоз</b>.\n"
        f"Мы свяжемся с вами по номеру {html.escape(phone)}, когда заказ будет готов к выдаче."
    )
    return order, reply_for_customer


def _customer_history_key(company_id, chat_id) -> str:
    return f"tg_cust_history:{company_id}:{chat_id}"


def get_customer_history(company_id, chat_id) -> list:
    try:
        return list(cache.get(_customer_history_key(company_id, chat_id)) or [])
    except Exception:
        return []


def save_customer_history(company_id, chat_id, history: list) -> None:
    try:
        cache.set(
            _customer_history_key(company_id, chat_id),
            history[-CUSTOMER_HISTORY_LEN:],
            timeout=CUSTOMER_HISTORY_TTL,
        )
    except Exception:
        pass


def clear_customer_history(company_id, chat_id) -> None:
    try:
        cache.delete(_customer_history_key(company_id, chat_id))
    except Exception:
        pass


def handle_customer_message(
    settings,
    chat_id: str,
    from_user: dict,
    text: str,
    is_voice: bool = False,
) -> None:
    """Главный обработчик сообщений покупателя."""
    from apps.main.telegram_bot.models import (
        TelegramInquiry,
        TelegramCustomerProfile,
    )
    from apps.main.models import Client

    token = settings.token
    if not token:
        return

    company = settings.company
    norm = (text or "").strip()
    lower_text = norm.lower()

    # 1. Проверка лимита запросов
    limit = settings.customer_limit_per_hour or 20
    if not check_rate_limit(company.id, chat_id, limit):
        phone_txt = settings.owner_phone or getattr(company, "phone", "") or ""
        msg = "Вы отправили слишком много сообщений за последний час. Пожалуйста, подождите некоторое время."
        if phone_txt:
            msg += f" Или позвоните нам по телефону: {phone_txt}"
        telegram_api.send_message(token, chat_id, msg)
        return

    # 2. Обновление профиля покупателя в БД
    cust_name = f"{(from_user.get('first_name') or '').strip()} {(from_user.get('last_name') or '').strip()}".strip()
    cust_username = from_user.get("username") or ""

    linked_client = Client.objects.filter(company=company, telegram_chat_id=str(chat_id)).first()

    profile, _ = TelegramCustomerProfile.objects.get_or_create(
        company=company,
        chat_id=str(chat_id),
        defaults={
            "name": cust_name,
            "username": cust_username,
            "client": linked_client,
        },
    )
    profile.messages_count += 1
    if cust_name:
        profile.name = cust_name
    if cust_username:
        profile.username = cust_username
    if linked_client and not profile.client:
        profile.client = linked_client
    profile.save(update_fields=["messages_count", "name", "username", "client", "last_at"])

    # Определение языка и меню покупателя (ТЗ-09 п. 2.1)
    from apps.main.telegram_bot.services.callback_handler import get_cart_count, _get_cart, _save_cart, _clear_cart
    from apps.main.telegram_bot.services.photo_service import send_product_photos_for_text
    from apps.main.models import Product, ProductCategory, ShowcaseOrder

    lang = "ky" if (from_user.get("language_code", "").startswith("ky") or any(w in lower_text for w in ["салам", "кандай", "кыргызча", "жеткирүү", "баасы", "канча"])) else "ru"
    cart_count = get_cart_count(company.id, chat_id)
    main_menu = get_customer_main_menu_keyboard(cart_count=cart_count, lang=lang)

    # 3. Специальные команды покупателя и кнопки постоянного меню
    if lower_text.startswith("/start"):
        parts = norm.split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            # /start <client_id>
            reply_text = link_client_by_start(company, chat_id, parts[1].strip())
            linked_client = Client.objects.filter(company=company, telegram_chat_id=str(chat_id)).first()
            if linked_client:
                profile.client = linked_client
                profile.save(update_fields=["client"])
        else:
            company_name = getattr(company, "name", "наш магазин")
            phone = settings.owner_phone or getattr(company, "phone", "") or ""
            reply_text = f"Здравствуйте! Добро пожаловать в «{company_name}». Чем мы можем вам помочь?"
            if is_valid_store_info(phone):
                reply_text += f"\nКонтакты магазина: {phone}"

        telegram_api.send_message(token, chat_id, reply_text, parse_mode="HTML", reply_markup=main_menu)
        TelegramInquiry.objects.create(
            company=company,
            chat_id=str(chat_id),
            name=cust_name,
            username=cust_username,
            text=text,
            reply=reply_text,
            is_voice=is_voice,
        )
        return

    # Меню: Каталог
    if lower_text in ("🛍 каталог", "каталог", "/catalog"):
        cats = ProductCategory.objects.filter(company=company)[:16]
        cat_btns = []
        row = []
        for c in cats:
            row.append({"text": c.name, "callback_data": f"cat:{c.id}:1"})
            if len(row) == 2:
                cat_btns.append(row)
                row = []
        if row:
            cat_btns.append(row)
        if cat_btns:
            telegram_api.send_message(token, chat_id, "🛍 <b>Каталог товаров:</b>\nВыберите категорию:", parse_mode="HTML", reply_markup={"inline_keyboard": cat_btns})
        else:
            telegram_api.send_message(token, chat_id, "Каталог товаров пуст или обновляется.", reply_markup=main_menu)
        return

    # Меню: Корзина
    if lower_text.startswith("🧺 корзина") or lower_text.startswith("🧺 себет") or lower_text in ("/cart", "корзина", "себет"):
        cart = _get_cart(company.id, chat_id)
        if not cart:
            telegram_api.send_message(token, chat_id, "Ваша корзина пуста. Напишите название товара, чтобы добавить его!", reply_markup=main_menu)
        else:
            lines = ["🧺 <b>Ваша корзина:</b>"]
            total = Decimal("0.00")
            for it in cart:
                qty = Decimal(str(it.get("qty") or "1"))
                price = Decimal(str(it.get("price") or "0"))
                line_tot = qty * price
                total += line_tot
                lines.append(f"• {html.escape(it.get('title',''))} — {format_qty(qty)} шт × {format_price_display(price)} = {format_price_display(line_tot)}")
            lines.append(f"\n<b>Итого: {format_price_display(total)}</b>")
            cart_markup = {
                "inline_keyboard": [
                    [{"text": "✅ Оформить заказ", "callback_data": "ord:cart_checkout"}],
                    [{"text": "🗑 Очистить", "callback_data": "ord:cart_clear"}],
                ]
            }
            telegram_api.send_message(token, chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=cart_markup)
        return

    # Меню: Мои заказы
    if lower_text in ("📦 мои заказы", "📦 менин буйрутмаларым", "мои заказы", "/orders"):
        orders = ShowcaseOrder.objects.filter(company=company, comment__icontains=f"chat_id={chat_id}").order_by("-created_at")[:5]
        if not orders.exists():
            telegram_api.send_message(token, chat_id, "У вас пока нет оформленных заказов.", reply_markup=main_menu)
        else:
            lines = ["📦 <b>Ваши последние заказы:</b>"]
            for o in orders:
                status_ru = "Новый" if o.status == ShowcaseOrder.Status.NEW else "Подтверждён" if o.status == ShowcaseOrder.Status.CONFIRMED else o.status
                lines.append(f"• Заказ №{o.number} от {o.created_at.strftime('%d.%m %H:%M')} — <b>{format_price_display(o.total)}</b> ({status_ru})")
            telegram_api.send_message(token, chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=main_menu)
        return

    # Меню: Адрес и время
    if lower_text in ("📍 адрес и время", "📍 дарек жана убакыт", "адрес", "график"):
        addr = getattr(company, "address", "")
        phone = settings.owner_phone or getattr(company, "phone", "")
        lines = ["📍 <b>Адрес и контакты магазина:</b>"]
        if is_valid_store_info(addr):
            lines.append(f"Адрес: {html.escape(addr)}")
        if is_valid_store_info(phone):
            lines.append(f"Телефон: {html.escape(phone)}")
        if len(lines) == 1:
            lines.append("Контакты магазина уточняйте у менеджера.")
        telegram_api.send_message(token, chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=main_menu)
        return

    # Меню: Связаться
    if lower_text in ("📞 связаться", "📞 байланышуу", "связаться", "контакты"):
        phone = settings.owner_phone or getattr(company, "phone", "")
        if is_valid_store_info(phone):
            msg = f"📞 Для связи с магазином звоните: <b>{html.escape(phone)}</b>"
        else:
            msg = "📞 Напишите ваш вопрос здесь, и мы обязательно вам ответим!"
        telegram_api.send_message(token, chat_id, msg, parse_mode="HTML", reply_markup=main_menu)
        return

    # Меню: Поиск товара
    if lower_text in ("🔎 найти товар", "🔎 товар издөө"):
        telegram_api.send_message(token, chat_id, "Напишите название товара, который вы ищете (например, «Алма» или «Кока-кола»).", reply_markup=main_menu)
        return

    if lower_text in ("/dolg", "мой долг", "долг", "менин карызым"):
        reply_text = get_customer_debt(company, chat_id)
        telegram_api.send_message(token, chat_id, reply_text, parse_mode="HTML", reply_markup=main_menu)
        TelegramInquiry.objects.create(
            company=company,
            chat_id=str(chat_id),
            name=cust_name,
            username=cust_username,
            text=text,
            reply=reply_text,
            is_voice=is_voice,
        )
        return

    # 4. Проверка запрещённых финансовых вопросов (TZ 5.3)
    financial_keywords = ["выручка", "прибыль", "сколько зарабатываете", "доход", "себестоимость", "чужой долг"]
    if any(k in lower_text for k in financial_keywords):
        reply_text = "Извините, финансовая информация и коммерческие данные магазина являются закрытыми. Я могу подсказать вам наличие товаров и цены."
        telegram_api.send_message(token, chat_id, reply_text, reply_markup=main_menu)
        TelegramInquiry.objects.create(
            company=company,
            chat_id=str(chat_id),
            name=cust_name,
            username=cust_username,
            text=text,
            reply=reply_text,
            is_voice=is_voice,
        )
        return

    # 5. Проверка кастомных сценариев по ключевым словам (ТЗ-11 п. 1.4)
    from apps.main.telegram_bot.views import match_scenario
    from apps.main.telegram_bot.models import TelegramBotScenario
    from apps.main.telegram_bot.tasks import _execute_scenario

    sc = match_scenario(company, text, audience="customers")
    if sc and sc.kind == TelegramBotScenario.Kind.KEYWORDS:
        _execute_scenario(settings, chat_id, sc, from_user, text, is_voice)
        return

    # 6. ИИ-консультант
    created_order = None
    reply_to_send = ""
    candidate_products_for_photos = []

    # Проверяем кэш одинаковых вопросов на 5 минут (ТЗ-09 п. 3.4)
    cached_reply = ai_service.get_cached_question_reply(company.id, text)
    if cached_reply:
        reply_to_send = cached_reply

    ai_key = ai_service.get_effective_ai_key(settings.ai_key)
    if not reply_to_send and settings.consultant_enabled and settings.ai_enabled and ai_key:
        history = get_customer_history(company.id, chat_id)
        recent_user_texts = [p.get("parts", [{}])[0].get("text", "") for p in history if p.get("role") == "user"]

        try:
            catalog_snippet, matched_catalog_items = build_catalog_context(company, user_text=text, recent_user_texts=recent_user_texts)
            matched_ids = [m["id"] for m in matched_catalog_items]
            candidate_products_for_photos = list(Product.objects.filter(id__in=matched_ids).prefetch_related("variants"))
        except Exception as exc:
            logger.exception("build_catalog_context failed: %s", exc)
            catalog_snippet = "Каталог временно недоступен — предложи покупателю позвонить в магазин."
            matched_catalog_items = []

        store_name = getattr(company, "name", "наш магазин")
        address = getattr(company, "address", "") or ""
        phone = settings.owner_phone or getattr(company, "phone", "") or ""

        store_lines = [f"Ты вежливый, внимательный ИИ-консультант магазина «{store_name}»."]
        if is_valid_store_info(address):
            store_lines.append(f"Адрес магазина: {address}")
        if is_valid_store_info(phone):
            store_lines.append(f"Телефон для связи: {phone}")
        store_info_block = "\n".join(store_lines)

        scenarios_block = build_scenarios_context(company)

        is_follow_up = len(history) > 0
        greeting_rule = "Это продолжение диалога (не первое сообщение). Не здоровайся снова, не пиши 'Здравствуйте!' или 'Приветствую!'." if is_follow_up else "Это первое сообщение диалога, вежливо поздоровайся."

        system_instruction = (
            f"{store_info_block}\n\n"
            f"КАТАЛОГ ТОВАРОВ МАГАЗИНА (название, варианты размеров и цветов, цена, статус наличия):\n"
            f"{catalog_snippet}\n\n"
            f"{scenarios_block}\n\n"
            "СТРОГИЕ ПРАВИЛА БЕЗОПАСНОСТИ И КОНСУЛЬТАЦИИ:\n"
            f"0. {greeting_rule}\n"
            "1. Отвечай только на основе каталога. Цену называй строго из каталога без лишних нулей (например, '120 сом', '1 900 сом').\n"
            "2. ВАРИАНТЫ (РАЗМЕРЫ И ЦВЕТА):\n"
            "   - Если у товара в каталоге указаны размеры и цвета — называй только те, что есть в наличии, и обязательно уточни размер и цвет перед заказом; акционную цену варианта называй, только если она указана в каталоге.\n"
            "   - Спросили про одежду и не назвали размер — сам перечисли размеры и цвета в наличии (коротко, по размерам) и спроси, какой нужен; нужного размера или цвета нет — предложи ближайший, который есть.\n"
            "   - Не говори «свободный размер», если в каталоге указаны размеры.\n"
            "3. ФОРМАТИРОВАНИЕ: отвечай ТОЛЬКО с HTML-тегами <b>, <i>. Никакого Markdown (НЕ используй **, *, _, #).\n"
            "4. ДАННЫЕ МАГАЗИНА: если адреса или телефона нет в описании магазина выше, НЕ выдумывай их и не упоминай.\n"
            "5. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО раскрывать: выручку магазина, продажи, прибыль, долги, точные складские остатки (говори только 'в наличии' или 'нет в наличии').\n"
            "6. ОФОРМЛЕНИЕ ЗАКАЗА (Самовывоз):\n"
            "   - Если у товара есть варианты, перед заказом ТЫ ОБЯЗАН УТОЧНИТЬ РАЗМЕР И ЦВЕТ.\n"
            "   - Уточни имя и контактный телефон покупателя.\n"
            "   - Спроси подтверждение: 'Оформить заказ на самовывоз?'\n"
            "   - ТОЛЬКО когда покупатель явно подтвердил заказ (написал 'да', 'оформить', 'заказываю' и т.д.), добавь В САМЫЙ КОНЕЦ ответа строго следующую служебную строку:\n"
            'ЗАКАЗ: {"name": "Имя", "phone": "+996...", "items": [{"title": "Название товара", "variant_id": "<variant_id из каталога>", "size": "32", "color": "синий", "qty": 1}], "comment": ""}\n'
            "   (variant_id копируй из каталога ровно как есть; у товара без вариантов variant_id = null, size и color пустые).\n"
            "   Вне этой ситуации строку ЗАКАЗ никогда не выводи.\n"
            "Отвечай вежливо и кратко на языке покупателя (русский или кыргызский)."
        )

        contents = list(history[-8:])
        contents.append({"role": "user", "parts": [{"text": text}]})
        try:
            # Очередь на компанию не более 3 одновременных запросов к Gemini (ТЗ-09 п. 3.3)
            with ai_service.CompanyAiConcurrencyLimit(company.id):
                raw_ai_reply, _ = ai_service.generate_chat_response(
                    api_key=ai_key,
                    system_instruction=system_instruction,
                    contents=contents,
                    temperature=0.3,
                    max_tokens=600,
                )

            # Проверяем, есть ли служебная строка заказа
            order_data, clean_ai_reply = extract_order_json(raw_ai_reply)
            if order_data:
                created_order, order_reply = create_order_from_ai_json(company, settings, order_data, chat_id)
                if created_order is not None:
                    reply_to_send = (clean_ai_reply + "\n\n" + order_reply) if clean_ai_reply else order_reply
                elif order_reply:
                    reply_to_send = order_reply
                else:
                    reply_to_send = clean_ai_reply
            else:
                reply_to_send = clean_ai_reply

            if not reply_to_send:
                reply_to_send = raw_ai_reply.strip()

            if created_order is not None:
                clear_customer_history(company.id, chat_id)
            else:
                history.append({"role": "user", "parts": [{"text": text}]})
                history.append({"role": "model", "parts": [{"text": reply_to_send}]})
                save_customer_history(company.id, chat_id, history)

            # Сохраняем в кэш на 5 минут (если нет оформления заказа)
            if not order_data:
                ai_service.set_cached_question_reply(company.id, text, reply_to_send)

        except Exception as exc:
            logger.error("Customer AI consultant failed: %s, falling back to catalog search", exc)
            reply_to_send = fallback_catalog_search(company, settings, text)
    elif not reply_to_send:
        # ИИ отключён или нет ключа -> поиск по каталогу
        reply_to_send = fallback_catalog_search(company, settings, text)
        try:
            matched_items = _match_catalog(get_catalog_data(company), text, limit=6)
            matched_ids = [m["id"] for m in matched_items]
            candidate_products_for_photos = list(Product.objects.filter(id__in=matched_ids).prefetch_related("variants"))
        except Exception:
            candidate_products_for_photos = []

    # 7. Отправка ответа покупателю
    if not reply_to_send:
        reply_to_send = "Спасибо за обращение! Мы скоро свяжемся с вами."

    cleaned_reply = ai_service.normalize_ai_output_for_telegram(reply_to_send)
    if not cleaned_reply:
        cleaned_reply = "Спасибо за обращение! Мы скоро свяжемся с вами."

    telegram_api.send_message(token, chat_id, cleaned_reply, parse_mode="HTML", reply_markup=main_menu)

    # 8. Отправка фотографий товаров (ТЗ-11 п. 3)
    if candidate_products_for_photos:
        try:
            send_product_photos_for_text(settings, chat_id, cleaned_reply, candidate_products_for_photos, is_owner=False)
        except Exception as exc:
            logger.warning("Photo delivery failed: %s", exc)

    # 9. Запись в журнал обращений (TZ 4.2 & 5.1)
    if created_order:
        profile.orders_count += 1
        profile.save(update_fields=["orders_count"])

    TelegramInquiry.objects.create(
        company=company,
        chat_id=str(chat_id),
        name=cust_name,
        username=cust_username,
        text=text,
        reply=reply_to_send,
        is_voice=is_voice,
        order=created_order,
    )
