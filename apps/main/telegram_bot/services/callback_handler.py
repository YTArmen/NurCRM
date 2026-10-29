import html
import logging
import re
import uuid
from decimal import Decimal
from django.core.cache import cache
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.main.telegram_bot.services import telegram_api
from apps.main.telegram_bot.services.photo_service import format_price_display, format_qty

logger = logging.getLogger("telegram_bot.callback")


def _get_cart(company_id, chat_id: str) -> list:
    key = f"tg_cart:{company_id}:{chat_id}"
    return cache.get(key) or []


def _save_cart(company_id, chat_id: str, cart: list) -> None:
    key = f"tg_cart:{company_id}:{chat_id}"
    cache.set(key, cart, timeout=86400 * 7)


def _clear_cart(company_id, chat_id: str) -> None:
    key = f"tg_cart:{company_id}:{chat_id}"
    cache.delete(key)


def get_cart_count(company_id, chat_id: str) -> int:
    cart = _get_cart(company_id, chat_id)
    return sum(int(item.get("qty", 1)) for item in cart)


def make_product_card_markup(product_id, qty: Decimal = Decimal("1"), has_variants: bool = False, variants: list = None) -> dict:
    """Кнопки под карточкой товара: [ - ] [ qty ] [ + ] / [ В корзину ] [ Оформить ]."""
    qty_str = f"{format_qty(qty)} шт"
    p_id = str(product_id)

    # Если есть варианты, вместо количества даём кнопки размеров
    if has_variants and variants:
        # Inline-кнопки размеров (до 8, по 4 в строке)
        size_buttons = []
        row = []
        in_stock_sizes = []
        for v in variants:
            s = (v.size or "").strip()
            if s and (v.quantity or 0) > 0 and s not in in_stock_sizes:
                in_stock_sizes.append(s)

        for s in in_stock_sizes[:8]:
            row.append({"text": s, "callback_data": f"vs:{p_id}:{s}"[:64]})
            if len(row) == 4:
                size_buttons.append(row)
                row = []
        if row:
            size_buttons.append(row)

        action_row = [
            {"text": "🧺 В корзину", "callback_data": f"p:{p_id}:add:{qty}"[:64]},
            {"text": "✅ Оформить", "callback_data": f"p:{p_id}:buy:{qty}"[:64]},
        ]
        size_buttons.append(action_row)
        return {"inline_keyboard": size_buttons}

    minus_qty = max(Decimal("1"), qty - Decimal("1"))
    plus_qty = qty + Decimal("1")
    return {
        "inline_keyboard": [
            [
                {"text": "−", "callback_data": f"p:{p_id}:m:{qty}"[:64]},
                {"text": qty_str, "callback_data": f"p:{p_id}:noop"[:64]},
                {"text": "+", "callback_data": f"p:{p_id}:p:{qty}"[:64]},
            ],
            [
                {"text": "🧺 В корзину", "callback_data": f"p:{p_id}:add:{qty}"[:64]},
                {"text": "✅ Оформить", "callback_data": f"p:{p_id}:buy:{qty}"[:64]},
            ],
        ]
    }


