#!/usr/bin/env bash
# make check-changed — проверки только по затронутым модулям относительно origin/main (коммиты
# ветки + незакоммиченное + новые файлы). Правило — CLAUDE.md, «Проверки кодом».
#
#   общие файлы (Makefile, зависимости, контракт, сам отбор) → полный make check (+ e2e)
#   *.py                       → ruff и pyright по этим файлам
#   pytest                     → тесты, которые импортируют изменённый модуль src — напрямую, транзитивно
#                                или через фикстуры conftest (scripts/select_tests.py); configs/,
#                                tests/fixtures/, удалённый модуль → весь pytest
#   src/ui/…                   → tsc + vitest по связанным тестам (--changed)
#   демо-путь (граф src/api/app.py, src/ui, фикстура ingest) → make e2e
#   API / контракт / типы      → openapi-check
#   хуки, issues.sh            → test-hooks
#   src/…                      → seal-check, stubs (секунды)
#   только документы           → ничего
# BASE=<ref> — сравнить с другой веткой. Полный прогон всего — make check.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2
export PYTHONUTF8=1 PYTHONPATH="$PWD"
PY=(uv run --frozen)
base_ref="${BASE:-origin/main}"
base="$(git merge-base HEAD "$base_ref" 2>/dev/null)" || { echo "check-changed: нет $base_ref — make check"; exec make check; }

# Changed paths, deleted included (the selection needs them); tr -d '\r' — Python на Windows.
mapfile -t changed < <("${PY[@]}" python scripts/select_tests.py changed "$base_ref" | tr -d '\r')
mapfile -t files < <(for f in "${changed[@]}"; do [ -e "$f" ] && echo "$f"; done)

if [ ${#changed[@]} -eq 0 ]; then echo "check-changed: изменений относительно $base_ref нет"; exit 0; fi
echo "check-changed: ${#changed[@]} файл(ов) относительно $base_ref"

has() { printf '%s\n' "${changed[@]}" | grep -qE "$1"; }
e2e_needed() { "${PY[@]}" python scripts/select_tests.py e2e "${changed[@]}"; }

shared='^(Makefile|pyproject\.toml|uv\.lock|\.gitattributes|src/ui/package(-lock)?\.json|src/contracts\.py|scripts/(check_changed\.sh|select_tests\.py))$'
if has "$shared"; then
  printf '%s\n' "${changed[@]}" | grep -E "$shared" | sed 's/^/  общий файл: /'
  echo "check-changed: затронут общий файл → полный make check"
  make check || exit 1
  if e2e_needed; then make e2e || exit 1; fi
  exit 0
fi

fail=0
run() { echo "+ $*"; "$@" || { fail=1; return 1; }; }

mapfile -t py < <(printf '%s\n' "${files[@]}" | grep -E '^(src|tests|scripts|\.claude/hooks)/.*\.py$')
if [ ${#py[@]} -gt 0 ]; then
  run "${PY[@]}" ruff check "${py[@]}"
  mapfile -t typed < <(printf '%s\n' "${py[@]}" | grep -E '^(src|tests)/')
  [ ${#typed[@]} -gt 0 ] && run "${PY[@]}" pyright "${typed[@]}"
fi

mapfile -t tests < <("${PY[@]}" python scripts/select_tests.py pytest "${changed[@]}" | tr -d '\r')
if [ "${tests[0]:-}" = ALL ]; then
  run "${PY[@]}" pytest
elif [ ${#tests[@]} -gt 0 ]; then
  run "${PY[@]}" pytest "${tests[@]}"
fi

if has '^src/ui/'; then
  (cd src/ui && run npx tsc -b --noEmit && run npx vitest run --changed "$base" --passWithNoTests) || fail=1
fi
has '^(src/api/|docs/openapi\.json|src/ui/src/api-types\.ts)' && run bash scripts/openapi_check.sh
has '^(\.claude/hooks/|\.claude/settings\.json|\.githooks/|scripts/issues\.sh)' && run bash .claude/hooks/test-hooks.sh
if has '^src/'; then
  run bash scripts/seal_check.sh
  run "${PY[@]}" python scripts/check_stubs.py
fi
e2e_needed && run make e2e

if [ $fail -ne 0 ]; then echo "check-changed: ЕСТЬ ОШИБКИ"; exit 1; fi
echo "check-changed: OK"
