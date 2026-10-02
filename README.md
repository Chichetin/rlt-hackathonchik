# Подбор поставщиков для АИС ГЗ и Электронного магазина СПб

RLT.Hack (Росэлторг), 1–2 октября 2026. Сервис подбирает поставщиков, производителей и
дистрибьюторов под потребность заказчика: два блока выдачи («С опытом госзакупок» и «Новые из
открытых источников»), объяснение каждой рекомендации по компонентам и источник каждого факта.

> **Статус (02.10):** поиск, обогащение и блок «Новые» работают на данных организаторов
> (604 452 лота 2024–2025) и открытых реестрах ФНС; в демо-пути заглушек нет (`make stubs`).
> Синтетическая фикстура с плашкой «демонстрационные данные» включается только без данных
> организаторов (`make demo` на чистом клоне, D-050). Что измерено и чем — `docs/tz-compliance.md`,
> цифры — `pitch/facts.md`; технический аудит кода и решений (самопроверка агентом) — `docs/REVIEW.md`.

## Для жюри: 3 команды

Нужны: Python 3.12, [uv](https://docs.astral.sh/uv/), Node ≥ 22, `jq`, Docker (только для `make up`), GitHub CLI `gh` с доступом к репозиторию
(`gh auth login`; репозиторий приватный — данные и снимок кеша лежат в его релизе `data-2026-10-02`).

```bash
git clone https://github.com/Chichetin/rlt-hackathonchik.git && cd rlt-hackathonchik
make data PART=processed && make data PART=cache && make data PART=gisp   # parquet (216 МБ), снимок кеша источников и реестр ГИСП (480 166 позиций), со сверкой SHA256
# быстрее для жюри: без `PART=processed` `make data` скачает ещё сырые CSV (+680 МБ), для демо они не нужны
make demo                           # зависимости по lockfile, UI, API + воркер → http://127.0.0.1:8000
```

С данными и снимком кеша на демо-лотах (**5903085**, 5875992, 6050858) видны названия компаний, роли с основанием,
источник и дата каждого факта и блок «Новые». Без `make data` `make demo` поднимается на синтетической фикстуре с плашкой
«демонстрационные данные» (проверка запуска, не для показа); без снимка кеша вместо названий будет «ИНН …», «Новые» пусты
(если нет сети) — подробности и что не работает без интернета: `docs/demo-clean-clone.md`. Снимок кеша — публичные ответы
реестров на 02.10.2026; записи устаревают 09.10.2026 (7 суток, `SOURCE_CACHE_TTL_S`), после этого `make demo` с сетью обновляет их сам.
Точное воспроизведение снимка: `ENRICH_OFFLINE=1 DISCOVERY_OFFLINE=1 make demo`.

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
| `make data` | датасет организаторов из релиза `data-2026-10-02` этого репозитория: `data/processed` (parquet) и `data/raw` (CSV), со сверкой SHA256; `make data PART=processed` — только parquet; `make data PART=cache` — снимок кеша источников в `cache/sources.sqlite` (влился в локальный кеш: его записи не затираются; `CACHE_FORCE=1` заменит целиком); `make data PART=gisp` — реестр ГИСП (D-153) в `data/external/gisp` (ассеты `gisp-products.parquet`, `gisp-meta.json`, суммы `SHA256SUMS.gisp`; без него «Новые» идут без ступени ГИСП); `make data PART=pool` — офлайн-пул «Новых» (#185, #219, #518) в `data/external/rsmp/pool` (все ассеты `pool-*` из `SHA256SUMS.pool`, ≈ 0,7 ГБ; без него «Новые» `/api/search` — без пула, с заметкой) |

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
