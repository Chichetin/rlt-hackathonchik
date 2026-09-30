#!/usr/bin/env bash
# Чужие коммиты в origin/main против текущей работы: пока агент работает, соседи
# пушат в main. Хук сам делает fetch и, если новые коммиты трогают мои файлы или
# общие правила/контракт, просит агента оценить, не противоречат ли они его задаче.
# Режимы:
#   stop   — Stop-хук: блокирует ход один раз на каждое новое состояние origin/main;
#   post   — PostToolUse: то же не чаще раза в 5 минут, без блокировки (в контекст);
#   manual — ручной запуск (Codex, человек): отчёт текстом.
# Без сети или без origin/main молчит.
set -uo pipefail

mode="${1:-manual}"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
gitdir="$(git rev-parse --git-dir)"   # у каждого worktree свой
seen_file="$gitdir/upstream-seen"

if [ "$mode" = post ]; then
  stamp="$gitdir/upstream-checked"
  [ -n "$(find "$stamp" -mmin -5 2>/dev/null)" ] && exit 0
  touch "$stamp"
fi

GIT_TERMINAL_PROMPT=0 timeout 10 git fetch -q origin main 2>/dev/null || exit 0
upstream="$(git rev-parse -q --verify origin/main)" || exit 0
base="$(git merge-base HEAD "$upstream" 2>/dev/null)" || exit 0

# С какого коммита main считать новым: последний показанный, но не раньше merge-base.
seen="$(cat "$seen_file" 2>/dev/null)"
if [ -z "$seen" ] || ! git merge-base --is-ancestor "$seen" "$upstream" 2>/dev/null ||
   git merge-base --is-ancestor "$seen" "$base"; then
  seen="$base"
fi
[ "$seen" = "$upstream" ] && { [ "$mode" = manual ] && echo "Новых коммитов в origin/main нет."; exit 0; }

theirs="$(git diff --name-only "$seen" "$upstream" | sort -u)"
mine="$({ git diff --name-only "$base"; git ls-files -o --exclude-standard; } | sort -u)"
overlap="$(comm -12 <(printf '%s\n' "$theirs") <(printf '%s\n' "$mine") | grep -v '^$')"
shared="$(printf '%s\n' "$theirs" | grep -E '^(CLAUDE\.md|GIT\.md|src/contracts\.py|research/DECISIONS\.md|docs/timeline\.md|docs/tz-compliance\.md|configs/|\.claude/)')"

if [ -z "$overlap" ] && [ -z "$shared" ]; then
  [ "$mode" = manual ] && echo "Новые коммиты в origin/main есть, но с твоими файлами и общими правилами не пересекаются."
  exit 0
fi

report="$(
  echo "В origin/main появились чужие коммиты, которые касаются твоей работы или общих правил."
  echo "Прежде чем продолжать, оцени, не противоречат ли они твоей текущей задаче: та же задача"
  echo "у другого, изменённый контракт или решение, правка того же файла. Противоречат — остановись"
  echo "и скажи человеку. Не противоречат — отметь это одной фразой и продолжай."
  echo "Подробно: git log -p ${seen:0:7}..origin/main -- <файл>; влить: git merge origin/main"
  echo
  echo "Коммиты:"
  git log --format='  %h %an, %ar: %s' "$seen..$upstream" | head -20
  [ -n "$overlap" ] && { echo "Пересекаются с твоими файлами:"; printf '  %s\n' $overlap; }
  [ -n "$shared" ] && { echo "Изменены общие правила или доска:"; printf '  %s\n' $shared; }
  if printf '%s\n' "$shared" | grep -qx 'src/contracts.py'; then
    echo "Изменён контракт src/contracts.py — сверь своих потребителей:"
    git diff "$seen" "$upstream" -- src/contracts.py | grep -E '^[+-][^+-]' | head -20 | sed 's/^/  /'
  fi
)"

case "$mode" in
  stop)
    echo "$upstream" > "$seen_file"
    jq -n --arg r "$report" '{decision: "block", reason: $r}' ;;
  post)
    echo "$upstream" > "$seen_file"
    jq -n --arg r "$report" '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $r}}' ;;
  *)
    echo "$report" ;;
esac
