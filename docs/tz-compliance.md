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
| 3 | Методология подбора | match | `research/METHODOLOGY.md` («Реализация v1»), `src/match/search.py`, `configs/search.yaml` (`retriever: bm25`) | `uv run pytest tests/unit/test_search.py` (компоненты, объяснения, `as_of` без утечки); `make eval EXP=<id>` против baseline — после кейсов T-07 (E-001 — только на фикстуре) | частично: v1 работает (4 компоненты из 6: product_fit, experience, activity, geography; reliability и price — «нет данных», D-029; сверка с кодом и предложения — METHODOLOGY.md, «На подтверждение человеку (№3)»; ждёт подтверждения №3), метрик на реальных кейсах нет |
| 4 | Поиск в открытых источниках | sources | блок «Новые из открытых источников», `src/sources/` | e2e: в блоке есть компания с URL источника; precision@10 блока B | заглушка |
| 5 | Обогащение данных | sources | `enrich()`, `src/enrich/evidence.py`, карточка | `pytest tests/unit/test_evidence.py tests/unit/test_enrich.py`; `curl /api/counterparty/<inn>`; покрытие обогащения (EVAL C) | `enrich()` из pb.nalog.ru через кеш, Evidence на каждый факт (D-028); запасные источники не сделаны |
| 6 | Удобный веб-интерфейс | app | `src/ui/`, `docs/ui.md` | `make e2e` (демо-сценарий); `operator` ≤ 90 с | заглушка на данных (поиск и обогащение — stub); номер закупки → запрос из извещения работает (`tests/unit/test_api.py::test_lot_*`, `make e2e`: «номер закупки → выдача», на фикстуре, не на реальном лоте); карточка: статистика, факты с URL и датой, «ИНН не подтверждён» |
| 7 | Тест на реальных кейсах | data | 5–10 закупок СПб: прогон, метрики, разбор; кейсы — `eval/cases/real.jsonl` (8 лотов, оба канала), `eval/cases/dev.jsonl` (2 000 из 51 599), `research/EVAL.md` D | `uv run python -m src.eval.build_cases` (счётчики — `eval/cases/build_stats.json`); `uv run pytest tests/unit/test_build_cases.py` (утечка, отсев, релевантность); `uv run python -m src.eval.run --exp <id> --part real`; скриншоты в `pitch/` | частично: кейсы собраны (D-032), прогона с разбором и скриншотов нет (T-08, T-12) |
| 8 | Масштабирование | app | `pitch/architecture.md`, `docker-compose.yml` (Postgres + pgvector, D-031…D-035), `src/db/` (загрузчик, эмбеддинги), `src/match/pg_retriever.py` | `make up` → `make db-load` (счётчики = `manifest.json`, 16 с) → векторы (`make db-embed` или импорт с GPU #48) → `/api/health`; `EXPLAIN` kNN → Index Scan HNSW; замер latency командой | частично: данные в Postgres и векторный ретривер `pg_hybrid` есть (#39); поиск по умолчанию — BM25 по parquet до eval и подтверждения №3; read-path и пагинация в Postgres — #49; latency не замерена |
| 9 | Демонстрация | app | `make demo`, запасное видео | `make demo` с чистого клона на машине показа | заглушка |
| 10 | Презентация | app + все | `pitch/outline.md`, `pitch/QA.md`, `pitch/facts.md` | `jury` режим 1 и 2 | нет |
