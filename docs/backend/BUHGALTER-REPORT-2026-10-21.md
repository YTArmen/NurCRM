# «Бухгалтер» — бэкенд (ветка feature/buhgalter, 21.10.2026)

ТЗ: front-market/docs/tz-beka-buhgalter.md. Сделаны п.1–6, тесты: `apps.construction.tests_buhgalter` (16),
`apps.main.tests_supplier_payables` (2) — проходят; старые падения те же, новых нет.

| Коммит | Пункт |
|---|---|
| 7b7d6dd | 1. «Только смотреть»: 8 полей `can_edit_*` (null = как раньше), 403 `read_only`, декоратор `@read_only_guard` |
| 365685f | 2. Закрытие периода для касс, edit/cancel-request, возвратов, оплат долга (400 `period_closed`) |
| 8038334 | 3. Статьи: `type/archived/code`, 11 статей по умолчанию (лениво), `*_by_category` в `tab=finance` |
| 30c7dc6 | 4. `GET construction/cashboxes/balances/` |
| d025d8c | 5. `GET main/suppliers/payables/` |
| e0b3c0c | 6. «Проверено»: `POST construction/cashflows/{id}/check/`, `?checked=` |

## Не сделано
- п.7 журнал изменений, п.8–9.
- Статья «Зарплата» у серверных выплат; P&L `opex` по статьям; «проверено» на продаже;
  `can_edit_salary` ни к чему не подключено; закрытие периода для `bulk/status`; возврат проверяется по дате
  возврата, не по дате чека.

## ПЕРЕД ВЫКАТКОЙ (обязательно)
1. **Ветка выросла из `feature/tz-17-28-backend`** (ТЗ-28, не выкачен): `read_only` использует `core.exceptions.ApiError`,
   которого на сервере нет. Выкатывать вместе с ТЗ-28 или заменить `ApiError` на `PermissionDenied` с `code`.
2. Миграции: users `0062_user_can_edit_flags` (после серверной 0061 — номер уже поправлен),
   construction `0032`, `0033` (после серверной 0031 — совпадает). Перед `migrate` ещё раз сверить `ls` на сервере.
3. Декоратор `@read_only_guard` стоит на классах в construction/views.py, main/views.py,
   warehouse/views_documents.py, users/views.py — подклассы наследуют. Для всех, у кого поля `null`, поведение прежнее.
4. Предупредить Беку: правки затрагивают кассы, продажи (возвраты) и права всех компаний.
