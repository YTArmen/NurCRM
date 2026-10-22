# Отчёты: история товара, часы/источники, скорость продаж (22.10.2026)

Ветка `feature/tz-reports` (от `feature/buhgalter`). Код: `apps/possync/views_reports.py`, маршруты в `apps/possync/urls.py`, тесты `apps/possync/tests_reports.py` (9 тестов, весь `apps.possync` — 34 OK). Схема БД не менялась.

## 1. GET /api/main/products/{id}/history/?date_from&date_to&branch&page&page_size
Ответ: `{product:{id,name,quantity}, count, page, page_size, results:[...]}` (page_size по умолчанию 50, максимум 200).
Строка: `at` (ISO), `type` (sale|return|receipt|supplier_return|write_off|count_fix|receive|inventory|transfer_in|transfer_out), `qty_delta` (строка, со знаком), `price`, `doc_id`, `doc_number`, `user` (имя или email), `branch` ({id,name} или null), `before`, `after` (есть у корректировок и инвентаризации, иначе null).
У `return` дополнительно: `restocked`, `is_defect`, `reason`; у корректировок `reason`.
Источники: SaleItem (чеки paid/debt/partially_returned и отменённые, у которых есть возврат), SaleReturn.returned_items, SupplierReceiptItem, SupplierReturnItem (posted), StockAdjustmentLine (receive/write_off/count_fix/return_to_supplier), ProductInventoryItem (сессии applied), BranchTransferItem (completed; product=out, dest_product=in).
Слияние: каждый источник отдаёт верхние page*page_size строк по времени, потом общая сортировка и срез; count считается отдельными COUNT.
Особенности: частичный возврат уменьшает quantity в чеке, поэтому для sale показываем quantity + частично возвращённое (иначе двойной учёт). Возврат брака: qty_delta=0, restocked=false. Номер есть только у чеков, корректировок и перемещений; у приёмок/возвратов поставщику нет (doc_number=null).
Не вошло: MarketStockTransferItem (между складами, суммарный остаток не меняется), StockMovement (журнал склада сфер «производство/агент», для маркета не пишется), ручная правка Product.quantity (нигде не логируется). Полностью возвращённая строка при частичном возврате удаляется из чека; на Postgres такой возврат находится по JSON, но парной строки sale нет (в тестах на sqlite эта ветка не проверена).

## 2. GET /api/main/analytics/market/hours-sources/?date_from&date_to&branch
Период/филиал как у analytics/market (по умолчанию текущий месяц; чеки paid и partially_returned по paid_at). Старый эндпоинт не тронут.
```
{"period":{"date_from","date_to_exclusive"},"timezone":"Asia/Bishkek","branch":null|"uuid",
 "hours":[{"hour":0..23,"count":n,"sum":"0.00"} x24],
 "sources":[{"source":"cashbox|showcase|other","label":"...","count":n,"sum":"0.00"}]}
```
Источники: showcase — чек привязан к ShowcaseOrder.sale; cashbox — у чека есть касса или смена; other — всё остальное. Веб-касса и мобильное приложение в чеке не различаются (поля source у Sale нет), для разделения нужна миграция.

## 3. GET /api/main/products/sales-velocity/?days=30&branch&product_ids=id,id&page&page_size
Ответ: `{days,count,page,page_size,results:[{product_id,name,barcode,sold_qty,per_day,stock,minimum_quantity,days_left}]}`; `days` 1..365; `days_left` = null, если продаж нет. Без product_ids: товары с продажами за период или с остатком <= minimum_quantity (при minimum_quantity > 0). Порядок: меньше дней остатка сверху. Архивные товары исключены.
