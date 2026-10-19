# ТЗ для бэкенда NurCRM — часть 19: конструктор функций, подписка, права

_12.10.2026. Источник — запрос владельца NurMarket: «эти все функции должны как в конструкторе собираться». Макеты — холст «NurMarket — редизайн в стиле Coca», блок «Конструктор магазина» (К1–К8), «Роли и доступ» (Э2), «Функции и абонплата» (18)._

Эта часть — основа для частей 20–25: каждая новая функция включается и оплачивается через неё.

## 1. Задача

Магазин сам собирает кассу из функций: выбирает вид магазина (шаблон), включает и выключает функции, видит цену в месяц, настраивает права сотрудников. Сейчас:

- тарифы «Старт» и «Стандарт» на сервере флагами не различаются — касса угадывает по названию тарифа (`TariffGate`), это хрупко;
- платные модули кассы включаются серийными ключами локально (`LicenseKeys`), сервер о них не знает;
- права сотрудников — только роль, без набора разрешений.

Нужно: **один список включённых функций на сервере**, отдельно по каждому филиалу, с пробным периодом и ценой, и **права сотрудника по функциям**. Касса, телефон и веб читают это одинаково.

## 2. Справочник функций (заводится NurCRM, меняется без выпуска программ)

```
Feature
  code            — латиница, неизменный: kassa, warehouse, calculation, restock, supplier_returns, branches,
                    debts, installments, rentals, analytics, sizes, services, site, clients_bonus, gift_cards,
                    telegram_bot, whatsapp, funnel, salary, ai_advisor, scales, customer_display,
                    payments_terminal_qr, fiscal_kkm, esf, cancel_control, tasks, documents_1c
  group           — sale | stock | money | tax | clients | staff | site_ai_reports
  title           — {ru, ky, en, tr, uz}
  description     — {ru, ky, en, tr, uz}
  price_month     — сом
  is_base         — true только у kassa (всегда включена, нельзя выключить)
  requires        — [code]   функции, без которых эта не работает
  trial_days      — 7
  is_new          — показывать метку «новое»
  sort
```

Цены уже существующих функций — с доски «Функции и абонплата»: касса 990, склад 500, калькуляция 300, пополнение и сроки 300, возвраты поставщикам 200, филиалы 500, долги 400, прокат 400, аналитика 500, размеры и цвета 400, сайт 700, клиенты и бонусы 300, Telegram-бот 400, WhatsApp 300, воронка 300, зарплата 400, Нур Советник 600, весы 300, экран покупателя 200.
**Цены новых функций — предложение, утвердить владельцу:** ККМ и электронный чек 500, ЭСФ 400, терминал и QR 300, подарочные сертификаты 200, задачи 200, контроль удалений 200, накладные и 1С 300, услуги и запись 400. Рассрочка входит в «Долги» (как на доске 18).

Зависимости (`requires`), которые нужны сразу:

| Функция | Требует |
|---|---|
| `esf` | `fiscal_kkm` |
| `gift_cards` | `clients_bonus` |
| `installments` | `debts` |
| `site` | `warehouse` |
| `services` | — |
| `restock`, `calculation`, `supplier_returns` | `warehouse` |

## 3. Шаблоны (вид магазина)

```
Preset
  code       — grocery | clothes | services | rental | pharmacy_tech | custom
  title, description   — 5 языков
  features   — [code]
  is_soon    — true для «Кафе» (показать «скоро», нельзя выбрать)
```

Состав шаблонов — как в К1: «Продукты», «Одежда и обувь», «Услуги», «Прокат», «Аптека и техника», «Собрать самому» (только `kassa`). Шаблон лишь заполняет список; дальше владелец меняет функции поштучно.

## 4. Включённые функции компании и филиала

```
CompanyFeature
  company_id, branch_id (null = все филиалы), feature_code
  status          — trial | active | disabled | blocked_unpaid
  trial_until     — дата окончания пробного периода
  enabled_at, enabled_by, disabled_at, disabled_by
```

Правила:
- включение новой функции → `trial` на `trial_days`, потом `active` и в счёт; повторное включение после выключения пробного периода не даёт;
- включение функции с `requires` → сервер включает и зависимые, если их нет (ответ перечисляет, что включено дополнительно; касса показывает окно К6 «Включить обе?» **до** запроса, по `preview`);
- выключение **не удаляет данные**: долги, рассрочки, бонусы, сертификаты, графики сохраняются и доступны для погашения и просмотра; запрещается только создавать новое;
- выключение функции, от которой зависят включённые, → ошибка `required_by` со списком;
- `blocked_unpaid` — оплата просрочена: создавать новое нельзя, смотреть и гасить можно (как при выключении).

## 5. Права сотрудников по функциям

Право = `feature_code.action`. Набор действий по функциям (минимум):

