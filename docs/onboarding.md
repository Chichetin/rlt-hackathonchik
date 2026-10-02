# Старт участника

Источник: раздел 16 `docs/source/hackathon-research-workflow.md` («Подготовка», блок «Для
людей») и `docs/onboarding-prompt.md` FloodValue. Два шага: 10 минут руками, потом один промпт в
свою сессию Claude Code. Дальше агент работает сам и приходит к тебе только за пятью
подтверждениями (`CLAUDE.md`, «Автономия») и доступами.

## Чек-лист на 30.09 (до площадки)

- [ ] Принять приглашение в https://github.com/Chichetin/rlt-hackathonchik (collaborator).
- [ ] Инструменты: `git`, `gh` (`gh auth login`, HTTPS), `jq` ≥ 1.7, `uv`, Python 3.12, Node ≥ 22,
      Docker (для `make up`, необязательно).
- [ ] `git clone https://github.com/Chichetin/rlt-hackathonchik.git && cd rlt-hackathonchik && make setup`
      (uv + npm по lockfile, Playwright chromium, git-хуки).
- [ ] Один раз на репозиторий (кто первый): `bash scripts/issues.sh labels` — метки `area:*`,
      `P0–P2`, `blocker`, `contract`, `ui`, `experiment`; без них `issues.sh create` с метками упадёт.
- [ ] `make check` и `make e2e` зелёные; `make demo` открывается на http://127.0.0.1:8000.
- [ ] `make setup-ml` и `make models` — эмбеддинг-модели в кеш HF до площадки (Wi-Fi — частая
      точка отказа). Хотя бы у одного человека в команде.
- [ ] Промпт 3 (`docs/prompts/prompt-3-research.md`) прогнан в ChatGPT Deep Research и Claude Research; ответы
      без правок — в `research/deep/` (через задачу и PR).
- [ ] Письмо организаторам (uni@roseltorg.ru) — вопросы из `docs/questions-for-organizers.md`.
- [ ] Ключи внешних API, если нужны (DaData, поисковый API, LLM), — в свой `.env` по
      `.env.example`; регистрация — подтверждение человека №4.
- [ ] Прочитать руководства АИС ГЗ (manuals.gz-spb.ru) по Электронному магазину и Порталу 44,
      публичную часть estore.gz-spb.ru.
- [ ] Решить: кто выступает на основной защите и на Топ-5; чья машина — демо-машина
      (`make demo` с чистого клона проверить на ней).
- [ ] Договориться о моделях Claude Code: главная сессия — по вкусу; роли — по frontmatter.

## 01.10: постановка и kickoff

1. 10:00–10:15 — один записывает постановку, остальные слушают. Материалы — в `docs/source/`,
   данные — в `data/raw/` (в git не попадают; после 01.10 — `make data`, релиз `data-2026-10-01`).
2. Один человек запускает промпт 2 (`docs/prompts/prompt-2-kickoff.md`) в основной копии: факты, профиль
   данных, пересмотр каркаса по `docs/kickoff-todo.md`, план задач → подтверждение человека №2.
3. Остальные трое тем временем открывают свои сессии промптом ниже и ждут плана.

## Промпт для своей сессии — скопировать

```text
Ты — сессия одного из четырёх равноправных участников команды на RLT.Hack (Росэлторг),
кейс «подбор поставщиков для АИС ГЗ и Электронного магазина СПб». Координатора среди людей
нет. Задачи — GitHub Issues, занятость — assignee, роли — метки area:, стыковка — контракты.

Моя основная область: area:<data|match|sources|app>.

Прочитай: CLAUDE.md, GIT.md, docs/agents.md, docs/timeline.md, docs/project-structure.md,
research/DECISIONS.md, src/contracts.py. Перескажи мне в 5 строках: ближайшая веха и сколько
до неё, как брать задачу, когда нужен я (пять подтверждений), что такое «holdout запечатан».

Затем выполни /role-start <DATA|MATCH|SOURCES|APP> и работай по /orchestrate:
1. bash scripts/issues.sh board. Возьми 1–3 свободные задачи ближайшей вехи, начиная со своей
   area:. Зависимости («Зависит от #…») должны быть в main, иначе — на заглушке с пометкой.
   Покажи выбор одной строкой на задачу и сразу, без ожидания ответа, take <N>.
2. Каждой задаче — свой worktree и builder в фоне (модель — по оценке сложности в задаче).
3. Каждый PR — через reviewer; merge по вердикту, по одному, после make check-changed.
4. Спорное решение — reviewer в режиме «решение», 2 отклонения — jury; запись в DECISIONS.
   Вопрос к организаторам — строка в docs/questions-for-organizers.md, работаешь по
   рекомендуемой трактовке.
5. Перед /compact, концом сессии и закрытием задачи — issues.sh note.
Меня зови только для пяти подтверждений из CLAUDE.md и доступов. Блоки ЗАДАЧА/СДЕЛАНО
печатай, но не жди ответа.
```

## Шпаргалка

| Что | Команда |
|---|---|
| Что происходит | `bash scripts/issues.sh board` |
| Свободные задачи моей области | `bash scripts/issues.sh free area:match` |
| Взять / по прямому указанию | `bash scripts/issues.sh take <N>` / `take <N> --force` |
| Память перед `/compact` | `bash scripts/issues.sh note <N> "сделано" "дальше" "блокер"` |
| Завести задачу | `bash scripts/issues.sh create "<заголовок>" "area:app,P0" <файл по шаблону>` |
| Проверить изменённое (по умолчанию) | `make check-changed` |
| Проверить всё (freeze, сдача, общие файлы) | `make check && make e2e` |
| Эксперимент | `make eval EXP=<id>` → строка в `research/EXPERIMENTS.md` |
| Демо | `make demo` → http://127.0.0.1:8000 |

## Красный CI

CI (`.github/workflows/ci.yml`, D-105) запускается на каждый PR и push в `main`: job **check** —
`make -k check`, job **e2e** — `make e2e` на фикстурах, job **check-pg** — весь pytest против
Postgres + pgvector (сервис `pgvector/pgvector:pg16`); там ни один тест не должен пропуститься из-за
`TEST_DATABASE_URL`. Данные организаторов в CI не нужны.

Postgres-тесты локально: `docker compose up -d db`, затем
`TEST_DATABASE_URL=postgresql://app:app@127.0.0.1:5432/app uv run pytest tests/unit/test_store.py`
(и `test_db_load`, `test_db_pool`, `test_db_embed`, `test_pg_retriever`); без переменной они пропускаются.

1. Открыть упавший job → шаг `make -k check`: `-k` прогоняет все цели, ищите строки
   `make: *** [Makefile:…: <цель>] Error` — это и есть упавшие цели (`lint`, `typecheck`, `test`, …).
2. Повторить у себя ту же цель: `make <цель>` (например, `make typecheck`). Зелёная локально,
   красная в CI — обычно незакоммиченный файл, зависимость не из lockfile или `--extra ml`
   (в CI его нет: тяжёлые импорты — с `# pyright: ignore[reportMissingImports]`).
3. e2e: внизу запуска — артефакт `playwright-traces` (каталог `test-results/` упавших тестов).
   Скачать, распаковать; в каталоге теста — `error-context.md` (снимок страницы) и трасса:
   `npx playwright show-trace <каталог теста>/trace.zip`. Локально — `make e2e`.
4. Красный не из-за вашего PR (тот же job красный на `main`) — задача с меткой `blocker`, а не
   повторный запуск «на удачу».
