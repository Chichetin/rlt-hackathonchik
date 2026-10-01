---
name: submission-checklist
description: Проверка комплекта сдачи по 10 пунктам условий — make demo с чистого клона, e2e, make check, STUB в строгом режиме, секреты и данные, eval-final, facts против eval/results, запасное видео, тег сдачи. Запускать перед чек-поинтами, на feature freeze, на code freeze и перед тегом сдачи.
---

# Чек-лист сдачи

Источник — разделы 0, 11, 12 `docs/source/hackathon-research-workflow.md`; пункты — таблица
«10 пунктов условий» в `CLAUDE.md`. Формат, адрес и срок сдачи — `[СПРОСИТЬ Q-12]` (из
постановки; `docs/timeline.md`), хостинг и зеркало — там же. Проверка идёт на
`origin/main`; каждый пункт — с командой и её выводом. Найденное — новые issues
(`blocker`, если риск сдачи), а не правки по ходу проверки.

## Команды

```bash
git fetch --prune && sha=$(git rev-parse origin/main)
rm -rf /tmp/rlt-clean && git clone -q "$(git remote get-url origin)" /tmp/rlt-clean
cd /tmp/rlt-clean && git checkout -q "$sha"
make setup && make check                          # всё зелёное
make e2e                                          # сценарий демо зелёный
STUB_STRICT=1 make stubs                          # после code freeze: # STUB в демо-пути нет
make demo PORT=8020 &                             # поднимается с чистого клона; открыть http://127.0.0.1:8020
bash .claude/hooks/check-secrets.sh               # пусто
git ls-files | grep -E '^(data|cache|indexes)/' | grep -v data/README.md   # пусто
tail -3 eval/final-log.md                          # eval-final выполнен для замороженной конфигурации
```

`make demo` на **машине показа** — отдельно, человеком (физическое действие).

## Таблица

| # | Пункт условий | Что проверить | Доказательство |
|---|---|---|---|
| 1 | Анализ данных | профиль и 2–3 инсайта с цифрами, команды рядом | `research/PROBLEM.md` |
| 2 | Бизнес-смысл полей | словарь с уверенностью и способом проверки | `research/DATA_DICTIONARY.md` |
| 3 | Методология | формула и компоненты совпадают с кодом и `configs/search.yaml` | `research/METHODOLOGY.md` |
| 4 | Поиск в открытых источниках | новая компания в блоке «Новые» на живом запросе | e2e / скриншот |
| 5 | Обогащение | у каждого факта карточки источник и дата; evidence-тест зелёный | `uv run --frozen pytest tests/unit/test_evidence.py` |
| 6 | Веб-интерфейс | сценарий `docs/ui.md` ≤ 90 с | отчёт `operator` |
| 7 | Реальные кейсы | 5–10 закупок СПб, метрики с интервалами, разбор | `eval/results/*.json`, `research/EVAL.md` |
| 8 | Масштабирование | схема + `make up` работает + цифры latency/стоимости | `pitch/architecture.md` |
| 9 | Демонстрация | `make demo` с чистого клона; запасное видео записано | команда выше; файл видео |
| 10 | Презентация | основная и Топ-5, `pitch/QA.md`, числа = `pitch/facts.md` = `eval/results` | `pitch/` |

Сверка чисел: каждое число в `pitch/facts.md` и слайдах найдено в `eval/results/*.json`
(`grep` по значению), `"stub": false`.

## Отчёт

```
origin/main: <sha>; проверено <время МСК>
| # | Пункт | Статус (готово / частично / нет) | Доказательство (команда → вывод / файл) |
Блокеры сдачи: issue #… | нет
Тег: не ставился | submit-final на <sha> (GIT.md, «Сдача»)
```

Тег сдачи ставится только после полностью зелёного отчёта — по `GIT.md`.
