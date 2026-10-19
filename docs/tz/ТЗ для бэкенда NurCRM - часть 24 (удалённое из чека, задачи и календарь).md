# ТЗ для бэкенда NurCRM — часть 24: журнал удалений из чека, задачи и календарь

_12.10.2026. Источник — запрос владельца: «нет отчёта по отменённым товарам — в корзину отсканировали и обратно удалили», «создание задач от админа сотрудникам и наоборот, напоминания для себя как календарь». Макеты — блоки «Отчёт: удалено из чека» (У1–У3) и «Задачи и календарь» (ЗД1–ЗД4). Функции в конструкторе — `cancel_control` и `tasks` (часть 19)._

---

## А. Журнал удалений из чека

### А1. Зачем

Сейчас касса собирает чек локально и отправляет на сервер только при оплате (`StagingCartService`). Удалённые до оплаты строки, уменьшение количества, очистка и отмена чека нигде не остаются. Это частый способ воровства: пробил, взял деньги, удалил строку. Нужен журнал на сервере и отчёт с подозрительными случаями.

### А2. Модель

```
CartEvent
  id, client_uuid                — уникален, касса шлёт пачками
  company_id, branch_id, cashbox_id, shift_id, employee_id
  local_cart_id                  — id корзины на кассе (до оплаты серверной продажи нет)
  sale_id?                       — если корзину потом оплатили
  kind      — line_removed | qty_decreased | cart_cleared | cart_canceled | line_added_after_precheck
  product_id?, name, qty, price, sum
  reason    — changed_mind | scan_error | no_money | defect | other
  reason_text
  after_precheck  — bool         пречек уже печатали
  admin_confirmed_by?, pin_used  — если удаление подтверждено PIN админа
  happened_at
  flags     — вычисляет сервер (А4)
```

Касса отправляет события: `POST api/main/cart-events/batch/ [CartEvent…]` сразу или пачкой раз в минуту. Без интернета — копит локально (`client_uuid` защищает от дублей).

### А3. Настройки контроля (У2, «Контроль удалений»)

```
CancelControlSettings
  require_reason              — bool
  pin_after_precheck          — bool
  pin_if_sum_over             — деньги (1 000)
  pin_for_clear_or_cancel     — bool
  notify_owner_if_per_shift_over — число (10), канал Telegram
  include_in_shift_report     — bool
```

Касса читает настройки и сама просит причину и PIN. Сервер их не требует, только хранит.

### А4. Подозрительные — правила (сервер считает `flags`)

| Флаг | Правило |
|---|---|
| `after_precheck` | удаление после печати пречека |
| `expensive_or_18plus` | цена строки ≥ порога (настройка) или категория с отметкой 18+ |
| `no_sale_after` | после удаления на этой кассе 3 минуты не было оплаченной продажи |
| `burst` | ≥ 5 удалений за 60 минут одним кассиром |
| `night_no_customer` | вне рабочих часов филиала |
| `above_norm` | доля удалений кассира за неделю ≥ 3× средней по компании |

### А5. API отчёта (У1, У3)

```
GET api/main/cart-events/?from=&to=&employee_id=&cashbox_id=&kind=&flag=&page=
GET api/main/cart-events/summary/?from=&to=
    → {count, sum, share_of_sales_percent, suspicious_count,
       by_employee:[{employee_id, count, sum, share_of_own_sales, times_norm}],
       top_products:[{product_id, name, count}]}
GET/PUT api/main/cart-events/settings/
```

Excel — тот же список (как остальные выгрузки).

Уведомление владельцу в Telegram при превышении `notify_owner_if_per_shift_over` и при `burst`: «Кассир Тест удалил 9 позиций за час».

Строка в Z-отчёте смены: «Удалено из чека: N позиций на сумму X».

### А6. Приёмка

