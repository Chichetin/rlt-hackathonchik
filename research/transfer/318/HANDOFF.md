# #318 — завершение работы на ПК, 02.10.2026

Ветка `worktree-318-cust-ranker`, PR: https://github.com/Chichetin/rlt-hackathonchik/pull/337.
Пользователь попросил сохранить разбор и закончить из-за лимитов. Новые расчёты
не запускать автоматически. **В main не вливать; E6 в продукт не включена.**

## Результат и куда смотреть

Все запланированные dev500/dev2000/fresh2000, shuffle, top3 ablation и RRF60
завершены. Итог **INVESTIGATE**: основные dev-гейты пройдены, общий эффект fresh
при 95% не подтверждён. Dev winner@10 +1.11 п.п.; fresh +0.61 п.п.,
95% CI [−0.25, +1.52] п.п. Выигрыш сосредоточен на знакомых победителях.
Вторичные просадки новых победителей на dev явно сохранены в отчёте.

- [RESULTS.md](RESULTS.md): результаты, слабые места, приоритеты и неудачные направления.
- `screen-500-comparisons.json`, `dev-2000-comparisons.json`,
  `fresh-2000-comparisons.json`: парные интервалы, срезы, хеши, проверки кандидатов.
- `eval/results/E6-*.json`: исходные результаты. `E6-stale-base-500` — только
  диагностика старой схемы snapshot, не валидная база.
- `models/ranker/E6-{cust,shuffled,ablate}-gbm.{txt,meta.json}`: модели и provenance.
- `research/EXPERIMENTS.md`: исходная гипотеза и итог без продвижения модели.

## Воспроизводимость и оставшиеся проверки

Все опубликованные метрики измерены на чистом **`5a8e662`**, до последующей
интеграции main. Повторный eval/parity после интеграции не выполнялся по просьбе
завершить работу. Перед внедрением повторить базу и основную E6 на интегрированном
коде под новыми ID; сравнить ranked и candidate traces, отдельно проверить fresh.
Продуктовый конфиг сохранён upstream; E6 фиксирует `spell: false`.

До интеграции `make check-changed`: 1076 Python passed, 31 skipped; 67 UI и 23 e2e
passed. После интеграции main90050c0 полный `make check-changed`: 1158 Python passed,
31 skipped; 70 UI и 23 e2e passed. Лог — `check-changed-integrated.log`.

Частные snapshots Apr1/July1: 31 столбец, 34 869/38 428 поставщиков;
последние участия 2025-03-31/2025-06-30. `dev_full` содержит 51 599 кейсов только
окна dev; holdout не читался. База совпала с D101-default: 0/500 и 0/2000 различий.
Кеш `data/ranker/e6-cust-3000.npz` (49 MB) и данные остаются вне git.
Обучение: 3000 запросов, 548 594×43, основная модель 123 дерева.
Для воспроизведения старого кеша нужен исходный commit `5a8e662`: после интеграции
fingerprint кода меняется и старый кеш корректно отклоняется. Не переписывать
metadata для обхода этой проверки. Сырые parquet, snapshots, caches и indexes
не переносить в git; symlinks при переносе данных копировать с содержимым целей.

Ниже исторический handoff с ноутбука; его незавершённые пункты и запрет вычислений
относятся к прошлому состоянию и не заменяют итог выше.

---

# Передача #318 на ПК — рабочий снимок

Пользователь остановил ВСЕ локальные вычисления и попросил немедленно сохранить ветку.
На ноутбуке ничего больше не запускать. Эксперимент не завершён, в main не вливать.

## Контекст
- Issue: https://github.com/Chichetin/rlt-hackathonchik/issues/318
- Ветка worktree-318-cust-ranker объединяет #274 и #246. #274 уже влит upstream во время работы; актуальный main ещё не интегрирован.
- Цель E6: текстовые кандидаты + до30 поставщиков заказчика в группе ОКПД2, переобучить LightGBM; dev500→2000, shuffle labels, top3 ablation, без holdout.
- Пользователь хочет проверить также алгоритм без ML; scripts/e6_fusion.py — фиксированный offline RRF60, ещё не измерен.
- configs/search.yaml возвращён к исходному main до начала работы: эксперимент не включён в продукт.

