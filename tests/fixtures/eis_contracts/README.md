# Фикстуры `eis_contracts` (#244)

Реальные страницы zakupki.gov.ru из архива Wayback Machine: 02.10.2026 zakupki.gov.ru с машины
разработчика не ответил (TLS handshake timeout на `/robots.txt` дважды, выход в интернет — не из РФ),
живых страниц реестра контрактов нет. Тело страницы — оригинальный ответ сайта в архиве (режим `id_`,
без панели Wayback), байт в байт. Публичные данные юрлиц, обезличивание не нужно.

| Файл | Исходный URL | Снимок Wayback | sha256 |
|---|---|---|---|
| `search_supplier_7733284010.html` | `https://zakupki.gov.ru/epz/contract/search/results.html?…&supplierTitle=7733284010&…&customerIdOrg=…ФНС…&contractDateFrom=26.05.2023&fz44=on&sortBy=UPDATE_DATE` | `20251224054604` | `8b4a2426…4d1c` |
| `card_1771014610219000243.html` | `https://zakupki.gov.ru/epz/contract/contractCard/payment-info-and-target-of-order.html?reestrNumber=1771014610219000243` | `20260514084345` | `3e360ed6…0bbd` |
| `card_3263207897522000001.html` | `https://zakupki.gov.ru/epz/contract/contractCard/payment-info-and-target-of-order.html?reestrNumber=3263207897522000001&contractInfoId=76533642` | `20250419075907` | `08238f80…75ae` |
| `search_no_results.html` | `https://zakupki.gov.ru/epz/contract/search/results.html?searchString=6829037099` | `20260504013733` | `f97cc2de…485c` |

Скачать заново: `https://web.archive.org/web/<снимок>id_/<исходный URL>` (полный URL поиска поставщика —
в CDX: `https://web.archive.org/cdx/search/cdx?url=zakupki.gov.ru/epz/contract/search/results.html&matchType=prefix&from=2025`).

Что видно на страницах:
- поиск по поставщику (`supplierTitle=<ИНН>`): «Результаты поиска 47 записей», 10 контрактов на странице:
  номер, статус («Исполнение» / «Исполнение завершено»), заказчик, «Заключение контракта», ИКЗ;
  кодов ОКПД2 и ИНН поставщика на странице нет;
- карточка, вкладка «Платежи и объекты закупки»: та же шапка (номер, статус, заказчик, дата заключения)
  и таблица «Объекты закупки» с колонкой «Позиции по КТРУ, ОКПД2» — код в скобках «(62.01.11.000)»;
- `search_no_results.html` — разметка «Поиск не дал результатов» реестра контрактов (запрос был
  `searchString=<ИНН>`, не фильтр поставщика).

Ни одной карточки контракта из списка поискового снимка в архиве нет, поэтому положительный путь в
тестах берёт список контрактов из шапки самой карточки (`_listed` в `tests/unit/test_eis_contracts.py`).
Производные страницы (каждая помечена в тесте `tests/unit/test_eis_contracts.py`): выдача с дописанными номерами карточек (`_listed`), выдача без даты заключения, выдача с другим статусом (2 варианта), карточка с кодами без скобок, карточка с дописанным чужим номером.