def handle_callback_query(settings, cq: dict) -> None:
    """Главный диспетчер callback_query."""
    from apps.main.models import Product, ProductVariant, ShowcaseOrder, ShowcaseOrderItem
    from apps.main.variant_utils import InsufficientStock, reserve_stock, variant_prices, name_with_variant

    token = settings.token
    company = settings.company
    cq_id = cq.get("id")
    from_user = cq.get("from", {})
    message = cq.get("message", {})
    chat_id = str(message.get("chat", {}).get("id") or from_user.get("id") or "")
    message_id = message.get("message_id")
    data = cq.get("data") or ""

    # Немедленный ответ Telegram API
    telegram_api.answer_callback_query(token, cq_id)

    if not data or not chat_id:
        return

    # 1. Сценарий через кнопку sc_cmd:<command>
    if data.startswith("sc_cmd:"):
        cmd_text = data[7:].strip()
        from apps.main.telegram_bot.tasks import process_telegram_update
        # Запускаем обработку команды как текстовое сообщение
        simulated_update = {
            "update_id": int(timezone.now().timestamp() * 1000) % 2000000000,
            "message": {
                "message_id": message_id,
                "date": int(timezone.now().timestamp()),
                "chat": {"id": int(chat_id) if chat_id.isdigit() else chat_id},
                "from": from_user,
                "text": cmd_text,
            },
        }
        process_telegram_update(str(settings.id), simulated_update)
        return

    # 1б. ТЗ ч.15, п. 4: «Выполнить»/«Отмена» изменений, предложенных ИИ (только чат владельца)
    if data.startswith("aiact:"):
        from apps.main.telegram_bot.services import owner_actions

        if not (settings.owner_chat_id and str(settings.owner_chat_id) == chat_id):
            telegram_api.answer_callback_query(token, cq_id, text="Доступно только владельцу.", show_alert=True)
            return
        parts = data.split(":")
        action, pid = (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")
        pending = owner_actions.get_pending(company.id, chat_id)
        if not pending or pending.get("id") != pid:
            telegram_api.edit_message_reply_markup(token, chat_id, message_id, None)
            telegram_api.send_message(token, chat_id, "Этот список уже не актуален — попросите ещё раз.")
            return
        telegram_api.edit_message_reply_markup(token, chat_id, message_id, None)
        if action == "run":
            result = owner_actions.execute_pending(company, chat_id, pending, user=getattr(company, "owner", None))
            telegram_api.send_message(token, chat_id, owner_actions.build_result_message(result), parse_mode="HTML")
        else:
            owner_actions.clear_pending(company.id, chat_id)
            telegram_api.send_message(token, chat_id, "Отменено, ничего не менял.")
        return

    # 2. Кнопки количества товара p:<prod_id>:<action>:<qty>
    if data.startswith("p:"):
        parts = data.split(":")
        if len(parts) >= 3:
            p_id = parts[1]
            action = parts[2]
            try:
                curr_qty = Decimal(parts[3]) if len(parts) > 3 else Decimal("1")
            except Exception:
                curr_qty = Decimal("1")

            prod = Product.objects.filter(company=company, id=p_id).first()
            if not prod:
                return

            if action == "m":
                new_qty = max(Decimal("1"), curr_qty - Decimal("1"))
                markup = make_product_card_markup(p_id, new_qty)
                telegram_api.edit_message_reply_markup(token, chat_id, message_id, markup)
            elif action == "p":
                new_qty = curr_qty + Decimal("1")
                markup = make_product_card_markup(p_id, new_qty)
                telegram_api.edit_message_reply_markup(token, chat_id, message_id, markup)
            elif action == "add":
                cart = _get_cart(company.id, chat_id)
                # Добавляем или обновляем
                found = False
                for item in cart:
                    if item.get("product_id") == str(p_id) and not item.get("variant_id"):
                        item["qty"] = str(Decimal(item.get("qty", "1")) + curr_qty)
                        found = True
                        break
                if not found:
                    cart.append({
                        "product_id": str(p_id),
                        "variant_id": None,
                        "title": prod.name,
                        "price": str(prod.price or 0),
                        "qty": str(curr_qty),
                    })
                _save_cart(company.id, chat_id, cart)
                telegram_api.answer_callback_query(token, cq_id, text=f"Добавлено {format_qty(curr_qty)} шт в корзину!", show_alert=False)
            elif action == "buy":
                # Сразу оформляем этот товар
                draft_id = f"d_{uuid.uuid4().hex[:8]}"
                draft_data = {
                    "items": [{
                        "product_id": str(prod.id),
                        "variant_id": None,
                        "title": prod.name,
                        "price": str(prod.price or 0),
                        "qty": str(curr_qty),
                    }],
                    "delivery_type": "pickup",
                    "user_name": f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip() or from_user.get("username", "Покупатель"),
                    "phone": "",
                }
                cache.set(f"tg_draft:{company.id}:{draft_id}", draft_data, timeout=3600)
                _send_order_summary(token, chat_id, company, draft_id, draft_data)
        return

    # 3. Выбор размера одежды (ТЗ ч. 10): vs:<prod_id>:<size>
    if data.startswith("vs:"):
        parts = data.split(":", 2)
        if len(parts) >= 3:
            p_id, selected_size = parts[1], parts[2]
            prod = Product.objects.filter(company=company, id=p_id).first()
            if not prod:
                return

            variants = list(ProductVariant.objects.filter(company=company, product=prod, size__iexact=selected_size, is_active=True))
            in_stock_vars = [v for v in variants if (v.quantity or 0) > 0]

            if not in_stock_vars:
                # Этот размер закончился, показываем доступные
                all_active = ProductVariant.objects.filter(company=company, product=prod, is_active=True, quantity__gt=0)
                avail_sizes = sorted(list({v.size for v in all_active if v.size}))
                msg = f"Размер <b>{html.escape(selected_size)}</b> только что закончился."
                if avail_sizes:
                    msg += f" В наличии есть размеры: {', '.join(avail_sizes)}."
                telegram_api.send_message(token, chat_id, msg, parse_mode="HTML")
                return

            # Показываем кнопки цветов
            color_buttons = []
            row = []
            colors_list = []
            for v in in_stock_vars:
                c_label = (v.color or "Стандарт").strip()
                colors_list.append(c_label)
                row.append({"text": c_label, "callback_data": f"vc:{v.id}"[:64]})
                if len(row) == 2:
                    color_buttons.append(row)
                    row = []
            if row:
                color_buttons.append(row)

            color_buttons.append([{"text": "◀ Назад к размерам", "callback_data": f"vs_back:{p_id}"[:64]}])

            price_str = format_price_display(prod.price)
            text_msg = (
                f"<b>{html.escape(prod.name)}</b>, размер <b>{html.escape(selected_size)}</b>\n"
                f"🎨 Цвета: {', '.join(colors_list)} — 💰 {price_str}\n"
                f"Выберите цвет:"
            )
            telegram_api.send_message(token, chat_id, text_msg, parse_mode="HTML", reply_markup={"inline_keyboard": color_buttons})
        return

    # Назад к выбору размера
    if data.startswith("vs_back:"):
        p_id = data.split(":", 1)[1]
        prod = Product.objects.filter(company=company, id=p_id).prefetch_related("variants").first()
        if prod:
            markup = make_product_card_markup(prod.id, has_variants=True, variants=list(prod.variants.all()))
            telegram_api.send_message(token, chat_id, f"Выберите размер для <b>{html.escape(prod.name)}</b>:", parse_mode="HTML", reply_markup=markup)
        return

    # 4. Выбор цвета одежды: vc:<variant_id>
    if data.startswith("vc:"):
        v_id = data.split(":", 1)[1]
        variant = ProductVariant.objects.filter(company=company, id=v_id, is_active=True).select_related("product").first()
        if not variant or (variant.quantity or 0) <= 0:
            telegram_api.send_message(token, chat_id, "Этот вариант товара только что закончился. Пожалуйста, выберите другой.")
            return

        prod = variant.product
        price, _old = variant_prices(variant, prod)
        price_str = format_price_display(price)
        v_label = f"{variant.size} {variant.color}".strip()

        draft_id = f"d_{uuid.uuid4().hex[:8]}"
        draft_data = {
            "items": [{
                "product_id": str(prod.id),
                "variant_id": str(variant.id),
                "title": f"{prod.name} ({v_label})",
                "price": str(price),
                "qty": "1",
            }],
            "delivery_type": "pickup",
            "user_name": f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip() or from_user.get("username", "Покупатель"),
            "phone": "",
        }
        cache.set(f"tg_draft:{company.id}:{draft_id}", draft_data, timeout=3600)

        buttons = [
            [{"text": "✅ Оформить заказ", "callback_data": f"ord:summary:{draft_id}"[:64]}],
            [
                {"text": "🧺 В корзину", "callback_data": f"ord:cart_add:{draft_id}"[:64]},
                {"text": "🔄 Другой размер", "callback_data": f"vs_back:{prod.id}"[:64]},
            ],
        ]
        msg = f"Вы выбрали: <b>{html.escape(prod.name)}</b> ({html.escape(v_label)})\n💰 Цена: <b>{price_str}</b>"
        telegram_api.send_message(token, chat_id, msg, parse_mode="HTML", reply_markup={"inline_keyboard": buttons})
        return

    # 5. Оформление заказа ord:...
    if data.startswith("ord:"):
        parts = data.split(":", 2)
        ord_action = parts[1]
        arg = parts[2] if len(parts) > 2 else ""

        if ord_action in ("summary", "cart_checkout"):
            draft_id = arg
            draft_data = cache.get(f"tg_draft:{company.id}:{draft_id}")
            if not draft_data and ord_action == "cart_checkout":
                # Создаём draft из корзины
                cart = _get_cart(company.id, chat_id)
                if not cart:
                    telegram_api.send_message(token, chat_id, "Ваша корзина пуста.")
                    return
                draft_id = f"d_{uuid.uuid4().hex[:8]}"
                draft_data = {
                    "items": cart,
                    "delivery_type": "pickup",
                    "user_name": f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip() or from_user.get("username", "Покупатель"),
                    "phone": "",
                }
                cache.set(f"tg_draft:{company.id}:{draft_id}", draft_data, timeout=3600)
            if draft_data:
                _send_order_summary(token, chat_id, company, draft_id, draft_data)
            return

        if ord_action == "cart_add":
            draft_id = arg
            draft_data = cache.get(f"tg_draft:{company.id}:{draft_id}")
            if draft_data and draft_data.get("items"):
                cart = _get_cart(company.id, chat_id)
                cart.extend(draft_data["items"])
                _save_cart(company.id, chat_id, cart)
                telegram_api.answer_callback_query(token, cq_id, text="Товар добавлен в корзину!", show_alert=False)
            return

        if ord_action in ("pickup", "delivery"):
            draft_id = arg
            draft_data = cache.get(f"tg_draft:{company.id}:{draft_id}")
            if draft_data:
                draft_data["delivery_type"] = ord_action
                cache.set(f"tg_draft:{company.id}:{draft_id}", draft_data, timeout=3600)
                _send_order_summary(token, chat_id, company, draft_id, draft_data, edit_message_id=message_id)
            return

        if ord_action == "confirm":
            draft_id = arg
            draft_data = cache.get(f"tg_draft:{company.id}:{draft_id}")
            if not draft_data:
                telegram_api.send_message(token, chat_id, "Срок действия заказа истёк. Пожалуйста, соберите заказ заново.")
                return

            # Убираем кнопки со сводки (чтобы нельзя было подтвердить дважды)
            telegram_api.edit_message_reply_markup(token, chat_id, message_id, reply_markup={"inline_keyboard": []})

            # Подтверждаем и создаём заказ
            items_raw = draft_data.get("items") or []
            if not items_raw:
                return

            # Проверяем товары и считаем сумму строго по БД
            items_to_create = []
            total_sum = Decimal("0.00")
            for it in items_raw:
                p_id = it.get("product_id")
                v_id = it.get("variant_id")
                qty = Decimal(str(it.get("qty") or "1"))
                prod = Product.objects.filter(company=company, id=p_id).first()
                if not prod:
                    continue
                variant = None
                if v_id:
                    variant = ProductVariant.objects.filter(company=company, id=v_id, is_active=True).first()

                if variant is not None:
                    line_price = variant_prices(variant, prod)[0]
                else:
                    line_price = Decimal(str(prod.price or 0))

                line_total = (line_price * qty).quantize(Decimal("0.01"))
                total_sum += line_total
                items_to_create.append({
                    "product": prod,
                    "variant": variant,
                    "product_name": name_with_variant(prod.name, variant),
                    "qty": qty,
                    "price": line_price,
                    "discount": Decimal("0.00"),
                    "total": line_total,
                })

            if not items_to_create:
                telegram_api.send_message(token, chat_id, "Не удалось сформировать позиции заказа.")
                return

            cust_name = draft_data.get("user_name") or "Покупатель Telegram"
            phone = draft_data.get("phone") or ""
            del_type = ShowcaseOrder.DeliveryType.DELIVERY if draft_data.get("delivery_type") == "delivery" else ShowcaseOrder.DeliveryType.PICKUP

            try:
                with transaction.atomic():
                    max_num = ShowcaseOrder.objects.filter(company=company).aggregate(m=Max("number"))["m"] or 0
                    order = ShowcaseOrder.objects.create(
                        company=company,
                        number=max_num + 1,
                        status=ShowcaseOrder.Status.NEW,
                        customer_name=cust_name,
                        customer_phone=phone,
                        delivery_type=del_type,
                        source="telegram",
                        telegram_chat_id=str(chat_id),
                        comment=f"[Telegram chat_id={chat_id}]",
                        total=total_sum,
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
                telegram_api.send_message(token, chat_id, f"К сожалению, {exc} Заказ не может быть подтверждён.")
                return
            except Exception as exc:
                logger.exception("Order confirmation failed: %s", exc)
                telegram_api.send_message(token, chat_id, "Ошибка при создании заказа. Пожалуйста, обратитесь в магазин.")
                return

            # Очищаем корзину и драфт
            _clear_cart(company.id, chat_id)
            cache.delete(f"tg_draft:{company.id}:{draft_id}")

            # Сообщение покупателю
            confirm_msg = (
                f"✅ <b>Заказ №{order.number} принят!</b>\n"
                f"Сумма к оплате: <b>{format_price_display(order.total)}</b>\n"
                f"Сообщим, когда он будет готов."
            )
            my_orders_btn = {"inline_keyboard": [[{"text": "📦 Мои заказы", "callback_data": "ord:my"}]]}
            telegram_api.send_message(token, chat_id, confirm_msg, parse_mode="HTML", reply_markup=my_orders_btn)

            # Уведомление владельцу / в группу заказов (ТЗ Б-22: единый путь, не больше одного на заказ)
            from apps.main.telegram_bot.services import order_notify
            order_notify.notify_new_order(order.id)
            return

        if ord_action == "cancel":
            telegram_api.edit_message_reply_markup(token, chat_id, message_id, reply_markup={"inline_keyboard": []})
            telegram_api.send_message(token, chat_id, "Оформление заказа отменено.")
            return

        if ord_action == "my":
            orders = ShowcaseOrder.objects.filter(company=company, comment__icontains=f"chat_id={chat_id}").order_by("-created_at")[:5]
            if not orders.exists():
                telegram_api.send_message(token, chat_id, "У вас пока нет оформленных заказов.")
                return
            lines = ["📦 <b>Ваши последние заказы:</b>"]
            for o in orders:
                status_ru = "Новый" if o.status == ShowcaseOrder.Status.NEW else "Подтверждён" if o.status == ShowcaseOrder.Status.ACCEPTED else o.status
                lines.append(f"• Заказ №{o.number} от {o.created_at.strftime('%d.%m %H:%M')} — <b>{format_price_display(o.total)}</b> ({status_ru})")
            telegram_api.send_message(token, chat_id, "\n".join(lines), parse_mode="HTML")
            return

    # 5б. ТЗ Б-22: кнопки в уведомлении о заказе ordn:<order_id>:a|c (владелец; в группе заказов тоже)
    if data.startswith("ordn:"):
        from apps.main.telegram_bot.services import order_notify

        parts = data.split(":")
        order = None
        if len(parts) == 3 and parts[2] in ("a", "c"):
            try:
                order = ShowcaseOrder.objects.filter(company=company, id=parts[1]).first()
            except Exception:
                order = None
        if order is None:
            telegram_api.answer_callback_query(token, cq_id, text="Заказ не найден.", show_alert=True)
            return
        ok, msg = order_notify.apply_order_action(settings, order, parts[2], from_user.get("id"), chat_id)
        if not ok:
            telegram_api.answer_callback_query(token, cq_id, text=msg, show_alert=True)
            return
        telegram_api.edit_message_reply_markup(token, chat_id, message_id, reply_markup={"inline_keyboard": []})
        telegram_api.send_message(token, chat_id, msg, parse_mode=None)
        return

    # 6. Действия владельца над заказом oo:<order_id>:accept / reject
    if data.startswith("oo:"):
        parts = data.split(":")
        if len(parts) >= 3:
            order_id = parts[1]
            owner_action = parts[2]
            order = ShowcaseOrder.objects.filter(company=company, id=order_id).first()
            if not order:
                return

            from apps.main.telegram_bot.services import order_notify
            ok, msg = order_notify.apply_order_action(
                settings, order, "a" if owner_action == "accept" else "c", from_user.get("id"), chat_id)
            if not ok:
                telegram_api.answer_callback_query(token, cq_id, text=msg, show_alert=True)
                return
            telegram_api.edit_message_reply_markup(token, chat_id, message_id, reply_markup={"inline_keyboard": []})
            telegram_api.send_message(token, chat_id, msg, parse_mode=None)
        return


def _send_order_summary(token: str, chat_id: str, company, draft_id: str, draft_data: dict, edit_message_id: int = None) -> None:
    """Отправляет сводку заказа с кнопками подтверждения (ТЗ ч. 9 п. 2.3)."""
    items = draft_data.get("items") or []
    lines = ["🧺 <b>Ваш заказ</b>"]
    total = Decimal("0.00")
    for it in items:
        qty = Decimal(str(it.get("qty") or "1"))
        price = Decimal(str(it.get("price") or "0"))
        line_tot = qty * price
        total += line_tot
        lines.append(f"{html.escape(it.get('title', ''))} — {format_qty(qty)} шт × {format_price_display(price)} = {format_price_display(line_tot)}")

    lines.append(f"<b>Итого: {format_price_display(total)}</b>")
    del_type = draft_data.get("delivery_type", "pickup")
    del_str = "🚚 Доставка" if del_type == "delivery" else "🏪 Самовывоз"
    cust_info = draft_data.get("user_name", "")
    if draft_data.get("phone"):
        cust_info += f", {draft_data['phone']}"
    lines.append(f"Получение: {del_str} · {html.escape(cust_info)}")

    text = "\n".join(lines)

    buttons = [
        [
            {"text": "🏪 Самовывоз" + (" ✓" if del_type == "pickup" else ""), "callback_data": f"ord:pickup:{draft_id}"},
            {"text": "🚚 Доставка" + (" ✓" if del_type == "delivery" else ""), "callback_data": f"ord:delivery:{draft_id}"},
        ],
        [
            {"text": "✅ Подтвердить", "callback_data": f"ord:confirm:{draft_id}"},
            {"text": "❌ Отменить", "callback_data": f"ord:cancel:{draft_id}"},
        ],
    ]

    markup = {"inline_keyboard": buttons}
    if edit_message_id:
        telegram_api.edit_message_text(token, chat_id, edit_message_id, text, parse_mode="HTML", reply_markup=markup)
    else:
        telegram_api.send_message(token, chat_id, text, parse_mode="HTML", reply_markup=markup)
