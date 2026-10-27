# CHANGELOG бэкенда (ветка feature/tz26-b52-b87)

Новые записи сверху. Единое ТЗ бэкенду 26.10.2026, разделы 8–11 (Б-52…Б-87).

## 27.10.2026 — каркас Б-52…Б-87
- Что: общие поля новых пунктов в существующих моделях (Product, CartItem, SaleItem, Sale, Client, Task), пакет `apps/main/tz26/` (куски hw_units, hw_prices, hw_catalog, cust_orders, staff, ai_stock, pos_sync, pos_print), новое приложение `apps/inbox` («Чаты»), подключение адресов (`api/main/…`, `api/users/…`, `api/public/…`, `api/main/inbox/…`, `api/inbox/…`).
- Зачем: чтобы куски ТЗ делались параллельно и не правили одни и те же файлы. Все новые поля необязательные — старые ответы API не меняются.
- Файлы: apps/main/models.py, apps/main/urls.py, core/urls.py, core/settings.py, apps/main/tz26/**, apps/inbox/**.
- Миграции — одной пачкой после слияния кусков.
