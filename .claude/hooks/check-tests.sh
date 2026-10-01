#!/usr/bin/env bash
# Stop-хук: ход не считается завершённым, если тесты красные. pytest (есть pyproject.toml и
# tests/) и vitest (src/ui с node_modules). Нет тестов или окружения — молчит. Прогон
# пропускается, если ничего не менялось с последнего зелёного.
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || exit 0  # хук может стартовать из подкаталога

MARKER=".claude/.last-green-test-run"

if [ -f "$MARKER" ]; then
  CHANGED="$(find . -type f -newer "$MARKER" \
    -not -path './.git/*' -not -path '*/node_modules/*' -not -path './.claude/*' -not -path './cache/*' \
    -not -path '*/.venv/*' -not -path '*/__pycache__/*' -not -path '*/dist/*' -not -path '*/.pytest_cache/*' \
    -not -path '*/test-results/*' -not -path './eval/results/*' 2>/dev/null | head -1)"
  [ -z "$CHANGED" ] && exit 0
fi

RAN=0
OUTPUT=""
STATUS=0

if [ -f pyproject.toml ] && [ -d tests ] && find tests -name 'test_*.py' | grep -q .; then
  RAN=1
  if command -v uv >/dev/null 2>&1; then PYTEST=(uv run --frozen pytest -q -p no:cacheprovider)
  elif [ -x .venv/bin/pytest ]; then PYTEST=(.venv/bin/pytest -q)
  elif command -v python3 >/dev/null 2>&1; then PYTEST=(python3 -m pytest -q)
  else PYTEST=(py -3 -m pytest -q); fi
  OUT="$(PYTHONPATH=. "${PYTEST[@]}" 2>&1)" || STATUS=1
  OUTPUT+="== pytest"$'\n'"$OUT"$'\n'
fi

for dir in . src/ui; do
  [ -f "$dir/package.json" ] && [ -d "$dir/node_modules" ] || continue
  jq -e '.scripts.test and (.scripts.test | test("no test specified") | not)' "$dir/package.json" >/dev/null 2>&1 || continue
  RAN=1
  OUT="$(cd "$dir" && npm test --silent 2>&1)" || STATUS=1
  OUTPUT+="== npm test ($dir)"$'\n'"$OUT"$'\n'
done

[ "$RAN" -eq 0 ] && exit 0

if [ "$STATUS" -eq 0 ]; then
  mkdir -p .claude && touch "$MARKER"
  exit 0
fi

TAIL="$(printf '%s' "$OUTPUT" | tail -n 40)"
jq -n --arg out "$TAIL" '{decision: "block", reason: ("Тесты красные перед завершением хода:\n" + $out)}'
exit 0
