# Фикстуры Yandex Search API

- `vaccines_spb.xml` — записанный ответ синхронного запроса 01.10.2026 («поставщик вакцин оптом Санкт-Петербург», регион 2).
- `operation_pending.json`, `operation_done.json`, `operation_error.json`, `operation_answer.xml` — отложенный режим
  (`POST /v2/web/searchAsync` → `Operation`, `GET operation.api.cloud.yandex.net/operations/<id>`).
  **Собраны по спецификации, не записаны с API**: поля — из `yandex/cloud/operation/operation.proto` и
  `yandex/cloud/searchapi/v2/search_service.proto` (github.com/yandex-cloud/cloudapi, прочитано 02.10.2026),
  идентификаторы и XML выдуманы для теста. **[ПРОВЕРИТЬ] вживую не проверено — агентам вызывать API запрещено**
  (решение человека 02.10, D-096). `rawData` в `operation_done.json` — base64 от `operation_answer.xml`.
