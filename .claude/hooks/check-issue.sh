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
    # Windows often has only the `py` launcher; without an interpreter every "git" command is blocked.
    if command -v python3 >/dev/null 2>&1; then PY=(python3)
    elif command -v py >/dev/null 2>&1; then PY=(py -3)
    else PY=(python); fi
    # UTF-8 out (py -3 on Windows writes cp1251 to a pipe and mangles a Cyrillic repo path), no \r.
    mapfile -t targets < <(printf '%s' "$cmd" | PYTHONUTF8=1 "${PY[@]}" -X utf8 "$HOOKS/git_targets.py" "$cwd" 2>/dev/null | tr -d '\r' || echo "$cwd")
    # A git target that does not exist is a parsing failure, not "outside the repo": fail closed on cwd.
    # (Empty output = no commit/push in the command: nothing to check. pipefail sends a python
    # failure to the `|| echo "$cwd"` branch.)
    for i in "${!targets[@]}"; do
      [ -z "${targets[$i]}" ] || [ -e "${targets[$i]}" ] || targets[$i]="$cwd"
    done
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