## Главная найденная проблема
Локальный suppliers_2025-07-01.parquet был старой схемы:13 столбцов, профильные признаки отсутствовали. Модель получала NaN.
E6-stale-base-500.json — ДИАГНОСТИКА, не валидная база для D101: recall .4930895, winner@10 .5112016, ceiling .7617108. На тех же500 в D101 recall .5631864, winner@10 .5763747.
В частном worktree пересобраны snapshots Apr1 и July1 текущим build_suppliers (31 столбец). На двух расходившихся query после исправления порядок ТОЧНО совпал с D101. Полные500 после исправления ещё не запускались.
Добавлен src/eval/input_audit.py: reject missing profile columns и hashes input snapshots/model. Guard43 targetedtests прошли; lint/type после guard не запускались.

## Что сохранено
Код, unit tests, configs E6, гипотеза research/EXPERIMENTS.md, диагностический JSON, логи и smoke-модель рядом с этим файлом.
Smoke:50 train queries,9173rows, features30s, fit0.3s. Это техническая проба, не оценка качества.
Полный collect ОСТАНОВЛЕН по просьбе пользователя после строки1800/3000,326306rows,428s. Он сохраняет npz только в конце: частичная матрица из RAM НЕ сохранена, полного E6 model/cache НЕТ.

## Данные для ПК (вне git)
Нужно перенести data/processed (notices/items/participants/manifest и eval/dev_full.jsonl), частные suppliers_2025-04-01.parquet и suppliers_2025-07-01.parquet из этого worktree. Symlinks переносить с содержимым целевых файлов.
Путь worktree на ноутбуке: /home/m1x/Documents/hackathons/rlt-hackathonchik/.claude/worktrees/318-cust-ranker
Корневые data/processed — старые snapshots! Использовать scripts/e6_prepare.py для пересборки частного каталога. Не читать holdout.
Опционально перенести indexes/ этого worktree и data/ranker/e6-smoke.npz. Smokecache устарел по fingerprint после усиления provenance, для3000 не пригоден.
Секреты/.env в ветку не добавлены.

## Продолжение на ПК
1. Прочитать CLAUDE.md, research/EVAL.md, эту записку и issue318; проверить git status/upstream. Установить uv sync --frozen --extra pg, npm ci в src/ui.
2. Проверить датасет и snapshots. При необходимости: uv run python scripts/e6_prepare.py --source <исходный processed> --out data/processed
3. Независимое ревью нашло два TODO в ranker_train: collect должен scoped STRICT_RETRIEVER=True как eval; input_fingerprint должен включать src/ingest/load.py. Исправить ДО полного сбора признаков. Закрепить DATA_STORE=parquet.
4. make check-changed после правок. Последние полные gates/e2e НЕ запускались по просьбе пользователя остановить нагрузку.
5. DATA_STORE=parquet make eval EXP=E6-base-500 EVAL_ARGS='--config-of E6-base --sample 500 --candidate-audit'; проверить паритет с D101 на тех же query_id.
6. DATA_STORE=parquet uv run python -m src.match.ranker_train --experiment E6-cust-ranker --n 3000 --lgbm-only --model-prefix E6-cust --cache data/ranker/e6-cust-3000.npz.
7. E6-cust-untrained и E6-cust-ranker через makeeval sample500; потом dev2000 и compare --alpha .1, E-003 и D101. Варианты/команды в configs/experiments.yaml и --help.
8. Shuffled: --from-cache --shuffle-labels --model-prefix E6-shuffled; ablation: --from-cache --drop-features <top3 gain> --model-prefix E6-ablate. Все остальные trainаргументы и cache идентичны.
9. RRF replay: scripts/e6_fusion.py --text-result <baseJSON> --pool-result <untrainedJSON> --cutoff 2025-07-01 --exp E6-fusion-500. Это экспериментальное воспроизведение, не продукт.
10. Fresh2000 seed318 после прохождения dev, holdout закрыт; включение дефолта — человеческое подтверждение№3.

## Suggested skills
- experiment: .claude/skills/experiment/SKILL.md
- diagnose: проверка паритета и schema drift; новый eval guard уже имеет regressiontest.
- agent-reach: GitHub через gh.
- handoff: сохранить следующее состояние при передаче.

GPU пока не подключался: основной расход — чтение parquet и сбор признаков CPU. Два процесса работали параллельно, LightGBM fit4threads. Не обещать ускорение от GPU без замера.
