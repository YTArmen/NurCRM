# ТЗ для бэкенда NurCRM — часть 22: бонусы на сервере и подарочные сертификаты

_12.10.2026. Источник — анализ конкурентов (Loyverse, Poster: лояльность общая для всех касс), запрос владельца «сделай их дизайн». Макеты — блок «Бонусы на сервере и подарочные сертификаты» (Л1–Л5), О2 «Клиенты и бонусы», 29 «Магазин в приложении». Функции в конструкторе — `clients_bonus` и `gift_cards` (часть 19)._

## 1. Зачем

Сейчас бонусы кассы (с 1.16) хранятся локально на кассе: у клиента разный баланс на разных кассах и в филиалах, а при переустановке баланс теряется. На доске «Магазин в приложении» уже есть «баллы, которые считает сервер». **Если в NurCRM баллы для приложения уже есть — это и есть основа: второй счёт не заводить**, а расширить до правил ниже.

Подарочных сертификатов нет совсем.

## 2. Настройки программы (Л1)

```
LoyaltySettings (компания)
  enabled
  levels         — [{code: base|silver|gold, title{5 языков}, percent, threshold_year_sum}]   по умолчанию 3/5/7 %, 0/20 000/60 000
  bonus_value    — 1 бонус = 1 сом
  max_spend_percent   — 30          сколько % чека можно оплатить бонусами
  expire_days         — 180
  welcome_bonus       — 100
  birthday            — {multiplier: 2, days_before: 3, days_after: 3}
  accrue_delay_days   — 0           начислять сразу или через N дней (защита от возвратов)
  exclude             — {discounted: true, category_ids:[…], product_ids:[…]}   — например сигареты и алкоголь
  remind_before_expire_days — 7     напоминание в WhatsApp
```

Уровень клиента = по сумме покупок за последние 365 дней, пересчёт раз в сутки и после каждой покупки.

## 3. Модель

```
LoyaltyAccount   — client_id, balance, level, year_sum, card_qr_token, created_at
BonusTransaction — id, client_id, sale_id?, kind: accrue | spend | expire | welcome | birthday | manual | refund_reverse,
                   amount (+/−), expires_at (для начислений), created_by, branch_id, client_uuid
GiftCard         — id, code ('NBS-7K4Q-2M9X'), qr_token, nominal, balance,
                   status: active | partially_used | used | expired | blocked,
                   sold_sale_id, sold_at, valid_until, message ('Айгуль, с днём рождения!'),
                   kind: printed | e_card | plastic, recipient_phone?
GiftCardTransaction — id, gift_card_id, sale_id, amount (−), created_at, branch_id
```

Бонусы списываются FIFO: сначала те, что сгорают раньше.

## 4. Продажа с бонусами

- касса: `GET api/main/loyalty/clients/{client_id}/` или поиск по телефону или QR (`?qr=`) → `{balance, level, percent, can_spend_max (для текущего чека), birthday_active}`;
- `POST api/main/loyalty/preview/ {client_id, lines[], total, spend}` → `{spend_allowed, accrue, total_after}` — касса показывает «650 − 195 = 455, начислим +23» (Л2);
- в `checkout` добавить `loyalty: {client_id, spend}` и строку оплаты `{method: bonus, amount: spend}`;
- сервер начисляет `accrue = (итог − spend − исключённые строки) × percent` (× `multiplier` в дни рождения), округление вниз до целого.

**Возврат:** начисленные по чеку бонусы списываются пропорционально возвращённому; потраченные — возвращаются клиенту (`refund_reverse`).

**Без интернета:** касса знает последний баланс клиента из кэша. Списать можно не больше кэшированного баланса и не больше `max_spend_percent`. Транзакция уходит с `client_uuid`. Если на сервере баланса не хватило — продажа проходит, баланс уходит в минус до следующих начислений, владельцу приходит уведомление.

## 5. Сертификаты

