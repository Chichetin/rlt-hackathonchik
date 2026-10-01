#!/usr/bin/env bash
# make demo, часть после сборки UI: выбор данных → индексы → API + worker.
# Порядок: готовый data/processed → сборка из data/raw (CSV организаторов) → синтетическая фикстура
# (демонстрационный режим: DEMO_FIXTURE=1, API отдаёт stub=true, UI показывает плашку).
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8000}"
PY=(uv run --frozen)
has_processed() { [ -f "${DATA_PROCESSED_DIR:-data/processed}/manifest.json" ]; }
has_raw() { compgen -G "${DATA_RAW_DIR:-data/raw}/Извещения*.csv" >/dev/null; }

if has_processed; then
  echo "[demo] данные: ${DATA_PROCESSED_DIR:-data/processed}"
elif has_raw; then
  echo "[demo] собираю parquet из ${DATA_RAW_DIR:-data/raw} (src.ingest.build)"
  "${PY[@]}" python -m src.ingest.build
else
  echo "[demo] ВНИМАНИЕ: данных организаторов нет (data/raw пуст) — синтетическая фикстура, выдача помечена" >&2
  echo "[demo] как получить данные: docs/demo-clean-clone.md" >&2
  export DEMO_FIXTURE=1 DATA_PROCESSED_DIR=cache/demo-processed INDEX_DIR=cache/demo-indexes
  "${PY[@]}" python -m src.ingest.build --raw-dir tests/fixtures/ingest --out-dir cache/demo-processed
fi

"${PY[@]}" python -m src.match.index   # предсборка BM25 (продукт + cutoff)
echo "Демо: http://127.0.0.1:${PORT}  (Ctrl+C — остановить)"
trap 'kill 0' EXIT INT TERM
"${PY[@]}" python -m src.worker.worker &
"${PY[@]}" uvicorn src.api.app:app --host 127.0.0.1 --port "$PORT"
