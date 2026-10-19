# ТЗ для бэкенда NurCRM — часть 25: товары (варианты одежды, услуги, описание и фото), закупки, документы

_12.10.2026. Источник — запросы владельца: «добавить описание и фото, поиск и добавление через интернет», «добавление услуг и одежды на склад», «дизайн закупки», «экспорт, печать накладных». Макеты — блоки «Новый товар» (Н1–Н8), «Закупки» (З1–З6), «Накладные» (Д1–Д5), «Размеры и цвета» (26), М14. Функции в конструкторе — `sizes`, `services`, `warehouse`, `restock`, `documents_1c` (часть 19)._

Из AI_HANDOFF: в NurCRM **у товара нет вариантов (размеров)**; режим «Одежда» в кассе сейчас работает без сервера. Этот документ закрывает пробел.

---

## А. Тип позиции и варианты

```
Product
  … существующие поля …
  item_type     — goods | clothes | service | rental        (тип в окне «Новая позиция»)
  description   — {ru, ky?}                                  (для сайта и бота; кыргызский по кнопке)
  photos        — [{url, sort, is_main, source: upload|camera|internet, source_url?}]
  brand, article
  gked_code, tnved_code, traceable                           (часть 23)
  show_on_site  — bool
  size_grid_id? — сетка размеров

SizeGrid        — id, company_id, title ('Одежда S–XXL', 'Обувь 36–46', 'Детская 92–152'), sizes:[…]
ProductVariant
  id, product_id, size, color, color_hex
  barcode       — свой у каждого варианта (печать бирок)
  price?        — своя цена (например, «XXL +10 %»), иначе цена товара
  stock         — остаток по складу (склад — часть 17)
```

- Продажа, приёмка, ревизия, списание и перемещение — **по варианту**, если они есть: в строке `variant_id`.
- Скан штрихкода варианта → `{product, variant}`: касса сразу кладёт в чек нужный размер и цвет.
- Создание пачкой (Н5, Н6): `POST api/main/products/ {…, item_type: clothes, variants:[{size, color, qty, price?}]}` → варианты, штрихкоды (генерация EAN-13 из префикса компании) и приход по каждой клетке «цвет × размер».
- Аналитика: «Размеры и цвета» — продажи и остатки по вариантам (что кончается, что не продаётся).
- Сайт: покупатель выбирает размер и цвет и видит только то, что есть в наличии.

## Б. Услуги и запись

```
Product(item_type = service)
  duration_min
  no_stock = true                — остатка нет, продаётся всегда
  consumables — [{product_id, qty}]   — списываются со склада при продаже услуги
  online_booking — bool

ServiceMaster   — employee_id, service_id, price?, percent        (своя цена и процент мастеру)
Booking
  id, branch_id, service_id, master_id, client_id, start_at, end_at,
  status — booked | came | no_show | canceled | done, sale_id?, source — kassa | site | bot | phone
```

- продажа услуги → строка с `master_id`; начисление мастеру (`percent`) — в зарплату (существующая функция «Зарплата и табель»);
- журнал записи: `GET api/main/bookings/?date=&master_id=`, `POST`, `PATCH` (С3, С7 — касса услуг);
- напоминание клиенту в WhatsApp за 2 часа;
- в отчётах услуги отдельно от товаров.

## В. Описание, фото и поиск в интернете

Касса уже умеет искать фото и штрихкоды (`ProductPhotoFinder`, barcode-list.ru) — но прямо с кассы, и это ломается при блокировках. Нужен серверный помощник, чтобы касса, телефон и веб работали одинаково.

```
POST api/main/products/lookup/  {barcode? , name? , photo? (multipart)}
  → {candidates:[{name, brand, description, photos:[url], category_hint, source, source_url,
                  match: exact_barcode | similar | photo_only}]}
```

- источники — на выбор NurCRM (открытые базы штрихкодов, поиск картинок). **Цена из интернета не возвращается никогда**;
- выбор «что взять» (название, фото, описание, категорию) — на клиенте. На сервер приходит обычное создание товара с `photos[].source = internet` и `source_url`;
- фото: `POST api/main/products/{id}/photos/` (multipart) → хранение, превью; «убрать фон» — `POST …/photos/{pid}/remove-bg/` (по желанию, можно позже);
- описание с Нур Советником — на стороне клиента (уже есть ИИ). «На кыргызском» — перевод описания: сохраняется в `description.ky`.

