# data/ — вне git

- `raw/` — данные организаторов как выдали (01.10). В git не попадают никогда: `.gitignore`
  и страж `.claude/hooks/check-data-files.sh` (pre-commit).
- `processed/` — результат `src/ingest/` (parquet), склейка контрагентов по ИНН.
- `processed/eval/holdout.jsonl` — запечатанный отложенный период, читает только `make eval-final`.

## Откуда взять: `make data`

Датасет хранится ассетами релиза **`data-2026-10-01`** этого (приватного) репозитория — в git он не попадает.

```bash
make data                  # data/processed (parquet, 216 МБ) + data/raw (CSV, 680 МБ)
make data PART=processed   # только parquet — хватает для make demo и make eval
make data PART=raw         # только CSV организаторов — чтобы пересобрать ingest
```

`scripts/data.sh`: нужен `gh` с доступом к репозиторию; каждый файл сверяется с `SHA256SUMS` релиза, совпавший
не скачивается повторно, скачанное попадает на место только после сверки. Другой релиз — `DATA_TAG=<тег> make data`;
другие каталоги — `DATA_PROCESSED_DIR`, `DATA_RAW_DIR`. В релизе нет:
- `processed/eval/holdout.jsonl` и `processed/eval/dev_full.jsonl` — собираются `uv run python -m src.eval.build_cases`
  из parquet; **команда заодно переписывает отслеживаемые `eval/cases/dev.jsonl`, `real.jsonl`, `build_stats.json`** —
  после неё проверьте `git status` и не коммитьте их изменения без задачи;
- `processed/okpd2.parquet` — справочник названий ОКПД2, `make okpd2` (без него `make db-load` не загрузит таблицу названий);
- `indexes/` — строится при первом поиске или `uv run python -m src.match.index`.

**Репозиторий приватный, и релиз тоже.** Перед публикацией репозитория или созданием зеркала релиз `data-2026-10-01`
нужно удалить — иначе данные организаторов станут публичными (D-087).

Описание полей — `research/DATA_DICTIONARY.md`. Владелец — DATA.
