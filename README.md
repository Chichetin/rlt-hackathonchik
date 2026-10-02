# Подбор поставщиков для АИС ГЗ и Электронного магазина СПб

RLT.Hack (Росэлторг), 1–2 октября 2026. Сервис подбирает поставщиков, производителей и
дистрибьюторов под потребность заказчика: два блока выдачи («С опытом госзакупок» и «Новые из
открытых источников»), объяснение каждой рекомендации по компонентам и источник каждого факта.

> **Статус (02.10):** поиск, обогащение и блок «Новые» работают на данных организаторов
> (604 452 лота 2024–2025) и открытых реестрах ФНС; в демо-пути заглушек нет (`make stubs`).
> Синтетическая фикстура с плашкой «демонстрационные данные» включается только без данных
> организаторов (`make demo` на чистом клоне, D-050). Что измерено и чем — `docs/tz-compliance.md`,
> цифры — `pitch/facts.md`; технический аудит кода и решений (самопроверка агентом) — `docs/REVIEW.md`.

## Запуск с чистого клона

Нужны: Python 3.12, [uv](https://docs.astral.sh/uv/), Node ≥ 22, `jq`; для `make up` — Docker.

```bash
git clone https://github.com/Chichetin/rlt-hackathonchik.git
cd rlt-hackathonchik
make demo            # ставит зависимости по lockfile, собирает UI, поднимает API + воркер
# → http://127.0.0.1:8000
```

`make demo` отвечает без интернета на закешированных ответах внешних источников, если рядом лежит каталог `cache/` — его нет в git и в релизе, на чистом клоне кеша нет. Без данных
организаторов в `data/raw/` поднимается на синтетической фикстуре с плашкой «демонстрационные данные»;
данные организаторов — `make data` (нужен `gh` с доступом к репозиторию), подробнее и про кеш — `docs/demo-clean-clone.md`.

| Команда | Что делает |
|---|---|
| `make setup` | окружение строго по `uv.lock` и `package-lock.json`, Playwright, git-хуки |
| `make check-changed` | проверки только затронутого относительно `origin/main`: тесты модулей, которые импортируют изменённое; e2e — если задет демо-путь |
| `make check` | всё: ruff, pyright, tsc, pytest, vitest, тесты хуков, сверка OpenAPI, `# STUB` |
| `make e2e` | сценарий демо в браузере (Playwright) |
| `make eval EXP=<id>` | оценка на dev-части → `eval/results/<id>.json` с bootstrap-интервалами |
| `make eval-final CONFIRM=<sha256>` | однократная оценка замороженной конфигурации на запечатанном периоде |
| `make up` / `make down` | docker-compose: `api`, `worker`, `db` (Postgres) |
| `make openapi` | `docs/openapi.json` и TS-типы UI из `src/contracts.py` |
| `make setup-ml`, `make models` | эмбеддинги (torch CPU) и скачивание моделей |
| `make data` | датасет организаторов из релиза `data-2026-10-01` этого репозитория: `data/processed` (parquet) и `data/raw` (CSV), со сверкой SHA256; `make data PART=processed` — только parquet |

## Как устроено

```text
UI (React) ──/api──▶ FastAPI ──▶ search() ──▶ фильтры ──▶ два блока выдачи
                        │                         ▲
                        └─▶ очередь (tasks) ─▶ worker ─▶ enrich(inn) ─▶ кеш источников ─▶ Evidence-проверка
```

- Контракты — `src/contracts.py`; спецификация API для встраивания в АИС ГЗ — `docs/openapi.json`.
- Методология — `research/METHODOLOGY.md`; оценка качества — `research/EVAL.md`.
- Источники и правила обогащения — `research/SOURCES.md`.
- Масштабирование — `pitch/architecture.md`.

## Для команды

Правила — `CLAUDE.md`, Git — `GIT.md`, старт участника — `docs/onboarding.md`, сроки —
`docs/timeline.md`, задачи — GitHub Issues (`bash scripts/issues.sh board`).
