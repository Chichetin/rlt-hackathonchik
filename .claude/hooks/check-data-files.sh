#!/usr/bin/env bash
# Страж коммита: в git не попадают данные организаторов (data/), кеш источников (cache/),
# индексы (indexes/), базы и parquet, а также любые файлы > MAX_FILE_MB (5). Проверяет индекс.
# Режимы:
#   pre-tool — PreToolUse на Bash: срабатывает только на `git commit`, отказывает JSON-ом;
#   (без аргумента) — .githooks/pre-commit и ручной запуск: код 1 при нарушении.
set -uo pipefail

mode="${1:-git}"
if [ "$mode" = pre-tool ]; then
  cmd="$(jq -r '.tool_input.command // ""' 2>/dev/null)"
  printf '%s' "$cmd" | grep -qE '(^|[;&|[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit' || exit 0
  dir="$(printf '%s' "$cmd" | sed -nE 's/.*git[[:space:]]+-C[[:space:]]+([^[:space:]]+).*/\1/p' | tr -d "\"'")"
  [ -z "$dir" ] && dir="$(printf '%s' "$cmd" | sed -nE 's/^[[:space:]]*cd[[:space:]]+([^[:space:];&]+).*/\1/p' | tr -d "\"'")"
  [ -n "$dir" ] && { cd "$dir" 2>/dev/null || exit 0; }
fi
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
cd "$(git rev-parse --show-toplevel)" || exit 0  # пути из git diff — от корня

MAX_MB="${MAX_FILE_MB:-5}"
DATA_RE='^(data|cache|indexes)/|\.(parquet|duckdb|sqlite3?|db|feather|arrow|pkl|npy|npz|safetensors|pt|bin|onnx)$'

FOUND=""
while IFS= read -r -d '' f; do
  [ -f "$f" ] || continue
  [ "$f" = data/README.md ] && continue
  if printf '%s' "$f" | grep -qE "$DATA_RE"; then
    FOUND+="$f: данные/кеш/индекс — в git не кладём (data/README.md)"$'\n'
    continue
  fi
  size=$(( $(stat -c %s "$f") / 1024 / 1024 ))
  [ "$size" -ge "$MAX_MB" ] && FOUND+="$f: ${size} МБ ≥ ${MAX_MB} МБ"$'\n'
done < <(git diff --cached --name-only -z --diff-filter=AM)

[ -z "$FOUND" ] && exit 0

MSG="Коммит остановлен стражем данных:"$'\n'"$FOUND""Убрать из индекса: git restore --staged <путь>."
if [ "$mode" = pre-tool ]; then
  jq -n --arg r "$MSG" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $r}}'
  exit 0
fi
printf '%s\n' "$MSG" >&2
exit 1
