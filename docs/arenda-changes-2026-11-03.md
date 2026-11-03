# Аренда помещений: доработки сервера от 03.11.2026

Ветка `feature/arenda-tz-fixes` (приложение `apps/arenda`). Все пути ниже — относительно `/api/arenda/`.
Суммы везде в тыйынах (целые). Ошибки: `{"detail": "...", "code": "..."}` (ApiError) или `{"поле": ["..."]}` (400).
Права: «владелец» = владелец компании, роли owner/admin и суперпользователь; остальным нужен раздел (см. `constants.py`).

## Новые эндпоинты

| Метод и путь | Тело запроса | Ответ | Права |
|---|---|---|---|
| POST `reminders/delivery/` | `{id, status: "delivered"\|"failed", error?}` | 200, запись напоминания; 400 неверные поля; 403 нет/неверный заголовок `X-Arenda-Delivery-Key`; 404 нет напоминания | без входа, но с ключом `ARENDA_DELIVERY_KEY` |
| POST `auth/reset/request/` | `{login}` (телефон или email) | 200 `{detail, expires_in: 600}` всегда одинаково; 400 пустой login; 503 `{code:"sms_unavailable"}` если SMS не подключён | без входа |
| POST `auth/reset/confirm/` | `{login, code, new_password}` | 200 `{detail}`; 400 `{code:["Код неверный или устарел."]}` или `{new_password:[...]}` | без входа |
| POST `tenants/<uuid>/charges/` | `{kind: "penalty"\|"utilities", amount>0, period: "ГГГГ-ММ", due_date: "ГГГГ-ММ-ДД", comment?, lease_id?}` | 201 `{id, tenant, lease, kind, period, amount, due_date, description, voided, debt_after}`; 409 `charge_exists` (коммунальные по этому договору и периоду уже есть) | пеня — только владелец (иначе 403); коммунальные — раздел «Касса» или «Арендаторы» |
| POST `charges/<uuid>/void/` | `{reason}` | 200 то же без `debt_after`, `voided: true`; 409 `charge_has_payments` (есть разнесённые оплаты) или `already_voided` | только владелец |
| GET `receipts/<uuid>/receipt.pdf` | — | PDF, ширина 80 мм (`?inline=1` — показать, иначе скачать) | «Касса» или «Арендаторы» |
| GET `acts/<uuid>/act.pdf` | — | PDF A4, внизу подписи, в т.ч. «Арендатор: ____ / ФИО /» | «Арендаторы» или «Касса» |
| POST `receipts/<uuid>/send/` | `{channel: "sms"\|"whatsapp"}` | 201 запись напоминания `{id, tenant, channel, text, is_auto, rule_kind, status, error, sent_at, created_at}` | «Касса» или «Арендаторы» |
| POST `acts/<uuid>/send/` | `{channel: "sms"\|"whatsapp"}` | 201 запись напоминания (как выше) | «Арендаторы» или «Касса» |
| PATCH `me/` | любые из `{first_name, last_name, phone, email}` | 200 как GET `me/` (обновлённый); 409 `email_taken` / `phone_taken`; 400 неверный формат | любой вошедший, свой профиль |

Что изменилось в существующем:

- GET `me/` и вложенные `user`-объекты теперь содержат `first_name`, `last_name`.
- `payments/`: способ `method` принимает ещё `wallet` (кошелёк); остатки считают его.
- `notifications/remind/` с `channel: "telegram"` отвечает 400 «Telegram — только для копий владельцу. Выберите SMS или WhatsApp.» (автоправило «копия владельцу» работает как раньше).
- `settings/` отдаёт и принимает `tax_due_day` (1–31, по умолчанию 25) — срок налогов с зарплаты в следующем месяце.
- `expenses/` понимает `date_from`, `date_to` (ГГГГ-ММ-ДД) и `branch`.
- `leases/` (POST): `template_id` необязателен; снимок текста строится по нему, иначе по первому не архивному шаблону аренды; нет шаблонов — снимок пустой, договор всё равно создаётся.
- `templates/import/` сохраняет исходный файл в `Template.source_file`.
- `auth/login/`: телефон ищется и по полю `User.phone_number` (любое написание +996…, 0…, пробелы), если номер однозначен.
- Статусы напоминания: `not_sent` (канал не подключён или нет телефона), `sent`, `failed` (шлюз не ответил 2xx), `delivered` (по отчёту шлюза).
- Оплата с «Добавить пеню» вычитает уже начисленную пеню текущего периода просрочки (двойной пени нет).
- «Сдано»: одно определение для шахматки и аналитики: договор действует на дату (start ≤ d ≤ end) и не расторгнут. Истёкший, но не расторгнутый договор помещение «сдано» не показывает.
- Амортизация в аналитике считается функцией `services/expenses.depreciation_for_month` (амортизация идёт со следующего месяца после покупки).

