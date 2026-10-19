# ТЗ для бэкенда NurCRM — часть 21: банковский терминал и QR-оплата с автоподтверждением

_12.10.2026. Источник — анализ конкурентов (MBANK MKassa и др.), запрос владельца «сделай их дизайн». Макеты — блок «Оплата — терминал, QR, электронный чек» (Б1–Б6). Функция в конструкторе — `payments_terminal_qr` (часть 19)._

## 1. Зачем

Сейчас при оплате картой или по QR кассир сам нажимает «Безналичные» и подтверждает — касса не знает, пришли ли деньги. Отсюда расхождения: чек закрыт «наличными», а деньги пришли на QR, или наоборот. Касса уже умеет показывать динамический QR ELQR (MBANK) с суммой.

Нужно: сумма уходит на терминал или в QR сама, а чек закрывается, **когда банк подтвердил оплату**. Плюс сверка с банком в конце смены и разбор платежей без чека.

**До разработки уточнить у банков** (MBANK, Optima Bank, O!Bank, затем ELQR-агрегатор): API создания платежа с суммой и номером заказа, вебхук об оплате, возврат, отчёт за день, комиссии. Интеграции с банками — на сервере NurCRM: ключи банков на кассы не попадают.

## 2. Модель

```
BankConnection
  company_id, bank — mbank | optima | obank | bakai | elqr
  kind             — qr | terminal | both
  credentials      — шифровано
  status, fee_percent, settlement_account

PaymentIntent (одна попытка оплаты)
  id, company_id, branch_id, cashbox_id, shift_id
  sale_id | cart_id     — к чему привязан
  amount, currency
  method                — card_terminal | qr
  bank
  status                — created | sent_to_terminal | waiting | paid | declined | canceled | expired
  bank_payment_id, card_mask, decline_reason
  qr_payload            — для QR
  expires_at            — по умолчанию +3 мин (настройка 1/3/5)
  created_at, paid_at

BankPayment (что пришло от банка, в том числе «лишнее»)
  id, bank, bank_payment_id, amount, paid_at, payer_mask
  intent_id (null — платёж без чека), sale_id
  status                — matched | unmatched | refunded
```

## 3. Ход оплаты

**QR**
1. Касса: `POST api/main/payments/intents/ {cart_id|sale_id, amount, method: qr, bank}` → `{intent_id, qr_payload, expires_at}`.
2. Касса показывает QR на экране покупателя (Б3) и в окне (Б2).
3. Банк присылает вебхук → сервер ставит `paid` и отправляет событие кассе (WebSocket или long-poll `GET …/intents/{id}/wait/?timeout=25`).
4. Касса закрывает чек: `checkout` с `payments:[{method: qr, amount, bank, reference: intent_id}]`.

**Терминал**
- Терминал по API банка (облако банка): сервер отправляет сумму на терминал (`terminal_id` из `BankConnection`), дальше как QR.
- Терминал по проводу или Wi-Fi через SDK на кассе: касса сама получает ответ терминала и присылает `PATCH …/intents/{id}/ {status: paid, bank_payment_id, card_mask}`. Сервер сверяет это в конце дня по отчёту банка.

**Отказ:** `declined` с причиной → окно «Банк отклонил оплату» (Б2). Чек не закрыт.

**Ручное подтверждение** (банк не ответил, а покупатель показывает «оплачено»): только с правом `payments_terminal_qr.manual_confirm` через PIN админа. `status = paid`, `manual = true`, при сверке такой платёж проверяется отдельно.

## 4. Разделить оплату

Один чек — несколько intent'ов и наличных. Чек закрывается, когда сумма оплаченных intent'ов плюс наличные равна итогу. Разбивка — в `payments` (часть 20, раздел 3).

## 5. Платёж без чека

Банк прислал деньги, а intent'а нет или он `expired` или `canceled` → `BankPayment.status = unmatched`.

`GET api/main/payments/unmatched/` — список с кандидатами: чеки той же кассы ±10 минут на ту же сумму, закрытые наличными (Б4).

Действия:
- `POST …/unmatched/{id}/link/ {sale_id, fix_payment_method: true}` — привязать и исправить способ оплаты в чеке;
- `POST …/unmatched/{id}/refund/` — вернуть через банк.

## 6. Сверка итогов (конец смены)

`GET api/main/payments/reconcile/?shift_id=` → по каждому банку и способу `{kassa_amount, bank_amount, diff, items_unmatched}`. Банк — из вебхуков и дневного отчёта банка.

Окно Б4 на кассе при закрытии смены. Расхождения не мешают закрыть смену, но попадают в отчёт смены и в уведомление владельцу.

## 7. Возврат

- на карту через терминал: `POST api/main/payments/refunds/ {sale_id, amount, method: card_terminal}` → покупатель прикладывает карту, ответ как у оплаты;
- QR: банк возвращает на счёт покупателя по `bank_payment_id`;
- возврат наличными при оплате картой — только с PIN админа (право `kassa.return` + `allow_with_pin`).

Чек возврата и ККМ — часть 20.

## 8. Зачисления

`GET api/main/payments/settlements/?date=` — сколько банк перечислил на счёт и когда (Б5, «Зачисления на счёт»), комиссия.

## 9. API (сводно)

```
GET/POST/PUT api/main/payments/banks/                 подключения (Б1)
POST   api/main/payments/banks/{id}/test/
POST   api/main/payments/intents/                     создать
GET    api/main/payments/intents/{id}/                состояние
GET    api/main/payments/intents/{id}/wait/?timeout=  долгий опрос до смены статуса
PATCH  api/main/payments/intents/{id}/                результат от SDK терминала на кассе
POST   api/main/payments/intents/{id}/cancel/
POST   api/main/payments/intents/{id}/manual-confirm/ {pin_token}
GET    api/main/payments/unmatched/
POST   api/main/payments/unmatched/{id}/link/
POST   api/main/payments/unmatched/{id}/refund/
GET    api/main/payments/reconcile/?shift_id=
POST   api/main/payments/refunds/
GET    api/main/payments/feed/?date=&bank=            лента платежей для веба (Б5)
GET    api/main/payments/settlements/?date=
POST   webhooks/banks/{bank}/                         вебхуки банков (подпись банка проверять)
```

## 10. Без интернета

Без связи с сервером QR и облачный терминал недоступны: кнопки показывают «нужна связь». Терминал через SDK на кассе работает, результат уходит на сервер при связи (`PATCH` с `client_uuid`).

## 11. Приёмка

1. QR MBANK на 650 → оплатить с телефона → чек закрылся сам ≤ 3 с после оплаты, в чеке `bank_payment_id`.
2. QR создан, покупатель ушёл → через 3 минуты `expired`, чек открыт.
3. Оплатить QR, а кассир закрыл чек «наличными» → платёж в `unmatched`, кандидат найден, «Привязать» исправляет способ оплаты.
4. Терминал: отказ «недостаточно средств» → окно отказа, «Повторить» создаёт новый intent.
5. Сверка: сумма по кассе = сумме по банку; искусственный лишний платёж → строка «≠».
6. Возврат на карту 195 → терминал, чек возврата, сверка дня учитывает возврат.