| Функция | Действия |
|---|---|
| kassa | `sale`, `return` (с PIN / без), `discount`, `price_edit`, `shift_close`, `cash_in_out` |
| warehouse | `view`, `receive`, `audit`, `write_off`, `transfer`, `cost_view` |
| debts / installments | `sale_on_credit`, `pay`, `create`, `payoff`, `cancel`, `settings` |
| fiscal_kkm | `z_report`, `settings` |
| payments_terminal_qr | `accept`, `refund`, `manual_confirm` (только с PIN админа), `settings` |
| clients_bonus / gift_cards | `view`, `spend_bonus`, `sell_card`, `settings` |
| cancel_control | `delete_line_without_pin`, `report_view` |
| analytics | `view`, `money_view` |
| tasks | `create_for_others`, `view_all` |

```
Role           — owner | admin | cashier | storekeeper | custom
RolePermission — role_id, permission ('kassa.return'), mode: allow | allow_with_pin | deny
EmployeePermissionOverride — employee_id, permission, mode   (исключения конкретному сотруднику)
PermissionPreset — strict | trust | legacy   (кнопки «Строгий», «Доверяю», «Как было» в К4)
```

`legacy` («Как было») = то, что касса разрешает сейчас, чтобы обновление ничего не сломало.
Кассир по умолчанию (`strict`): продажа, оплата, ККМ (Z-отчёт), клиенты — да; возвраты и долг — с PIN; склад, закупочные цены, аналитика — нет. Это совпадает с макетом К4 и с решением владельца «кассир — только продажа и склад».

Права действуют только для **включённых** функций: выключенная функция скрывает и её права.

## 6. Счёт и оплата

- `price_month` × включённые функции × количество филиалов, где они включены (К7 считает итог по филиалам);
- пробные функции в сумму не входят, в ответе — дата первого списания;
- смена состава в середине месяца — пересчёт пропорционально дням (или с 1-го числа — решение NurCRM, касса только показывает сумму из ответа).

## 7. API

```
GET    api/main/features/catalog/                 справочник функций и шаблонов (раздел 2–3), 5 языков
GET    api/main/features/?branch_id=              включённые функции, статусы, trial_until
POST   api/main/features/preview/                 {branch_id, enable:[code], disable:[code]}
       → {will_enable:[code], blocked_by:[{code, required_by}], price_month_before, price_month_after,
          first_charge_date, data_kept:[{code, counts:{debts:12, installments:3}}]}
POST   api/main/features/apply/                   то же тело → применить; ответ = GET
POST   api/main/features/preset/                  {branch_id, preset_code} → заполнить по шаблону (как apply)
GET    api/main/billing/summary/                  {by_branch:[{branch_id, price_month}], total, trial:[{code, until}], next_charge}

GET    api/main/roles/                            роли и их права
PUT    api/main/roles/{id}/permissions/           [{permission, mode}]
POST   api/main/roles/{id}/preset/                {preset: strict|trust|legacy}
GET    api/main/employees/{id}/permissions/       итог для сотрудника (роль + исключения + включённые функции)
GET    api/main/me/permissions/                   то же для вошедшего — касса вызывает при входе и смене кассира
```

`data_kept` нужен окну «Выключить …?» (К6): «12 должников · 3 рассрочки — данные сохранятся».

## 8. Как касса узнаёт об изменениях

- при входе, при смене кассира и раз в 5 минут — `GET api/main/features/` и `me/permissions/`;
- в ответе `version` (число); касса кэширует и работает по кэшу без интернета;
- изменение в вебе применяется на кассе без перезапуска (кнопки появляются и исчезают).

## 9. Переход со старого

- серийные ключи (`LicenseKeys`), уже введённые на кассах, касса один раз отправляет `POST api/main/features/import-license/ {key}` → сервер включает соответствующие функции как `active` до конца оплаченного срока;
- тарифы «Старт»/«Стандарт» → набор функций: «Старт» = `kassa` + то, что было открыто ключами; «Стандарт» = текущий набор «Стандарта». После перехода касса **не смотрит на название тарифа**.

## 10. Ошибки

| Код | Когда |
|---|---|
| `required_by` | выключают функцию, нужную другим |
| `feature_disabled` | запрос к API выключенной функции на создание (касса показывает «Функция выключена — включить в Конструкторе») |
| `permission_denied` | у сотрудника нет права; в ответе `permission` и `mode` (если `allow_with_pin` — касса просит PIN админа и повторяет запрос с `pin_token`) |
| `unpaid` | `blocked_unpaid` |

PIN админа: `POST api/main/auth/pin/ {employee_id, pin}` → `pin_token` на 2 минуты, одно действие.

## 11. Приёмка

1. Выбрать шаблон «Продукты» → включено ровно то, что в К1, сумма 4 290 сом.
2. Включить `esf` без `fiscal_kkm` → `preview` предлагает включить обе, `apply` включает обе.
3. Выключить `debts` при 12 должниках → `data_kept` их показывает; после выключения долг можно погасить, нового в долг продать нельзя (`feature_disabled`).
4. Кассир с `strict`: возврат → `permission_denied` с `allow_with_pin`; с `pin_token` админа — проходит.
5. Включить функцию в вебе для «Филиал 2» — на кассе филиала кнопка появляется за ≤ 5 минут без перезапуска, на кассе «Главного» — нет.
