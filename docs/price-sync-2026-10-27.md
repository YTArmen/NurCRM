# Цена продажи: баги Б-06 / Б-46 / Б-03 (ТЗ от команды кассы, 27.10.2026)

Ветка `fix/price-sync-2026-10-27`. База ветки — снимок боевого сервера
(`fbf8eeb4` + правки без коммита, см. `docs/server-hotfix/`), поэтому файлы ветки
можно копировать на боевой один в один.

Миграций нет (`makemigrations --check` — изменений не найдено).

## 1. Б-06: PATCH purchase_price больше не меняет цену продажи

Было: `Product.save()` при `markup_percent > 0` всегда пересчитывал `price = purchase_price × (1 + наценка)`,
а ветка «пришёл только purchase_price» в `ProductSerializer.update()` делала то же явно.
Закупка 150→160 при наценке 30 % давала 195→205.

Стало (`apps/main/models.py::_recalc_price`, `apps/main/serializers.py::update`):

| Запрос | price | markup_percent |
|---|---|---|
| `PATCH {"purchase_price": "160"}` | не меняется | пересчитывается от новой закупки к текущей цене |
| `PATCH {"purchase_price": "160", "recalc_price": true}` | = 160 × (1 + наценка) | не меняется |
| `PATCH {"recalc_price": true}` | = закупка × (1 + наценка) | не меняется |
| `PATCH {"price": "200"}` | 200 | пересчитывается |
| `PATCH {"markup_percent": "40"}` | пересчитывается | 40 |

Ответ PATCH/PUT: добавлено `price_changed: true|false` (менялась ли цена продажи этим запросом); `markup_percent` как раньше.

Приход поставщика `POST suppliers/{id}/receipt/`: строка `{"product", "qty", "purchase_price"}` цену не трогает,
пересчитывает наценку. Строка с `"recalc_price": true` пересчитывает цену по текущей наценке.
Массовый приход `POST products/mass-incoming/`: в строке с `product_id` приняты `purchase_price` и `recalc_price`
с той же логикой; явный `price` в строке работает как раньше.

Модель: `save()` существующего товара цену не трогает (любая ревизия, остаток, имя). Пересчёт только если
код-путь выставил `product._recalc_price_from_markup = True`. При создании товара без цены, но с наценкой
цена по-прежнему считается из закупки (импорт, create-by-barcode).

Журнал: смена цены продажи пишется в `AuditLog` (`object_type=product`, `action=price_change`, before/after,
кто и когда) — через PATCH (было и раньше), приход и массовый приход (добавлено).

Приёмка из ТЗ (товар закупка 11, цена 15): `PATCH {"purchase_price":"1.00"}` → `price 15.00`,
`price_changed:false`; приход с закупкой 1.00 → `price 15.00`; `PATCH {"purchase_price":"11.00","recalc_price":true}`
→ `price 165.00` (наценка после первого шага 1400 %), `price_changed:true`. Покрыто тестами.

## 2. Б-46: updated_at и лента изменений

Что проверено по коду боевого: PATCH (полный `save()`), приход (`ProductQuerySet.update()` сам подставляет
`updated_at`), продажа и возврат (явный `updated_at`), массовый приход — все двигают `updated_at`.
Запрос кассы `products/list/?ordering=-updated_at&page_size=1` поддерживается. Воспроизвести «версия не менялась»
по коду не удалось; вероятная причина — область выборки (`?branch=`: касса без филиала не видит товар филиала и
наоборот). Нужен точный пример: товар, действие, филиал, время.

Что сделано, чтобы закрыть вопрос независимо от причины:

1. `Product.save(update_fields=[...])` всегда добавляет `updated_at` (auto_now без этого в БД не пишется).
2. Новый эндпоинт **`GET /api/main/products/changes/?since=<курсор>&limit=500`** (`apps/main/product_changes.py`):

```json
{
  "changes": [
    {"id": "…", "name": "…", "price": "195.00", "purchase_price": "150.000", "markup_percent": "30.0000",
     "discount_percent": "0.00", "wholesale_price": "0.00", "quantity": "10.000", "updated_at": "2026-10-27T12:00:00+06:00"}
  ],
  "deleted": ["<uuid>"],
  "cursor": 1761550000000,
  "server_time": "2026-10-27T12:00:01+06:00",
  "server_time_ms": 1761550001000,
  "has_more": false,
  "catalog_version": "…"
}
```

