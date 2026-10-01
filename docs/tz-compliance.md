# Соответствие условиям: пункт → артефакт → проверка → статус

Источник: раздел 0 `docs/source/hackathon-research-workflow.md` (таблица 10 пунктов). Строка
обновляется **в том же PR**, что и артефакт. Статус без команды-проверки — не статус: `jury`
его не засчитывает. `[СПРОСИТЬ Q-12]` если критерии и веса отличаются — таблица переписывается
под них (промпт 2).

Статусы: `нет` → `заглушка` → `частично` → `готово` (с командой и её выводом в PR).

| # | Пункт условий | area: | Артефакт | Проверка (команда) | Статус |
|---|---|---|---|---|---|
| 1 | Анализ данных организаторов | data | `research/PROBLEM.md` (профиль), 2–3 инсайта в `pitch/facts.md` | `uv run python -m src.ingest.profile` (цифры профиля), `uv run python -m src.ingest.build` (4 parquet, 12.5 с; `pytest tests/unit/test_ingest.py`) `[СПРОСИТЬ Q-12]` | частично: профиль и ingest есть, инсайтов в `pitch/facts.md` нет |
| 2 | Бизнес-смысл полей | data | `research/DATA_DICTIONARY.md` | каждое поле с уверенностью и «как проверили» | нет |
| 3 | Методология подбора | match | `research/METHODOLOGY.md`, `configs/search.yaml` | `make eval EXP=<id>` против baseline | заглушка (v0 текстом) |
| 4 | Поиск в открытых источниках | sources | блок «Новые из открытых источников», `src/sources/` | e2e: в блоке есть компания с URL источника; precision@10 блока B | заглушка |
| 5 | Обогащение данных | sources | `enrich()`, `src/enrich/evidence.py`, карточка | `pytest tests/unit/test_evidence.py tests/unit/test_enrich.py`; `curl /api/counterparty/<inn>`; покрытие обогащения (EVAL C) | `enrich()` из pb.nalog.ru через кеш, Evidence на каждый факт (D-028); запасные источники не сделаны |
| 6 | Удобный веб-интерфейс | app | `src/ui/`, `docs/ui.md` | `make e2e` (демо-сценарий); `operator` ≤ 90 с | заглушка на данных (поиск и обогащение — stub); номер закупки → запрос из извещения работает (`tests/unit/test_api.py::test_lot_*`, `make e2e`: «номер закупки → выдача», на фикстуре, не на реальном лоте); карточка: статистика, факты с URL и датой, «ИНН не подтверждён» |
| 7 | Тест на реальных кейсах | data | 5–10 закупок СПб: прогон, метрики, разбор | `make eval`, скриншоты в `pitch/` | нет |
| 8 | Масштабирование | app | `pitch/architecture.md`, `docker-compose.yml` | `make up` → `/api/health`; замер latency командой | частично (compose работает на stub) |
| 9 | Демонстрация | app | `make demo`, запасное видео | `make demo` с чистого клона на машине показа | заглушка |
| 10 | Презентация | app + все | `pitch/outline.md`, `pitch/QA.md`, `pitch/facts.md` | `jury` режим 1 и 2 | нет |
