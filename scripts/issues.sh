#!/usr/bin/env bash
# GitHub Issues — очередь задач команды (CLAUDE.md, «Задачи»). Клиент — gh; без gh, авторизации
# или remote команды печатают предупреждение и не падают (чтение — код 0, запись — код 3).
#
#   bash scripts/issues.sh board                 сводка для хука начала сессии
#   bash scripts/issues.sh list [метка]          открытые задачи (area:match, blocker, P0 …)
#   bash scripts/issues.sh free [метка]          открытые и никем не взятые
#   bash scripts/issues.sh mine                  мои открытые
#   bash scripts/issues.sh view <N>              задача целиком
#   bash scripts/issues.sh take <N> [--force]    взять. Занятая той же area: — предупреждение, не отказ.
#                                                Чужая задача без движения > 45 мин — перехват с комментарием.
#                                                --force — прямое указание человека «возьми #N»: перехват сразу.
#   bash scripts/issues.sh note <N> "<сделано>" "<дальше>" "<блокер|нет>"   комментарий-память агента
#   bash scripts/issues.sh note <N> <файл|->     то же произвольным текстом
#   bash scripts/issues.sh create "<заголовок>" "<метки через запятую>" <файл|->   по шаблону задачи
#   bash scripts/issues.sh label <N> <метки>     добавить метки
#   bash scripts/issues.sh labels                создать/обновить метки команды
#   bash scripts/issues.sh check [путь]          можно ли здесь работать: worktree задачи, задача моя и открыта
#
# «Движение» по задаче N — самое позднее из: обновление issue, обновление её открытого PR
# (ветка worktree-N-…), коммит в ветке worktree-N-… на GitHub. Порог — ISSUES_STALE_MIN (45).
set -uo pipefail

cmd="${1:-help}"
[ $# -gt 0 ] && shift
STALE_MIN="${ISSUES_STALE_MIN:-45}"
T="${ISSUES_TIMEOUT:-15}"
HOW="Сначала задача: bash scripts/issues.sh free → take <N> → git worktree add .claude/worktrees/<N>-<тема> -b worktree-<N>-<тема> origin/main (GIT.md)"

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"  # до любого cd: $0 может быть относительным
warn() { echo "[issues] $*" >&2; }

# --- check: правило «без взятой задачи не работаем». Ветка проверяется без сети; назначение
# сверяется с GitHub и кешируется на 30 минут в git-dir worktree. GitHub недоступен —
# предупреждение, но не запрет: номер в ветке уже есть.
if [ "$cmd" = check ]; then
  target="${1:-.}"
  while [ ! -d "$target" ] && [ "$target" != / ] && [ "$target" != . ]; do target="$(dirname "$target")"; done
  git -C "$target" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0  # вне репозитория — не наше
  home="$(cd "$(dirname "$0")" && cd "$(git rev-parse --git-common-dir)" && pwd -P)"
  cd "$target" || exit 0
  [ "$(cd "$(git rev-parse --git-common-dir)" && pwd -P)" = "$home" ] || exit 0  # другой репозиторий
  branch="$(git branch --show-current 2>/dev/null || true)"
  if [ "$(git rev-parse --absolute-git-dir)" = "$(cd "$(git rev-parse --git-common-dir)" && pwd -P)" ]; then
    echo "Основная копия ($branch): здесь файлы не меняют и не коммитят. $HOW" >&2
    exit 1
  fi
  n="$(printf '%s' "$branch" | sed -n 's/^worktree-\([0-9][0-9]*\)-.*/\1/p')"
  if [ -z "$n" ]; then
    echo "Ветка «$branch» без номера задачи: работа начинается только со взятой задачи. $HOW" >&2
    exit 1
  fi
  cache="$(git rev-parse --git-dir)/issue-check"
  if [ -f "$cache" ] && [ "$(cat "$cache")" = "$n" ] && [ -z "$(find "$cache" -mmin +30 2>/dev/null)" ]; then
    exit 0
  fi
  if ! command -v gh >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    warn "нет gh или jq — задача #$n не сверена с GitHub"
    exit 0
  fi
  verdict="$(ISSUES_TIMEOUT=5 bash "$SELF" verify "$n" 2>&1)"
  case $? in
    0) printf '%s' "$n" > "$cache"; exit 0 ;;
    1) echo "#$n: ${verdict#нет: }. $HOW" >&2; exit 1 ;;
    *) warn "GitHub недоступен — задача #$n не сверена ($verdict)"; exit 0 ;;
  esac
