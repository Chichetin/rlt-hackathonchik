# Записанные ответы запасных источников enrich() (#58)

Настоящие ответы, записаны 01.10.2026 (сессионные `token` убраны, `recorded_at` — unix-время):

- `rmsp_quick_7804428656.json` — `POST https://rmsp.nalog.ru/search-proc.json`, `mode=quick&query=7804428656`
  (ООО «БРАСС», МСП): строка реестра;
- `rmsp_quick_7736050003.json` — тот же запрос по ПАО «Газпром» (не МСП): `data: []`;
- `bo_7736050003.json` — `GET https://bo.nalog.gov.ru/advanced-search/organizations/search?query=7736050003&page=0`:
  ПАО «Газпром», название, статус, ОКВЭД2 (выручка в тестах не используется).
