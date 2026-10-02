#!/usr/bin/env bash
# make static-demo: the static demo stand without the API (#283) — dist-static/ opens in a browser offline.
#   1. scripts/static_demo.py records the API answers on the demo path (cache only, no network);
#   2. the UI is built with VITE_STATIC_DEMO=1: src/ui/src/staticApi.ts answers /api/* from static-demo.json;
#   3. scripts/serve_static.py serves it locally (any path → index.html, as the API does).
# Publishing dist-static/ anywhere is an external action: human confirmation №4 (CLAUDE.md). It holds organizers'
# data (INNs, statistics) — dist-static/ and cache/ are not in git.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
OUT="$ROOT/dist-static"
DATA="$ROOT/cache/static-demo/static-demo.json"
uv run --frozen python scripts/static_demo.py --out "$DATA" "$@"
(cd src/ui && VITE_STATIC_DEMO=1 npx vite build --outDir "$OUT" --emptyOutDir)
cp "$DATA" "$OUT/static-demo.json"
echo "статический стенд: $OUT (данные: $DATA)"
echo "открыть: uv run python scripts/serve_static.py $OUT  →  http://127.0.0.1:8080"