fi

# --- доступ к GitHub
remote="$(git remote get-url origin 2>/dev/null || true)"
repo="$(printf '%s' "$remote" | sed -nE 's#^(https?://([^@/]+@)?github\.com/|git@github\.com:)([^/]+/[^/]+)$#\3#p' | sed 's/\.git$//')"
offline() { # offline <код>: предупреждение вместо падения
  if [ -z "$repo" ]; then warn "нет remote origin на GitHub — задачи недоступны (git remote add origin …)"
  else warn "gh недоступен или не авторизован — задачи недоступны (gh auth login)"; fi
  exit "$1"
}
write_code=3; case "$cmd" in board|list|free|mine|view|help) write_code=0 ;; esac
[ "$cmd" = help ] || { [ -n "$repo" ] && command -v gh >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; } || offline "$write_code"
owner="${repo%%/*}" name="${repo##*/}"
g() { timeout "$T" gh "$@"; }

now="${ISSUES_NOW:-$(date +%s)}"
stale_s=$((STALE_MIN * 60))

# Одним GraphQL-запросом: открытые задачи, открытые PR, ветки worktree-*, кто я.
# shellcheck disable=SC2016
Q='query($owner:String!,$name:String!){viewer{login}
repository(owner:$owner,name:$name){
 issues(states:OPEN,first:100,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{number title createdAt updatedAt
  assignees(first:10){nodes{login}} labels(first:20){nodes{name}}
  timelineItems(last:1,itemTypes:[ASSIGNED_EVENT]){nodes{...on AssignedEvent{createdAt}}}}}
 pullRequests(states:OPEN,first:100){nodes{number headRefName updatedAt}}
 refs(refPrefix:"refs/heads/",query:"worktree-",first:100){nodes{name target{...on Commit{committedDate}}}}}}'
