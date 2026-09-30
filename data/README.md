# data/ — вне git

- `raw/` — данные организаторов как выдали (01.10). В git не попадают никогда: `.gitignore`
  и страж `.claude/hooks/check-data-files.sh` (pre-commit).
- `processed/` — результат `src/ingest/` (parquet), склейка контрагентов по ИНН.
- `processed/eval/holdout.jsonl` — запечатанный отложенный период, читает только `make eval-final`.

Описание полей — `research/DATA_DICTIONARY.md`. Владелец — DATA.
