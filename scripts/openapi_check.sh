#!/usr/bin/env bash
# make openapi-check: контракты, OpenAPI и TS-типы UI не разошлись. Скрипт, а не рецепт Makefile:
# виндовый make портит кириллицу в командной строке рецепта (кракозябры в выводе).
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2
export PYTHONUTF8=1 PYTHONPATH="$PWD"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
uv run --frozen python -c "import json; from src.api.app import app; print(json.dumps(app.openapi(), ensure_ascii=False, indent=2))" > "$tmp/openapi.json" || exit 1
# --strip-trailing-cr: на Windows (core.autocrlf) рабочая копия в CRLF, генератор пишет LF.
diff -q --strip-trailing-cr "$tmp/openapi.json" docs/openapi.json >/dev/null || { echo "openapi: docs/openapi.json устарел — make openapi"; exit 1; }
(cd src/ui && node node_modules/openapi-typescript/bin/cli.js "$tmp/openapi.json" -o "$tmp/api-types.ts" >/dev/null 2>&1) || { echo "openapi: openapi-typescript упал"; exit 1; }
diff -q --strip-trailing-cr "$tmp/api-types.ts" src/ui/src/api-types.ts >/dev/null || { echo "openapi: src/ui/src/api-types.ts устарел — make openapi"; exit 1; }
echo "openapi: контракты, OpenAPI и типы UI совпадают"
