# Структура репозитория и владение

Источник: раздел 2 `docs/source/hackathon-research-workflow.md` («Роли и владение»), переписан
под Issues (D-001). Роль = метка `area:` + каталоги ниже. У области есть основной человек, но
**любой может взять свободную задачу чужой области**, если там никто не работает; занятость
видно в `bash scripts/issues.sh board`. Правка чужого каталога — только в рамках задачи этой
области.

`[СПРОСИТЬ]` основной человек каждой области — решение команды после kickoff; до него любой берёт свободную P0.

| area: | Роль | Основной человек | Владеет | Отвечает за |
|---|---|---|---|---|
| `area:data` | DATA | `[СПРОСИТЬ]` | `src/ingest/`, `src/eval/`, `eval/`, `configs/split.yaml`, `data/processed/`, `research/DATA_DICTIONARY.md`, `research/EVAL.md`, `research/ERROR_ANALYSIS.md` | данные организаторов, смысл полей, единая схема, реальные кейсы, backtest и метрики |
| `area:match` | MATCH | `[СПРОСИТЬ]` | `src/match/`, `indexes/`, `configs/search.yaml`, `research/METHODOLOGY.md`, `research/EXPERIMENTS.md` | понимание запроса, кандидаты из истории, скоринг, объяснения |
| `area:sources` | SOURCES | `[СПРОСИТЬ]` | `src/sources/`, `src/enrich/`, `src/llm/`, `cache/`, `research/SOURCES.md` | поиск компаний в открытых источниках, сопоставление с ИНН, обогащение, проверки надёжности |
| `area:app` | APP | `[СПРОСИТЬ]` | `src/api/`, `src/ui/`, `src/worker/`, `deploy/`, `docker-compose.yml`, `Dockerfile`, `pitch/`, `docs/ui.md` | API, интерфейс, docker-compose, сценарий демо и его e2e, чек-поинты, презентация |

APP одновременно ведёт интерфейс и питч; во второй день это самая нагруженная роль. После feature
freeze (02.10, 14:00) цифры и слайды по методологии готовят DATA и MATCH, APP собирает презентацию.

## Общие файлы — один владелец изменения

| Файл | Кто меняет | Как |
|---|---|---|
| `src/contracts.py` | DATA до заморозки 11:30; после — любой | отдельный PR, метка `contract`, перечень потребителей; затем `make openapi` в том же PR |
| `docs/openapi.json`, `src/ui/src/api-types.ts` | генерируются | только `make openapi`; руками не правятся |
| `pyproject.toml`, `uv.lock`, `src/ui/package*.json` | тот, кому нужна зависимость | в своём PR + строка в `research/DECISIONS.md` |
| `src/settings.py`, `.env.example` | любой, по задаче | единственное место, где читается окружение: новая переменная — поле `Settings` и строка в `.env.example`; файл из `scripts/settings_allowlist.txt` переводится на `get_settings()` и вычёркивается из списка (`make settings-check`, D-139) |
| `src/ui/e2e/demo.spec.ts` | APP; область, подключившая свою часть, добавляет шаг | PR, ломающий сценарий, не вливается |
| `research/DECISIONS.md`, `docs/tz-compliance.md` | все, в своём PR | append-only; при конфликте сохраняются обе строки |
| `CLAUDE.md`, `GIT.md`, `.claude/`, `docs/timeline.md` | любой, по задаче | ревью `reviewer` обязательно; сроки — одним решением (D-014) |

## Дерево