fetch() {
  local raw
  raw="$(g api graphql -f query="$Q" -f owner="$owner" -f name="$name" 2>/dev/null)" || return 1
  printf '%s' "$raw" | jq -e '.data.repository.issues.nodes | type == "array"' >/dev/null 2>&1 || return 1
  printf '%s' "$raw" | jq -c '{me: .data.viewer.login,
    issues: [.data.repository.issues.nodes[] | {number, title, createdAt, updatedAt,
      assignees: [.assignees.nodes[].login], labels: [.labels.nodes[].name],
      assignedAt: (.timelineItems.nodes[0].createdAt // null)}],
    prs: [.data.repository.pullRequests.nodes[] | {number, head: .headRefName, updatedAt}],
    refs: [.data.repository.refs.nodes[] | {name, at: (.target.committedDate // null)}]}'
}
# jq-библиотека: moved (последнее движение), branch (ветка задачи), fresh, utc, dur
LIB='def ep: if . == null then 0 else (sub("\\.[0-9]+"; "") | fromdateiso8601) end;
def pfx: "worktree-\(.number)-";
def brs($d): pfx as $p | (.createdAt | ep) as $c
  | [$d.refs[] | select(.name | startswith($p)) | {n: .name, t: (.at | ep)} | select(.t >= $c)];
def moved($d): pfx as $p | ([.updatedAt | ep] + [$d.prs[] | select(.head | startswith($p)) | .updatedAt | ep]
  + [brs($d)[].t]) | max;
def branch($d): brs($d) | max_by(.t) | .n // "";
def fresh($d): ($now - moved($d)) <= $stale;
def others($me): [.assignees[] | select(. != $me)];
def utc: strftime("%Y-%m-%d %H:%M UTC");
def dur: (. / 60 | floor) as $m | if $m >= 60 then "\($m / 60 | floor) ч \($m % 60) мин" else "\($m) мин" end;
def row: "#\(.number) \(.title) [\(.labels | join(", "))] — \(if (.assignees | length) > 0 then (.assignees | join(", ")) else "свободна" end)";'
J() { # J <json> [аргументы jq] <фильтр>
  local data="$1" f="${*: -1}"
  set -- "${@:2:$#-2}"
  printf '%s' "$data" | jq -r --argjson now "$now" --argjson stale "$stale_s" "$@" "$LIB . as \$d | $f"
}
need_data() { data="$(fetch)" || { warn "GitHub не ответил (сеть, права или ошибка) — движение неизвестно"; exit "$1"; }; }

case "$cmd" in
  list|free|mine)
    need_data 0
    lbl="${1:-}"
    J "$data" --arg l "$lbl" --arg c "$cmd" '.issues[]
      | select($l == "" or any(.labels[]; . == $l))
      | select($c != "free" or (.assignees | length) == 0)
      | select($c != "mine" or any(.assignees[]; . == $d.me)) | row'
    ;;
  view)
    g issue view "${1:?номер задачи}" -R "$repo" --comments || offline 0
    ;;
  verify) # для check: код 0 — моя и открыта; 1 — «нет: …»; 2 — GitHub не ответил
    n="${1:?}"
    me="$(g api user --jq .login 2>/dev/null)" || { echo "api user"; exit 2; }
    js="$(g issue view "$n" -R "$repo" --json state,assignees 2>&1)" || {
      printf '%s' "$js" | grep -qiE 'could not resolve|not found' && { echo "нет: задачи #$n в репозитории нет"; exit 1; }
      echo "issue view"; exit 2; }
    [ "$(printf '%s' "$js" | jq -r .state)" = OPEN ] || { echo "нет: задача закрыта"; exit 1; }
    printf '%s' "$js" | jq -e --arg me "$me" 'any(.assignees[]; .login == $me)' >/dev/null \
      || { echo "нет: не назначена на $me (bash scripts/issues.sh take $n)"; exit 1; }
    ;;
  take)
    n="${1:?номер задачи}"; force="${2:-}"
    need_data 1
    me="$(J "$data" '.me')"
    issue="$(printf '%s' "$data" | jq -c --argjson n "$n" '.issues[] | select(.number == $n)')"
    if [ -z "$issue" ]; then
      echo "#$n не найдена среди открытых задач (закрыта или нет такой): bash scripts/issues.sh free" >&2; exit 1
    fi
    sub() { J "$data" --argjson n "$n" --arg me "$me" ".issues[] | select(.number == \$n) | $1"; }
    other="$(sub 'others($me) | join(",")')"
    moved="$(sub 'moved($d)')"; branch="$(sub 'branch($d)')"
    age=$((now - moved))
    since="$(J "$data" --argjson m "$moved" '$m | utc') ($(J "$data" --argjson a "$age" '$a | dur') назад)"
    reason=""
    if [ -n "$other" ]; then
      if [ "$age" -le "$stale_s" ] && [ "$force" != --force ]; then
        echo "#$n взята: ${other//,/, }; последнее движение $since." >&2
        echo "Перехват — после $STALE_MIN мин без движения ($(J "$data" --argjson t "$((moved + stale_s))" '$t | utc'))" >&2
        echo "или по прямому указанию человека: bash scripts/issues.sh take $n --force. Сейчас — другую: free." >&2
        exit 1
      fi
      reason="без движения с $since"
      [ "$age" -le "$stale_s" ] && reason="по прямому указанию человека; последнее движение $since"
    fi
    # Мягкая занятость: та же area: у другого — предупреждение, не отказ.
    busy="$(sub '(.labels[] | select(startswith("area:"))) as $a
      | [$d.issues[] | select(.number != $n and any(.labels[]; . == $a) and (others($me) | length) > 0
          and (any(.assignees[]; . == $me) | not) and fresh($d))
        | "#\(.number) \(.assignees | join(",")) с \(.assignedAt // .updatedAt | ep | utc)"]
      | select(length > 0) | "\($a): \(join("; "))"')"
    if [ -n "$busy" ]; then
      warn "в той же области уже работают — $busy."
      warn "Берём, но договоритесь по файлам (docs/project-structure.md), чтобы не править одно и то же."
    fi
    args=(issue edit "$n" -R "$repo" --add-assignee "$me")
    [ -n "$other" ] && args+=(--remove-assignee "$other")
    g "${args[@]}" >/dev/null || { echo "#$n: gh issue edit не прошёл" >&2; exit 1; }
    if [ -n "$other" ]; then
      note="Перехвачена @$me у @${other//,/, @}: $reason."
      if [ -n "$branch" ]; then note="$note Ветка: \`$branch\` — продолжаю её, чужие коммиты не переписываю."
      else note="$note Ветки нет."; fi
      g issue comment "$n" -R "$repo" --body "$note" >/dev/null
      echo "#$n перехвачена: $me (у ${other//,/, }; $reason); комментарий оставлен."
    else
      echo "#$n взята: $me."
    fi
    if [ -n "$branch" ]; then
      echo "Ветка уже есть — продолжайте её:"
      echo "  git fetch origin $branch && git worktree add .claude/worktrees/${branch#worktree-} -b $branch origin/$branch"
    else
      echo "Worktree: git fetch --prune && git worktree add .claude/worktrees/$n-<тема> -b worktree-$n-<тема> origin/main"
    fi
    ;;
  note)
    n="${1:?номер задачи}"; shift
    if [ $# -ge 2 ]; then
      body="$(printf '**Сделано:** %s\n**Дальше:** %s\n**Блокер:** %s\n\n_%s, %s_' "$1" "$2" "${3:-нет}" \
        "$(git branch --show-current 2>/dev/null)" "$(git rev-parse --short HEAD 2>/dev/null)")"
      g issue comment "$n" -R "$repo" --body "$body" >/dev/null || offline 3
    else
      src="${1:?файл, - или три строки: сделано дальше блокер}"
      g issue comment "$n" -R "$repo" --body-file "$src" >/dev/null || offline 3
    fi
    echo "комментарий к #$n оставлен"
    ;;
  create)
    title="${1:?заголовок}" labels="${2:-}" body="${3:?файл описания по .github/ISSUE_TEMPLATE/task.md или -}"
    g issue create -R "$repo" --title "$title" ${labels:+--label "$labels"} --body-file "$body" || offline 3
    ;;
  label)
    g issue edit "${1:?номер}" -R "$repo" --add-label "${2:?метки}" >/dev/null && echo "#$1 +$2"
    ;;
  labels)
    while IFS='|' read -r lname color desc; do
      g label create "$lname" -R "$repo" --color "$color" --description "$desc" --force >/dev/null \
        && echo "метка $lname"
    done <<'LABELS'
