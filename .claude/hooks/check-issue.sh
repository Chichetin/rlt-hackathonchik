#!/usr/bin/env bash
# PreToolUse: без взятой задачи не работаем (CLAUDE.md, «Задачи»). Правка файла (Edit, Write,
# MultiEdit, NotebookEdit) и git commit / git push через Bash разрешены только в worktree
# задачи: ветка worktree-<N>-<тема>, задача #N открыта и назначена на меня.
# Проверку делает `scripts/issues.sh check`; какие каталоги коммитит/пушит команда Bash,
# решает git_targets.py (git только в позиции команды, не в кавычках). Файлы вне репозитория
# (scratchpad, память, другие репозитории) и удаление ветки через push не проверяются.
# Отказ — код 2, причина уходит агенту в stderr.
set -uo pipefail

input="$(cat)"
command -v jq >/dev/null 2>&1 || exit 0
HOOKS="$(cd "$(dirname "$0")" && pwd)"
tool="$(printf '%s' "$input" | jq -r '.tool_name // ""')"
cwd="$(printf '%s' "$input" | jq -r '.cwd // "."')"

case "$tool" in
  Edit|Write|MultiEdit|NotebookEdit)
    mapfile -t targets < <(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty')
    ;;
  Bash)
    cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // ""')"
    case "$cmd" in *git*) ;; *) exit 0 ;; esac
    mapfile -t targets < <(printf '%s' "$cmd" | python3 "$HOOKS/git_targets.py" "$cwd" 2>/dev/null || echo "$cwd")
    ;;
  *) exit 0 ;;
esac

for target in "${targets[@]}"; do
  [ -n "$target" ] || continue
  # Скрипт — из того worktree, где идёт работа; в старой ветке без него — копия рядом с хуком.
  dir="$target"
  while [ ! -d "$dir" ] && [ "$dir" != / ] && [ "$dir" != . ]; do dir="$(dirname "$dir")"; done
  script="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)/scripts/issues.sh"
  [ -f "$script" ] || script="$(cd "$HOOKS/../.." && pwd)/scripts/issues.sh"
  [ -f "$script" ] || continue
  if ! reason="$(bash "$script" check "$target" 2>&1)"; then
    echo "Без взятой задачи работать нельзя (CLAUDE.md, «Задачи»). $reason" >&2
    exit 2
  fi
  [ -n "$reason" ] && echo "$reason" >&2
done
exit 0