```text
CLAUDE.md  GIT.md  README.md  Makefile  pyproject.toml  uv.lock  Dockerfile  docker-compose.yml
src/
  contracts.py        # Query, Evidence, Counterparty, ScoreComponent, CounterpartyMatch, SearchResponse
  ingest/             # DATA: data/raw → data/processed (parquet), склейка по ИНН
  match/              # MATCH: search.py (# STUB), filters.py — жёсткие фильтры
  sources/            # SOURCES: cache.py — кеш внешних источников (SQLite, TTL); клиенты источников
  enrich/             # SOURCES: enrich.py (# STUB), evidence.py — механическая проверка фактов
  llm/                # SOURCES: provider.py — LLM из .env, режим без LLM обязателен
  eval/               # DATA: metrics.py, split.py (печать holdout), run.py (make eval), final.py (make eval-final)
  worker/             # APP: queue.py (таблица задач), worker.py (процесс обогащения)
  api/                # APP: app.py — FastAPI; отдаёт собранный UI
  ui/                 # APP: React + Vite + TS; e2e/demo.spec.ts — сценарий демо
configs/              # search.yaml (MATCH), split.yaml (DATA) — их sha256 замораживает eval-final
eval/                 # cases/dev.jsonl, results/<id>.json, final-log.md
tests/                # pytest: unit/, fixtures/ (evidence: честная и выдуманная карточка)
data/ cache/ indexes/ # вне git
research/             # PROBLEM, DATA_DICTIONARY, METHODOLOGY, SOURCES, DECISIONS, EXPERIMENTS, ERROR_ANALYSIS, EVAL, deep/
pitch/                # outline, facts, QA, architecture, demo-insurance
docs/                 # timeline, onboarding, agents, ui, tz-compliance, questions-for-organizers, kickoff-todo, plans/, source/
scripts/              # issues.sh, download-models.sh, check_stubs.py, check_layers.py (+ layers_allowlist.txt), openapi.py
.claude/              # settings.json, hooks/, agents/, skills/
.githooks/            # pre-commit, pre-push
.github/              # ISSUE_TEMPLATE/task.md, pull_request_template.md
```

## Правила импортов

Направление зависимостей проверяет код, а не память: `make layers` (`scripts/check_layers.py`) входит в
`make check` и `make check-changed`. Решение и обоснование — D-106.

**Модуль слоя Lk импортирует только слои ≤ k** — вниз или в свой слой, не вверх. Считаются все
импорты: верхнего уровня, ленивые (внутри функции), относительные и под `if TYPE_CHECKING:`.

| Слой | Что | Модули `src/` |
|---|---|---|
| L0 | типы и настройки | `contracts.py`, `settings.py` (#290); `regions` — когда появится (#292) |
| L1 | внешний мир и хранение | `sources/`, `db/`, `llm/`, `ingest/` (build, load, notice, okpd2, profile, split_config, testset), `worker/queue.py` |
| L2 | ядро match | `match/` — store, index, search, fallback, filters, signals, profiles, purchase_type, text, pg_retriever |
| L3 | обогащение | `enrich/` — enrich, discover, evidence, role, web_leads_warm (прогрев веб-лидов, #108) |
| L4 | сценарии поверх ядра | `match/pipeline.py`, `ingest/predict.py` |
| L5 | точки входа | `api/`, `worker/` (кроме `queue.py`) |
| L6 | инструменты | `eval/`, `match/ranker_train.py` (обучение ранжировщика, #198) — импортируют продукт; продукт их не импортирует |

- Слой модуля — по самому длинному префиксу из `LAYERS` в `scripts/check_layers.py`: новый модуль
  внутри пакета получает слой пакета, исключение — отдельная строка (так `match/pipeline.py` — L4,
  а не L2). Новый пакет без строки в `LAYERS` — ошибка: его размещают осознанно, и строка таблицы
  выше обновляется в том же PR. Правка `LAYERS` в `scripts/check_layers.py` — смена схемы слоёв: строка
  `D-…` в `research/DECISIONS.md` и вердикт `reviewer`.
- Нужно импортировать вверх — не добавляйте строку в список, а вынесите нужное вниз (общий тип — в
  `src/contracts.py`) или передайте зависимость аргументом.
- Модули ролей стыкуются только через `src/contracts.py` и публичные функции `search()`,
  `enrich()`, `apply_filters()`; внутренние файлы соседа не импортируются (слои этого не заменяют).
- Нарушения, которые уже были в `src/` (рёбра «модуль → модуль»; полный текущий список —
  `python scripts/check_layers.py --all`), записаны в `scripts/layers_allowlist.txt` с причиной и
  способом устранения (#294). Новое нарушение — ошибка. Строку без живого нарушения можно удалять
  (она тогда устарела — предупреждение); пока нарушение живо, удаление строки делает `make layers`
  красным. Ребро задано парой модулей: новый импорт по уже допущенной паре проходит молча, растёт
  только число допущенных нарушений в выводе `make layers`.
- `src/api/` — единственный, кто знает про HTTP; `src/ui/` ходит только в `/api/*`.
- `src/eval/` вызывает `search()` так же, как API: метрика измеряет то, что видит пользователь.