area:data|1971C2|DATA: данные организаторов, словарь полей, eval, backtest, реальные кейсы
area:match|2F9E44|MATCH: понимание запроса, кандидаты, скоринг, объяснения, методология
area:sources|F08C00|SOURCES: открытые источники, ИНН, обогащение, кеш, надёжность
area:app|7048E8|APP: API, UI, docker-compose, демо, питч
P0|E03131|Критический путь демо и сдачи — раньше всего
P1|F59F00|Заметно лучше по пунктам условий
P2|ADB5BD|Только после P0/P1
blocker|C92A2A|Риск демо или сдачи, не закрытый в этом ходе
contract|0C8599|Меняет src/contracts.py — отдельный PR со списком потребителей
ui|E64980|UI-фича: UI-гейт из CLAUDE.md
experiment|8CE99A|Гипотеза и eval: строка в research/EXPERIMENTS.md
LABELS
    ;;
  board)
    data="$(fetch)" || { warn "GitHub недоступен — занятость не видна (bash scripts/issues.sh mine)"; exit 0; }
    J "$data" '[.issues[] | select(any(.labels[]; . == "blocker"))]
      | if length > 0 then "[blocker] открыты — закрыть раньше своей задачи:", (.[] | "  " + row) else empty end'
    J "$data" '[.issues[] | select(any(.assignees[]; . == $d.me))]
      | if length > 0 then "[issues] мои открытые (\($d.me)):", (.[] | "  " + row)
        else "[issues] за \($d.me) открытых задач нет. Взять: bash scripts/issues.sh free → take <N>" end'
    J "$data" '[.issues[] | select((others($d.me) | length) > 0 and (any(.assignees[]; . == $d.me) | not) and fresh($d))
        | {a: ([.labels[] | select(startswith("area:"))] | join(",") | if . == "" then "без area" else . end),
           w: "#\(.number) \(.assignees | join(",")) с \(.assignedAt // .updatedAt | ep | utc)"}]
      | if length > 0 then "[issues] в работе у других (та же area: — можно, но договориться по файлам):",
          (group_by(.a)[] | "  \(.[0].a): \([.[].w] | join("; "))") else empty end'
    J "$data" --arg m "$STALE_MIN" '[.issues[] | select((others($d.me) | length) > 0 and (any(.assignees[]; . == $d.me) | not)
        and (fresh($d) | not)) | "#\(.number) \(.assignees | join(",")), \(($now - moved($d)) | dur)"]
      | if length > 0 then "[issues] можно перехватить (без движения > \($m) мин): \(join("; ")) — take <N>, продолжать ветку задачи"
        else empty end'
    J "$data" '"[issues] свободных: \([.issues[] | select((.assignees | length) == 0)] | length) — bash scripts/issues.sh free"'
    ;;
  *)
    sed -n '2,21p' "$SELF" | sed 's/^# \{0,1\}//'
    ;;
esac
