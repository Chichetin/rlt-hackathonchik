# Демо с чистого клона: откуда данные

`make demo` (проверено на `git clone` в пустой каталог, #13) выбирает данные так (`scripts/demo.sh`):

1. `data/processed/manifest.json` есть — используем готовые parquet.
2. Иначе в `data/raw/` лежат `Извещения*.csv`, `Поставщики*.csv`, `ТРУ*.csv` организаторов
   («Данные 24-25», вне git) — собираем parquet: `python -m src.ingest.build` (минуты на ≈ 680 МБ).
3. Иначе — **синтетическая фикстура** `tests/fixtures/ingest` (`DEMO_FIXTURE=1`): `/api/health`
   отдаёт `stub: true`, UI показывает плашку «Демонстрационные данные». Для проверки запуска, не для показа жюри.

Где взять: данные — у организаторов (копировать CSV в `data/raw/`; DATA_RAW_DIR меняет путь);
индексы BM25 строятся при старте (`python -m src.match.index`, `indexes/`); кеш внешних источников
`cache/` — SQLite, в git нет, на машине показа переносится копированием каталога `cache/`
(запросы и названия из кеша отвечают без сети). Основной путь с Postgres — `make up` (D-035).

Проверка: `make demo` → `curl localhost:8000/api/health` (`status: ok`) → поиск в UI.
Скриншоты: `BASE=http://127.0.0.1:8000 node scripts/screenshots.mjs [лот]` → `pitch/screenshots/`.
