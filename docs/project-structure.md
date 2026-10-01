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
scripts/              # issues.sh, download-models.sh, check_stubs.py, openapi.py
.claude/              # settings.json, hooks/, agents/, skills/
.githooks/            # pre-commit, pre-push
.github/              # ISSUE_TEMPLATE/task.md, pull_request_template.md
```

## Правила импортов

- Модули ролей стыкуются только через `src/contracts.py` и публичные функции `search()`,
  `enrich()`, `apply_filters()`; внутренние файлы соседа не импортируются.
- `src/api/` — единственный, кто знает про HTTP; `src/ui/` ходит только в `/api/*`.
- `src/eval/` вызывает `search()` так же, как API: метрика измеряет то, что видит пользователь.
