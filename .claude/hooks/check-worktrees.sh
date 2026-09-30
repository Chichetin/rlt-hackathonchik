#!/usr/bin/env bash
# SubagentStop: субагенты (builder, scribe, qa) работают в worktree под
# .claude/worktrees/, а Stop-хук check-secrets.sh видит только текущую копию.
# Прогоняем его в каждом worktree репозитория и блокируем первым найденным.
# Без git молчит. Ручной запуск: bash .claude/hooks/check-worktrees.sh
set -uo pipefail

HOOKS="$(cd "$(dirname "$0")" && pwd)"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

while read -r wt; do
  [ -d "$wt" ] || continue
  out="$(cd "$wt" && bash "$HOOKS/check-secrets.sh" 2>/dev/null)"
  if [ -n "$out" ]; then
    printf '%s' "$out" | jq --arg wt "$wt" '.reason = ("worktree " + $wt + ":\n" + .reason)'
    exit 0
  fi
done < <(git worktree list --porcelain | awk '/^worktree /{print $2}')

exit 0