## Г. Закупки (заказы поставщикам)

```
Supplier — id, name, phone_whatsapp, inn?, pay_terms_days
PurchaseOrder
  id, number, supplier_id, branch_id, status — draft | sent | in_transit | partially_received | received | canceled
  lines — [{product_id, variant_id?, qty_ordered, price_expected}]
  expected_at, payment — now | credit_until, created_by, sent_at (WhatsApp)
SupplierPayment — id, supplier_id, purchase_order_id?, amount, method, from — cash_kassa | card | transfer, shift_id?
```

- «Пополнение» предлагает строки заказа (уже есть расчёт «что заказать»);
- «Отправить в WhatsApp» — текст заказа (З3);
- приёмка по заказу: «заказано / пришло / разница», недостача остаётся в заказе или закрывается без неё (З4, З5);
- неоплаченное — долг поставщику; оплата из кассы уходит изъятием смены (З6);
- изменение закупочной цены при приёмке → предложение новых цен продажи (наценка, округление) — как в макете З4.

```
GET/POST api/main/suppliers/
GET/POST api/main/purchase-orders/?status=
PATCH    api/main/purchase-orders/{id}/
POST     api/main/purchase-orders/{id}/send/
POST     api/main/purchase-orders/{id}/receive/      {lines:[{line_id, qty_received}], close_missing: bool}
POST     api/main/supplier-payments/
GET      api/main/suppliers/debts/
```

## Д. Документы: накладные, печать, экспорт, 1С

Документы уже есть по смыслу: продажа, приёмка, перемещение (`накладная` NurCRM при перемещении между филиалами), списание, возврат поставщику. Нужно:

1. **Единый журнал документов:** `GET api/main/documents/?type=sale|receiving|transfer|write_off|supplier_return&from=&to=&q=&page=` → `[{id, type, number, date, counterparty, sum, status}]` (Д4).
2. **Постоянные номера документов** — как номер чека (часть 20), по типу.
3. **Печатные формы на сервере** (одинаковые на кассе, телефоне и в вебе): `GET api/main/documents/{type}/{id}/print/?form=invoice|receipt|bill|reconciliation&paper=a4|a5|80mm&lang=ru|ky&stamp=1&logo=1&words=1&qr=1&cost=0` → PDF.
   Формы — Д3: накладная A4, товарный чек 80 мм, счёт на оплату, акт сверки с поставщиком A5.
4. **Реквизиты компании для бланков:** название, ИНН, адрес, логотип, картинка печати и подписи — `PUT api/main/company/requisites/`.
5. **Пакетный экспорт:** `POST api/main/documents/export/ {types[], from, to, columns[], format: xlsx|csv|pdf_zip}` → файл (Д2). Закупочные цены — только с правом `warehouse.cost_view`.
6. **Выгрузка для 1С:** `POST api/main/documents/export-1c/ {from, to, branch_id}` → CommerceML или XML (Д2). Перед выгрузкой — список проблем (товары без штрихкода, поставщики без ИНН). Автоотправка бухгалтеру: `PUT api/main/documents/export-1c/schedule/ {day_of_month: 1, email}`.
7. **Отправка:** `POST api/main/documents/{type}/{id}/send/ {channel: whatsapp|telegram|email, to, text}` — PDF вложением или ссылкой.

## Е. Приёмка

1. Футболка: сетка S–XL × 3 цвета, 46 шт → 12 вариантов со штрихкодами; скан бирки «Белый M» → в чеке этот вариант, остаток −1 только у него.
2. Услуга «Стрижка 45 мин», мастер 1 (600, 40 %) → продажа → мастеру начислено 240; шампунь 10 мл списан.
3. Поиск по штрихкоду 4870001234567 → кандидат «точно» с фото и описанием, без цены.
4. Заказ поставщику из 4 строк → WhatsApp → приёмка 3 из 4 → недостача осталась в заказе → долг поставщику → оплата из кассы уменьшила наличные смены.
5. Накладная по чеку → PDF A4 с печатью и суммой прописью, на кыргызском — тоже; экспорт месяца в Excel — 45 документов; выгрузка 1С показывает предупреждения.