## Отправка SMS и WhatsApp (services/sender.py)

Шлюз вызывается так: `POST <URL>`, заголовки `Authorization: Bearer <TOKEN>`, `Content-Type: application/json`, тело `{"phone": "+996…", "text": "...", "id": "<uuid напоминания>"}`, таймаут 10 с. Ответ 2xx — `sent`. URL не задан — `not_sent` с текстом «Сервис SMS не подключён» (для WhatsApp — «Сервис WhatsApp не подключён»). Другой код или сбой сети — `failed`.
Шлюз может позже сообщить о доставке запросом на `reminders/delivery/` (поле `id` — то, что мы передали).

## Расписание (Celery, `apps/arenda/tasks.py`)

- `apps.arenda.tasks.monthly_charges` — каждый день 00:10 (Asia/Bishkek): начисления месяца по всем договорам и заполнение налогового календаря на текущий и следующий месяц. Идемпотентно.
- `apps.arenda.tasks.auto_reminders` — каждый день 09:00: автонапоминания и автопеня. Идемпотентно.
- Модуль добавлен в `CELERY_IMPORTS`; запуск требует работающих celery worker и celery beat. Старые команды `arenda_monthly_charges` и `arenda_auto_reminders` остались.

## Новые переменные окружения

| Переменная | Для чего |
|---|---|
| `ARENDA_SMS_URL`, `ARENDA_SMS_TOKEN` | шлюз SMS |
| `ARENDA_WHATSAPP_URL`, `ARENDA_WHATSAPP_TOKEN` | шлюз WhatsApp |
| `ARENDA_DELIVERY_KEY` | ключ для заголовка `X-Arenda-Delivery-Key` отчётов о доставке; не задан — эндпоинт всегда 403 |
| `ARENDA_TELEGRAM_BOT_TOKEN` | бот Telegram для копий владельцу (теперь читается из env в `core/settings.py`) |

## Миграции (`apps/arenda/migrations`, добавлены принудительно: в `apps/.gitignore` есть `*migrations`)

- `0003_password_reset_code` — таблица кодов восстановления пароля (хранится только хеш кода).
- `0004_tax_due_day` — поле `ArendaSettings.tax_due_day` (по умолчанию 25).

## Восстановление пароля по SMS

Код 6 цифр, действует 10 минут, новый запрос гасит прежний код; не чаще одного запроса в минуту на пользователя (повтор молча игнорируется, ответ тот же); 5 неверных вводов — код сгорает. Только для пользователей аренды (есть доступ, запись безопасности или у компании есть настройки аренды), телефон берётся из `User.phone_number`. После смены пароля все сеансы завершаются, запись в журнале (`action=password_reset`). Правила пароля: 8 символов и цифра.

## Свои значения в списках и ставки (доработка вечером 03.11.2026)

- `POST /api/arenda/payments/` (прочий приход, без `tenant`): принимает `category` (advertising / parking / other) ИЛИ новое `category_title` (свободный текст до 64 символов, без двоеточия). Обе вместе — 400. Описание платежа: `«{category_title}: {comment}»` (без комментария — просто название). В ответе и в списке оплат: `category: null`, `category_title` — часть описания до двоеточия; у известных категорий и у оплат арендаторов `category_title: null`.
- `branches`: `object_type` — свободный текст до 64 символов (длиннее — 400, поле `object_type`). Свои типы есть в списке филиалов, фронт берёт их оттуда.
- `GET/PATCH settings/`: все ставки и сроки уже записываются владельцем (`penalty_rate`, `soon_days`, `lease_expiring_days`, `indexation_notice_days`, `payroll_social_employee`, `payroll_income_tax`, `payroll_social_employer`, `advance_percent`, `advance_day`, `settle_day` (0 = последний день месяца), `tax_due_day`); добавлен тест на запись.

## Тесты

`python manage.py test apps.arenda --settings=core.settings_test_sqlite` — все зелёные. Тесты больше не зависят от сегодняшней даты (дата зафиксирована через mock) и не пишут файлы в боевой `media/` (в `ArendaBase` подменён `MEDIA_ROOT`).