- `since` — `cursor` из прошлого ответа (мс с эпохи) или ISO 8601; без `since` — начальная загрузка.
- `has_more: true` — повторить сразу с новым `cursor`.
- `ETag` / `If-None-Match` → `304` без тела, пока каталог не менялся (один агрегирующий запрос).
- Область та же, что у `products/list/` (компания, `?branch=`, без архивных).
- Опрос раз в 5–10 с.

Уже есть и тоже подходит: `GET products/catalog-version/` — хеш от количества, max(updated_at) и сумм
остатков/цен/закупок. Надёжнее, чем смотреть один `updated_at`.

## 3. Б-03: сверка цены чека с каталогом

`POST pos/checkout/` (и `pos/checkout/batch/`, и мобилка — тот же путь):

- Строка чека с `price`, отличающейся от текущей цены каталога, **без** скидки, **без** `price_override: true`
  (в строке или в теле чека) и не по оптовой цене — считается расхождением.
- По умолчанию продажа проходит как раньше (оффлайн-досылка), в ответе
  `"price_mismatch": [{"index", "product_id", "name", "sent", "current"}]` (пустой список, если расхождений нет).
  В журнал пишется `AuditLog` (`object_type=sale`, `action=price_mismatch`).
- С заголовком `X-NurMarket-Strict-Price: 1` чек **не проводится**: `409`
  `{"code": "price_changed", "detail": "…", "price_mismatch": [...]}`. В batch — статус 409 у этой строки, остальные
  продажи проходят.
- Нижняя граница (не ниже закупки) — как раньше.

## 4. Нур Советник (раздел 4 ТЗ)

Это фронт сайта владельца, в локальных репозиториях (`front-market`, `front-usluga`, `nurcrm-app`) страницы нет —
только ключ прав `advisor`. Передать команде сайта (репозиторий undef1ned7/Nur).

## 5. Выкатка на боевой

Нужно скопировать 7 файлов и перечитать gunicorn. Миграций нет.

```
apps/main/models.py
apps/main/serializers.py
apps/main/views.py
apps/main/kassa_views.py
apps/main/mass_incoming_views.py
apps/main/urls.py
apps/main/product_changes.py      (новый)
```

Перед копированием — резервная копия каждого файла (`cp f f.bak_YYYYMMDD_HHMM`), после — `kill -HUP <master gunicorn>`.
Тесты (`apps/main/tests_price_sync_2026_10_27.py`, 25 шт.) прогнать после копирования можно и на сервере,
но лучше не нагружать боевой — они уже прошли локально на том же коде.

Проверка на боевом (NBS, `market@nur.kg`, товар на 1 сом): сценарий приёмки из п.1, затем
`GET products/changes/` без параметров и с `since` — товар должен появиться после PATCH.

## 6. Что ответить команде кассы

- Берём все три бага. Контракт как в ТЗ: `recalc_price`, `price_changed`, `price_mismatch`,
  заголовок `X-NurMarket-Strict-Price`, плюс `price_override` (в строке и в теле чека) и лента `products/changes/`.
- Б-06 и Б-03 готовы в коде и покрыты тестами, Б-46 — лента готова; «updated_at не менялся» по коду не
  воспроизводится, просим пример с филиалом.
- Время выкатки — после подтверждения Умара (см. п.5).

## Тесты

- Новые: `apps/main/tests_price_sync_2026_10_27.py` — 25 тестов, все проходят.
- Старые `SupplierReceiptSellingPriceTests` в `apps/main/tests.py` починены (фикстура была нерабочей: нет `username`,
  роли `COMPANY_OWNER`, импорта `Client`) и переведены на новый контракт — 5 проходят.
- Регрессия `tests_kassa_api` (68), `tests_price_check`, `tests_quick_restock_receipt`, `tests_price_override_flag`,
  `tests_tz_bugs_11_12`: те же 6 падений, что и на нетронутом снимке боевого (аддон `client_app`, долг→paid,
  вебхуки без Redis, поиск по имени на SQLite) — к этим правкам не относятся.
