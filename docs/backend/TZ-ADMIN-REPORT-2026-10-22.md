# ТЗ «Админка» (tz-beka-backend п.8–10, 11.3, 11.4, 12.21, 12.23, 12.24) — 22.10.2026

Ветка `feature/tz-admin` (от `feature/buhgalter`). Тесты: `apps/construction/tests_admin.py` (20 тестов). Полный прогон main/construction/users/possync/warehouse: те же падения, что на базе (+20 новых тестов зелёные).

## Миграции (написаны руками, `.gitignore` дополнен)
- users `0063_can_edit_products_backfill` (данные: can_view_products=True и can_edit_products=NULL -> True)
- users `0064_adminpin_companypinsetting` (AdminPin, CompanyPinSetting, db_default)
- construction `0035_trashentry_auditlog_branch` (AuditLog.branch, TrashEntry). Также в git добавлена 0034.
Номера сверить с сервером перед migrate. Схема apps/main не менялась.

## п.8 Журнал
Тот же AuditLog. Эндпоинты: `GET main/audit-log/` = `GET construction/audit-log/`.
Фильтры: `object_type, object_id, action, user, branch, date_from, date_to, page` (по 50).
Ответ: `{count,page,page_size,results:[{id,created_at,action,user,user_name,object_type,object_id,object_name,before,after,ip,branch}]}`.
Новые action: `price_change` (before/after: price, purchase_price, markup_percent), `stock_change` (quantity), `delete` (object_type product|client|category|brand|sale), `rights_change` (object_type user; все can_* и role), `login` (object_type user), `restore`.
Видят: владелец/админ — всё; у кого только can_view_cashbox — лишь cashflow/period.
Пишется: PATCH products/{id}/, DELETE products/{id}/, bulk-delete, DELETE clients/categories/brands, DELETE pos/sales/{id} и sales/bulk-delete, PATCH users/employees/{id}/, users/auth/login/.
Не сделано: ручной остаток через другие пути (приходы/ревизии/bulk) не пишется; изменение цен через bulk-update (там цены нет). Хранение: чистки нет, живёт бессрочно.

## п.9 Корзина (TrashEntry, без deleted_at в main)
При DELETE снимок строки и каскада кладётся в TrashEntry; восстановление пересоздаёт. Если товар остаётся архивным (есть продажи) — у него освобождаются barcode/code/plu, restore снимает архив.
- `GET main/trash/?type=product|client|category|brand&search=&page=` -> `{count,page,page_size,retention_days:30,results:[{id,type,object_id,name,deleted_at,deleted_by,expires_at,days_left}]}`
- `POST main/trash/{type}/{object_id}/restore/` (object_id — id удалённого объекта) -> `{restored,type,id,name,notes[]}`; 404 если нет/истёк срок; 409 `{code:"restore_failed"}`. Если штрихкод/код/PLU занят новым товаром — возвращается без него, причина в notes.
- Права: видеть — can_view_* раздела, вернуть — не can_edit=False.
- Просроченное чистится при открытии списка и командой `manage.py purge_trash` (повесить на cron).
- Ограничения: категории/бренды MPTT — после возврата `rebuild()`; связи SET_NULL (товар->категория) восстанавливаются по сохранённым id. Удаление клиента с долгами/сделками каскадом: снимок до 5000 объектов, больше — только сама строка. Кнопка отмены удаления продаж не входит в корзину.

## п.10 PIN
- `POST users/auth/pin/` `{pin:"1234", current_pin?}` -> `{has_pin:true}` (владелец/админ; при смене нужен current_pin; хэш)
- `POST users/auth/pin/verify/` `{pin}` -> `{token, expires_in:300}`; неверный: 400 `{code:"pin_invalid", attempts_left}`; 5 ошибок: 429 `{code:"pin_locked", retry_after}` на 15 мин. Сотрудник без своего PIN подтверждает PIN-ом владельца/админа.
- `GET/PATCH users/auth/pin/settings/` `{require_pin}` -> `{require_pin, has_pin}` (PATCH только владелец/админ; включить можно после задания PIN).
- Защита `@pin_guard` (apps/users/pin.py): DELETE products/{id}/, DELETE products/bulk-delete/, POST pos/sales/{id}/return/, POST construction/cashflows/{id}/cancel-request/. Без `X-Confirm-Token`: 403 `{code:"pin_required"}`. Токен подписан, 5 минут, привязан к пользователю и компании.
- Не покрыто: «массовое изменение цен» — отдельного эндпоинта нет (bulk-update меняет только бренд/категорию/поставщика).

## п.11.3 can_edit_products
Проверка уже стояла (read_only_guard на products/{id}, bulk-update, bulk-delete, create, категории, бренды): False -> 403 `{code:"read_only"}`, owner/admin всегда можно. Добавлена миграция-бэкфилл 0063.

## п.11.4 cashboxes
`GET construction/cashboxes/` теперь всегда содержит `branch` (id или null).

## п.12.21 Профили зарплаты
`branch` записываемый (`null` = для всех филиалов), всегда в ответе. Не передан: create берёт активный филиал, PATCH не трогает. Чужой филиал -> 400. Список без активного филиала отдаёт все схемы компании.

## п.12.23
`charts.sales_dynamics[]` = `{date, value, transactions}`.

## п.12.24
`GET main/analytics/market/?tab=stock_items&page=1&page_size=200(<=1000)&search=&category=&kind=&sort=name|quantity|purchase_price|price|stock_value&order=asc|desc&only_in_stock=1&abc=1`
-> `{tab,count,page,page_size,totals:{quantity,stock_value,retail_value},items:[{id,name,barcode,code,unit,category,category_name,quantity,purchase_price,price,stock_value,class?}],abc?:{thresholds,counts:{A,B,C}}}`. Услуги и архив не входят; класс ABC — по стоимости остатка (закупка).
