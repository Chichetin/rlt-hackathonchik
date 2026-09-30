# Карта переноса: раздел документа → куда

Исходник без правок — `docs/source/hackathon-research-workflow.md`. Документ не подключается
целиком через `@` (≈ 550 строк в каждой сессии и каждом субагенте — агенты начинают пропускать
правила), поэтому разложен по файлам. Ниже — где теперь живёт каждый раздел и что в нём изменено
намеренно (промпт 1, `docs/prompts/prompt-1-karkas.md`).

| § | Раздел | Куда перенесён | Что изменено намеренно |
|---|---|---|---|
| — | «Для людей» (шапка) | `docs/onboarding.md` | фраза старта сессии — вместо `status/*.md` читаем Issues |
| 0 | Кейс, что считается победой, таблица 10 пунктов | `CLAUDE.md` («Что считается победой»), `docs/tz-compliance.md` | колонка «Владелец» → `area:` |
| 1 | Факты, вопросы на постановке | `CLAUDE.md` («Факты»), `docs/questions-for-organizers.md`, `research/PROBLEM.md` (open questions) | неизвестное — `[ОБНОВИТЬ 01.10]` |
| 2 | Роли и владение | `docs/project-structure.md`, метки `area:` (`scripts/issues.sh labels`) | роли = метки; любой может взять свободную задачу чужой области (D-001) |
| 2 | Контракты | `src/contracts.py` (pydantic), `docs/openapi.json`, `src/ui/src/api-types.ts` | Evidence: `url` обязателен, `field`/`value` (D-005); `SearchResponse` — два блока |
| 2 | Общая память (`status/*.md`) | GitHub Issues + `issues.sh note` | `status/*.md` не заводим (D-001) |
| 2 | Git и секреты | `GIT.md`, `.claude/settings.json`, `check-secrets.sh` | не trunk-based: worktree + PR (D-002) |
| 2 | Лимиты | `CLAUDE.md` («Автономия») | ограничение на массовые LLM-вызовы снято; подвыборка и «одна модель не ранжирует и не судит» остались |
| 3 | Таймлайн с критериями выхода | `docs/timeline.md`; ближайшая веха — `session-context.sh` | сроки только здесь, перенос — одним решением (D-014) |
| 4 | Предметная область | `research/PROBLEM.md` | — |
| 5 | Методология | `research/METHODOLOGY.md` v0, `configs/search.yaml` | изменение формулы — подтверждение человека №3 |
| 6 | Открытые источники и обогащение | `research/SOURCES.md`, правила — `CLAUDE.md`, код — `src/sources/cache.py`, `src/enrich/evidence.py` | правило «LLM не порождает факты» проверяется кодом |
| 7 | Оценка качества | `research/EVAL.md`, `src/eval/`, `eval/`, `configs/split.yaml` | holdout запечатан кодом: `make eval-final` (D-010) |
| 8 | Дисциплина экспериментов | скилл `experiment`, `research/EXPERIMENTS.md`, `research/ERROR_ANALYSIS.md` | — |
| 9 | Research-спринты | скилл `experiment` (раздел «research-спринт»), `research/SOURCES.md`, `research/deep/` | — |
| 10 | Интерфейс | `docs/ui.md`, `src/ui/`, `src/ui/e2e/demo.spec.ts` | сценарий демо = e2e с первого коммита |
| 11 | Масштабирование | скилл `pitch`, `pitch/architecture.md`, `docker-compose.yml` | — |
| 12 | Демо и питч | скилл `pitch`, `pitch/outline.md`, `pitch/QA.md`, `pitch/facts.md`, `pitch/demo-insurance.md` | числа — только из `pitch/facts.md` со ссылкой на `eval/results/` |
| 13 | Guardrails | `CLAUDE.md` («Guardrails», «Проверки кодом») | «застрял > 20 мин» → скилл `diagnose`, потом человек; `requirements.txt` → `uv.lock` |
| 14 | Формат общения | `CLAUDE.md` («Формат общения») | блоки не блокирующие: агент печатает и продолжает (D-011) |
| 15 | Первые действия ролей | скилл `role-start` (`/role-start DATA` …), `docs/plans/draft-issues.md` | шаги — через взятые issues |
| 16 | Подготовка | `docs/onboarding.md` (чек-лист на 30.09 и старт сессии), `scripts/download-models.sh` | — |
| 17 | Главный критерий | `CLAUDE.md` («Главный критерий») | — |

Промпты команды: `docs/prompts/prompt-1-karkas.md` (этот каркас), `prompt-2-kickoff.md`
(01.10 после постановки), `prompt-3-research.md` (внешнее исследование → `research/deep/`).
