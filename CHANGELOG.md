# CHANGELOG (серверная ветка NurCRM)

## 2026-10-27 — fix/server-bugs-2026-10-27 (поверх fix/price-sync-2026-10-27)
Срочные баги, которые нашла касса (Бекназар), документ «ТЗ Умару — СРОЧНО баг сервера».
- GET main/products/list/?ordering=-updated_at (и updated_at) теперь сортирует по времени изменения. Раньше избранные товары всегда шли первыми, и «версия каталога» кассы (page_size=1) не менялась. Файл: apps/main/views.py (ProductListView.filter_queryset). Тест: apps/main/tests_products_ordering_2026_10_27.py.
- Возврат строками у чека со скидкой на чек учитывает долю этой скидки (чек 4,00 со скидкой 0,40 → возврат 3,60, а не 4,00). Сумма всех возвратов не больше оплаченного, последний возврат — ровно остаток. В ответе возврата новое поле refund, у повтора replayed: true. Повторный возврат полностью возвращённого чека: 400 c code refund_exceeds_paid (статус прежний, чтобы старые кассы не сломались). Перенесено из ТЗ Н-02 (fdd6391). Файлы: apps/main/pos_views.py, apps/main/tests_tz_n02_return_discount.py.
- GET main/pos/sales/?page=<за концом> отвечает 200 с пустым results и next: null вместо 500. Перенесено из ТЗ Н-28 (d65a917). Файлы: apps/main/views.py (SupplierReceiptLimitPagination), apps/main/tests_tz_n28_sales_pages.py.
- Не сделано: аванс в clients/{id}/kpis ждёт формулу от программы владельца.
