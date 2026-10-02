#!/usr/bin/env bash
# make check-changed — проверки только по затронутым модулям относительно origin/main (коммиты
# ветки + незакоммиченное + новые файлы; переименование — оба пути). Правило — CLAUDE.md,
# «Проверки кодом»; отбор — scripts/select_tests.py.
#
#   общие файлы (Makefile, зависимости, контракт, сам отбор) → полный make check (+ e2e)
#   *.py                       → ruff по этим файлам; pyright по ним и модулям, которые их импортируют
#   pytest                     → тесты, которые импортируют изменённый модуль src — напрямую, транзитивно
#                                или через фикстуры conftest; всё, чего граф не знает (configs/,
#                                eval/cases/, фикстуры, удалённый модуль, хелпер тестов) → весь pytest;
#                                документы, src/ui, хуки, *.sh → pytest не нужен
#   src/ui/…                   → tsc + vitest по связанным тестам (--changed)
#   демо-путь (граф src/api/app.py, src/ui, фикстуры, configs, Makefile) → make e2e
#   API / контракт / типы / генератор OpenAPI → openapi-check
#   хуки, issues.sh            → test-hooks
#   src/…, check_layers.py     → layers (импорты вниз по слоям, D-106), для src/ ещё seal-check, stubs (секунды)
# Отбор упал — проверяется всё, а не ничего. BASE=<ref> — сравнить с другой веткой.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2
export PYTHONUTF8=1 PYTHONPATH="$PWD"
PY=(uv run --frozen)
SELECT=("${PY[@]}" python scripts/select_tests.py)
base_ref="${BASE:-origin/main}"
base="$(git merge-base HEAD "$base_ref" 2>/dev/null)" || { echo "check-changed: нет $base_ref — make check"; exec make check; }

lines() { tr -d '\r' | grep -v '^$'; }  # tr -d '\r' — Python на Windows

# Changed paths, deleted included (the selection needs them).
out="$("${SELECT[@]}" changed "$base_ref")" || { echo "check-changed: список изменений не получен — make check"; exec make check; }
mapfile -t changed < <(printf '%s\n' "$out" | lines)
mapfile -t files < <(for f in "${changed[@]}"; do [ -e "$f" ] && echo "$f"; done)

if [ ${#changed[@]} -eq 0 ]; then echo "check-changed: изменений относительно $base_ref нет"; exit 0; fi
echo "check-changed: ${#changed[@]} файл(ов) относительно $base_ref"

has() { printf '%s\n' "${changed[@]}" | grep -qE "$1"; }
# exit 0 — нужен, 1 — не нужен, другое (отбор упал) — нужен
e2e_needed() { "${SELECT[@]}" e2e "${changed[@]}"; [ $? -ne 1 ]; }

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
[ ${#py[@]} -gt 0 ] && run "${PY[@]}" ruff check "${py[@]}"
if printf '%s\n' "${changed[@]}" | grep -qE '^(src|tests)/.*\.py$'; then
  if out="$("${SELECT[@]}" pyright "${changed[@]}")"; then
    mapfile -t typed < <(printf '%s\n' "$out" | lines)
    [ ${#typed[@]} -gt 0 ] && run "${PY[@]}" pyright "${typed[@]}"
  else
    echo "check-changed: отбор для pyright упал — pyright целиком"; run "${PY[@]}" pyright
  fi
fi

if out="$("${SELECT[@]}" pytest "${changed[@]}")"; then
  mapfile -t tests < <(printf '%s\n' "$out" | lines)
else
  echo "check-changed: отбор тестов упал — весь pytest"; tests=(ALL)
fi
if [ "${tests[0]:-}" = ALL ]; then
  run "${PY[@]}" pytest
elif [ ${#tests[@]} -gt 0 ]; then
  run "${PY[@]}" pytest "${tests[@]}"
fi

if has '^src/ui/'; then
  (cd src/ui && run npx tsc -b --noEmit && run npx vitest run --changed "$base" --passWithNoTests) || fail=1
fi
has '^(src/api/|docs/openapi\.json|src/ui/src/api-types\.ts|scripts/openapi(\.py|_check\.sh))' && run bash scripts/openapi_check.sh
has '^(\.claude/hooks/|\.claude/settings\.json|\.githooks/|scripts/issues\.sh)' && run bash .claude/hooks/test-hooks.sh
if has '^src/'; then
  run bash scripts/seal_check.sh
  run "${PY[@]}" python scripts/check_stubs.py
fi
has '^(src/|scripts/(check_layers\.py|layers_allowlist\.txt)$)' && run "${PY[@]}" python scripts/check_layers.py
e2e_needed && run make e2e

if [ $fail -ne 0 ]; then echo "check-changed: ЕСТЬ ОШИБКИ"; exit 1; fi
echo "check-changed: OK"