1. Отсканировать 3 товара, удалить 1 с причиной «Покупатель передумал», оплатить → событие с `sale_id` и причиной.
2. Удалить строку после пречека → касса просит PIN; событие `after_precheck`, `admin_confirmed_by`.
3. 6 удалений за 10 минут → флаг `burst`, сообщение в Telegram.
4. Касса без интернета: 4 удаления → при связи 4 события без дублей.

---

## Б. Задачи и календарь

### Б1. Модель

```
Task
  id, company_id, branch_id?
  kind        — task | request | reminder
                task     — админ → сотруднику (или «все кассиры», «кто на смене»)
                request  — сотрудник → админу («Сообщить администратору»: закончился товар, поломка, отгул, другое)
                reminder — себе, видит только автор
  title, checklist — [{text, done, done_at}]
  assignees   — [employee_id] | group: all_cashiers | on_shift
  author_id
  due_at, priority — normal | urgent
  repeat      — none | daily | weekly | monthly ({day: 5})
  photo_report_required — bool
  remind      — {before_minutes | before_days, channels: [kassa, phone, telegram, whatsapp]}
  status      — new | in_progress | done | checked | rejected | overdue | canceled
  photos      — [url], comment
  request_type — out_of_stock | breakdown | day_off | other   (для request)
  linked      — {product_id?, purchase_order_id?}   «Закончился товар → в закупки»
  created_at, done_at, checked_at
```

Повторяющаяся задача создаёт новый экземпляр на каждую дату; статистика ведётся по экземплярам.

### Б2. Поведение

- **Задача кассиру** приходит на кассу окном, которое не перекрывает продажу и не забирает фокус ввода (ЗД2), и на телефон пуш-уведомлением.
- **«Сделано»**: если нужен фотоотчёт — без фото нельзя. Статус `done` → админ проверяет → `checked` (или `rejected` с комментарием).
- **Просрочка**: cron каждые 15 минут → `overdue`, уведомление автору.
- **Запрос «Закончился товар»**: кнопка «В закупки» создаёт строку в черновике заказа поставщику (закупки, часть 25) и связывает её с задачей.
- **Запрос «Отгул»**: «Разрешить» и «Отказать» с ответом сотруднику.
- **Напоминание себе**: только автор; повтор «каждый месяц, 5-го»; каналы на выбор.
- **Календарь** отдаёт вместе: задачи, напоминания, ожидаемые поставки из закупок, платежи по рассрочкам (часть 18) — по желанию владельца.

### Б3. API

```
GET    api/main/tasks/?scope=mine|assigned_by_me|reminders|requests&status=&from=&to=
POST   api/main/tasks/
GET    api/main/tasks/{id}/
PATCH  api/main/tasks/{id}/                    checklist, status, comment
POST   api/main/tasks/{id}/photos/             загрузка фото (multipart)
POST   api/main/tasks/{id}/check/              {result: checked|rejected, comment}
POST   api/main/tasks/{id}/to-purchase/        «В закупки»
GET    api/main/calendar/?from=&to=            [{type: task|reminder|delivery|installment, at, title, status, color}]
GET    api/main/tasks/summary/                 {today, overdue, waiting_check, done_week}
```

Касса и телефон получают новые задачи через тот же канал событий, что и оплату QR (часть 21), иначе — опрос раз в минуту.

### Б4. Права

`tasks.create_for_others`, `tasks.view_all` — админ и владелец. Запросы и напоминания себе — все сотрудники.

### Б5. Приёмка

1. Админ поручает «Пересчитать напитки» кассиру с фотоотчётом до 18:00 → окно на кассе; «Сделано» без фото не проходит; с фото → `done` → админ «проверено».
2. Кассир: «Закончились пакеты», срочно → у админа в «От сотрудников»; «В закупки» → строка в черновике заказа.
3. Напоминание «Оплатить аренду», каждый месяц 5-го, в Telegram → приходит за день; другим сотрудникам не видно.
4. Задача «Все кассиры» → у каждого кассира своя отметка выполнения.