- **продажа:** строка чека `{type: gift_card, nominal, kind, message, recipient_phone}`. На эту строку бонусы не начисляются. Сертификат создаётся при `checkout`, в ответе `code` и `qr_token` для печати (Л3);
- **электронный:** ссылка в WhatsApp на страницу сертификата `public/gift/{qr_token}`;
- **оплата сертификатом:** `GET api/main/gift-cards/?code=` → `{balance, status, valid_until}`; в `checkout` строка оплаты `{method: gift_card, gift_card_id, amount}`. Сервер блокирует сертификат на время запроса, чтобы два чека не потратили его одновременно;
- **частичное использование:** остаток сохраняется (`partially_used`);
- **деньги:** продажа сертификата — это предоплата, а не выручка. В «Прибыли и деньгах» и аналитике — отдельная строка «Сертификаты: продано / погашено». Выручка признаётся при погашении. Уточнить с бухгалтером, как это попадает в ККМ (часть 20);
- **истечение:** cron 00:10, `valid_until < сегодня` → `expired`. Остаток выключенного сертификата видно в отчёте.

## 6. Напоминания и рассылки

- за `remind_before_expire_days` до сгорания бонусов — WhatsApp «У вас 120 бонусов сгорят [дата]»;
- кнопка «Напомнить в WhatsApp» (Л4) — массово всем, у кого бонусы сгорят в этом месяце;
- акция «Бонусы × 2»: `LoyaltyCampaign {multiplier, from, to, branch_ids, category_ids}`.

## 7. Карта клиента в Telegram-боте

Бот по `/card` показывает баланс, уровень, QR (`card_qr_token`), историю, сгорание (Л3). QR на кассе находит клиента (`?qr=`). Токен можно перевыпустить.

## 8. API

```
GET/PUT api/main/loyalty/settings/
GET     api/main/loyalty/clients/?phone=&qr=
GET     api/main/loyalty/clients/{client_id}/
GET     api/main/loyalty/clients/{client_id}/transactions/
POST    api/main/loyalty/preview/
POST    api/main/loyalty/manual/                 {client_id, amount, reason}  — право clients_bonus.settings
GET     api/main/loyalty/summary/                {members, share_with_card, accrued_month, spent_month,
                                                  balance_total (обязательство), expiring_month:{amount, clients},
                                                  levels:[{code, count}], top_clients:[…]}
POST    api/main/loyalty/remind-expiring/
GET/POST api/main/loyalty/campaigns/

GET     api/main/gift-cards/?code=&status=&page=
GET     api/main/gift-cards/{id}/
POST    api/main/gift-cards/{id}/resend/         переслать электронный
POST    api/main/gift-cards/{id}/block/          {reason}
GET     api/main/gift-cards/summary/             {sold, redeemed, outstanding}
GET     public/gift/{qr_token}/
```

## 9. Перенос локальных бонусов

Касса однократно отправляет локальные балансы: `POST api/main/loyalty/import/ [{phone, balance, history_sum}]`. Сервер берёт **максимум** из баланса сервера и кассы, а не сумму: несколько касс могли хранить одного клиента. Владелец видит итог импорта перед подтверждением.

## 10. Права

`clients_bonus.spend_bonus` — кассир. `clients_bonus.settings`, `manual` — админ. `gift_cards.sell_card` — кассир. Блокировка сертификата — админ.

## 11. Приёмка

1. Клиент «Серебро» 5 %: чек 650 без списания → +32; со списанием 195 → к оплате 455, +23 (числа из Л2).
2. Тот же клиент на кассе филиала — тот же баланс сразу.
3. Сертификат 2 000: оплатить 650 → остаток 1 350; тот же код одновременно на двух кассах — второй запрос получает отказ.
4. Возврат чека со списанными бонусами → бонусы вернулись, начисленные по чеку сняты.
5. Бонусы с истёкшим сроком сгорели в cron; за 7 дней ушло напоминание.
6. Импорт локальных бонусов с двух касс для одного телефона → берётся максимум.
