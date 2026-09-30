#!/usr/bin/env bash
# SessionStart: stdout попадает в контекст сессии. Ветка и worktree, предупреждение о работе в
# основной копии, мои задачи, занятые area:, кандидаты на перехват (> 45 мин без движения),
# открытые blocker (scripts/issues.sh board) и ближайшая веха из docs/timeline.md со временем
# до неё. Ничего не блокирует. Без gh или сети — одна строка предупреждения.
set -uo pipefail

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
root="$(git rev-parse --show-toplevel)"

branch="$(git branch --show-current 2>/dev/null)"
dirty="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
echo "[git] ветка: ${branch:-detached}; незакоммиченных путей: $dirty; $root"

# В связанном worktree git-dir и git-common-dir различаются.
if [ "$(git rev-parse --git-dir)" = "$(git rev-parse --git-common-dir)" ] && [ "$branch" = main ]; then
  echo "[git] ВНИМАНИЕ: ты в основной копии на main. Задачу, меняющую файлы, делай в своём worktree:"
  echo "      bash scripts/issues.sh take <N>, затем git worktree add .claude/worktrees/<N>-<тема> -b worktree-<N>-<тема> origin/main (GIT.md)"
  [ "$dirty" != 0 ] && echo "      Незакоммиченные изменения здесь могут быть чужими — не коммить и не трогай их."
fi

# Ближайшая веха: строки «- ГГГГ-ММ-ДД ЧЧ:ММ | Название» в docs/timeline.md, время московское.
tl="$root/docs/timeline.md"
if [ -f "$tl" ]; then
  now="${SESSION_NOW:-$(date +%s)}"
  next="$(sed -nE 's/^- ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}) \| (.*)$/\1|\2/p' "$tl" | while IFS='|' read -r ts title; do
      t="$(TZ=Europe/Moscow date -d "$ts" +%s 2>/dev/null)" || continue
      [ "$t" -ge "$now" ] && printf '%s|%s|%s\n' "$t" "$ts" "$title"
    done | sort -n | head -1)"
  if [ -n "$next" ]; then
    IFS='|' read -r t ts title <<<"$next"
    left=$(( (t - now) / 60 ))
    if [ "$left" -ge 1440 ]; then human="$((left / 1440)) д $(((left % 1440) / 60)) ч"
    elif [ "$left" -ge 60 ]; then human="$((left / 60)) ч $((left % 60)) мин"
    else human="$left мин"; fi
    echo "[веха] $ts МСК — $title (через $human). Критерий выхода — docs/timeline.md"
  else
    echo "[веха] все вехи docs/timeline.md прошли"
  fi
fi

[ -f "$root/scripts/issues.sh" ] || exit 0
if ! ISSUES_TIMEOUT=6 timeout 10 bash "$root/scripts/issues.sh" board 2>&1; then
  echo "[issues] GitHub недоступен — занятость не видна (bash scripts/issues.sh mine)."
fi
exit 0
