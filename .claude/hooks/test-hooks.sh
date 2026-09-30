#!/usr/bin/env bash
# Проверка хуков Claude Code, git-хуков и scripts/issues.sh на временном git-репозитории с
# фальшивым gh: bash .claude/hooks/test-hooks.sh (входит в make check).
set -uo pipefail

HOOKS="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HOOKS/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
cd "$TMP" && git init -q -b main . && mkdir -p .claude
FAIL=0
ok() { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; FAIL=1; }

expect() { # expect <block|pass> <описание> <хук>
  local out
  out="$(bash "$HOOKS/$3" 2>&1)"
  if [ "$1" = block ] && ! printf '%s' "$out" | grep -q '"decision": *"block"'; then
    bad "$2 — ожидалась блокировка, вывод: $out"
  elif [ "$1" = pass ] && [ -n "$out" ]; then
    bad "$2 — ожидался пустой вывод, вывод: $out"
  else ok "$2"; fi
}

# --- check-secrets.sh
expect pass  "секреты: чистый репозиторий"                check-secrets.sh
echo 'LLM_API_KEY=your_api_key_here' > .env.example
expect pass  "секреты: .env.example с плейсхолдером"      check-secrets.sh
echo 'token = "f9a8Xc72kLmQ01zYvB3n4dHs"' > bot.py
expect block "секреты: токен в коде"                      check-secrets.sh
echo 'token = "f9a8Xc72kLmQ01zYvB3n4dHs"  # secret-scan: allow' > bot.py
expect pass  "секреты: строка с secret-scan: allow"       check-secrets.sh
rm bot.py
echo 'client = OpenAI(api_key="sk-3f9a8c72b1d04e6fa9b7c2d5e8f10a4b")' > llm.py
expect block "секреты: ключ LLM в коде"                   check-secrets.sh
rm llm.py
echo 'DADATA_API_KEY=abc' > .env
expect block "секреты: .env не в .gitignore"              check-secrets.sh
echo '.env' > .gitignore
expect pass  "секреты: .env в .gitignore"                 check-secrets.sh
printf -- '-----BEGIN RSA PRIVATE KEY-----\n' > key.pem
expect block "секреты: приватный ключ"                    check-secrets.sh
rm key.pem

# --- check-tests.sh (npm test в корне; pytest нет — нет pyproject.toml)
expect pass  "тесты: тестов нет — хук молчит"             check-tests.sh
mkdir -p node_modules
echo '{"scripts": {"test": "node -e \"process.exit(1)\""}}' > package.json
expect block "тесты: npm test падает"                     check-tests.sh
echo '{"scripts": {"test": "node -e \"process.exit(0)\""}}' > package.json
expect pass  "тесты: npm test зелёный"                    check-tests.sh
echo '{"scripts": {"test": "node -e \"process.exit(1)\""}}' > package.json
touch -d '-1 min' package.json
expect pass  "тесты: ничего не менялось с зелёного прогона" check-tests.sh
rm -rf node_modules package.json .claude/.last-green-test-run

# --- check-data-files.sh (страж данных)
guard() { # guard <block|pass> <описание>
  if bash "$HOOKS/check-data-files.sh" >/dev/null 2>&1; then
    [ "$1" = pass ] && ok "$2" || bad "$2 — ожидалась блокировка"
  else
    [ "$1" = block ] && ok "$2" || bad "$2 — ожидался пропуск"
  fi
}
mkdir -p data/raw src && echo 'a,b' > data/raw/contracts.csv && git add -f data/raw/contracts.csv
guard block "страж: data/raw в индексе"
git rm -q --cached data/raw/contracts.csv
echo x > src/a.parquet && git add -f src/a.parquet
guard block "страж: parquet в src/"
git rm -q --cached src/a.parquet && rm src/a.parquet
head -c $((6 * 1024 * 1024)) /dev/zero > src/big.txt && git add src/big.txt
guard block "страж: файл 6 МБ"
git rm -q --cached src/big.txt && rm src/big.txt
echo 'x = 1' > src/a.py && git add src/a.py
guard pass  "страж: обычный код"
out="$(jq -cn --arg c "cd $TMP && git commit -m x" '{tool_input: {command: $c}}' | bash "$HOOKS/check-data-files.sh" pre-tool)"
[ -z "$out" ] && ok "страж pre-tool: чистый индекс — пропуск" || bad "страж pre-tool: лишний вывод $out"
git add -f data/raw/contracts.csv
out="$(jq -cn --arg c "cd $TMP && git commit -m x" '{tool_input: {command: $c}}' | bash "$HOOKS/check-data-files.sh" pre-tool)"
printf '%s' "$out" | jq -e '.hookSpecificOutput.permissionDecision == "deny"' >/dev/null 2>&1 \
  && ok "страж pre-tool: данные — deny" || bad "страж pre-tool: нет deny, вывод $out"
git rm -q --cached data/raw/contracts.csv src/a.py && rm -rf data src

# --- фальшивый gh: GraphQL-фикстура, «сейчас» = 2026-10-01 12:00 UTC
mkdir -p "$TMP/bin" scripts
cp "$REPO/scripts/issues.sh" scripts/
cat > "$TMP/gql.json" <<'JSON'
{"data": {"viewer": {"login": "me"}, "repository": {
 "issues": {"nodes": [
  {"number": 3, "title": "BM25 по наименованиям", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T11:50:00Z",
   "assignees": {"nodes": [{"login": "me"}]}, "labels": {"nodes": [{"name": "area:match"}]}, "timelineItems": {"nodes": []}},
  {"number": 5, "title": "Карточка контрагента", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T11:40:00Z",
   "assignees": {"nodes": [{"login": "mate"}]}, "labels": {"nodes": [{"name": "area:app"}]},
   "timelineItems": {"nodes": [{"createdAt": "2026-10-01T11:00:00Z"}]}},
  {"number": 20, "title": "Брошенный профиль", "createdAt": "2026-10-01T08:00:00Z", "updatedAt": "2026-10-01T10:00:00Z",
   "assignees": {"nodes": [{"login": "gone"}]}, "labels": {"nodes": [{"name": "area:data"}]}, "timelineItems": {"nodes": []}},
  {"number": 7, "title": "Никем не взята", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T09:00:00Z",
   "assignees": {"nodes": []}, "labels": {"nodes": [{"name": "area:sources"}]}, "timelineItems": {"nodes": []}},
  {"number": 9, "title": "Демо не поднимается", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T09:00:00Z",
   "assignees": {"nodes": []}, "labels": {"nodes": [{"name": "blocker"}, {"name": "area:app"}]}, "timelineItems": {"nodes": []}},
  {"number": 13, "title": "Без движения, есть ветка", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T11:00:00Z",
   "assignees": {"nodes": [{"login": "mate"}]}, "labels": {"nodes": []}, "timelineItems": {"nodes": []}},
  {"number": 16, "title": "Свободная в занятой area:app", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T09:00:00Z",
   "assignees": {"nodes": []}, "labels": {"nodes": [{"name": "area:app"}]}, "timelineItems": {"nodes": []}},
  {"number": 17, "title": "Свежий PR", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T11:00:00Z",
   "assignees": {"nodes": [{"login": "mate"}]}, "labels": {"nodes": []}, "timelineItems": {"nodes": []}},
  {"number": 18, "title": "Свежий коммит", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T10:00:00Z",
   "assignees": {"nodes": [{"login": "mate"}]}, "labels": {"nodes": []}, "timelineItems": {"nodes": []}},
  {"number": 21, "title": "Свободная в area:data", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T09:00:00Z",
   "assignees": {"nodes": []}, "labels": {"nodes": [{"name": "area:data"}]}, "timelineItems": {"nodes": []}},
  {"number": 23, "title": "Ровно 45 минут", "createdAt": "2026-10-01T09:00:00Z", "updatedAt": "2026-10-01T11:15:00Z",
   "assignees": {"nodes": [{"login": "mate"}]}, "labels": {"nodes": []}, "timelineItems": {"nodes": []}},
  {"number": 24, "title": "Старая ветка с тем же номером", "createdAt": "2026-10-01T10:50:00Z", "updatedAt": "2026-10-01T10:50:00Z",
   "assignees": {"nodes": [{"login": "mate"}]}, "labels": {"nodes": []}, "timelineItems": {"nodes": []}}]},
 "pullRequests": {"nodes": [{"number": 40, "headRefName": "worktree-17-y", "updatedAt": "2026-10-01T11:50:00Z"}]},
 "refs": {"nodes": [
  {"name": "worktree-13-x", "target": {"committedDate": "2026-10-01T11:05:00Z"}},
  {"name": "worktree-18-z", "target": {"committedDate": "2026-10-01T11:30:00Z"}},
  {"name": "worktree-24-old", "target": {"committedDate": "2026-10-01T09:00:00Z"}}]}}}}
JSON
cat > "$TMP/bin/gh" <<'GH'
#!/usr/bin/env bash
[ -n "${GH_FAIL:-}" ] && exit 1
[ -n "${GH_LOG:-}" ] && printf '%s\n' "$*" >> "$GH_LOG"
case "$1 $2" in
  "auth status") exit 0 ;;
  "api graphql") [ -n "${GH_GQL_BAD:-}" ] && { echo '{"errors": [{"message": "boom"}]}'; exit 0; }
    cat "$GH_FIXTURE" ;;
  "api user") echo me ;;
  "issue view") case "$3" in
      12) echo '{"state": "OPEN", "assignees": [{"login": "me"}]}' ;;
      13) echo '{"state": "OPEN", "assignees": [{"login": "mate"}]}' ;;
      14) echo '{"state": "CLOSED", "assignees": [{"login": "me"}]}' ;;
      15) echo 'GraphQL: Could not resolve to an issue or pull request with the number of 15.' >&2; exit 1 ;;
    esac ;;
  "issue edit"|"issue comment"|"issue create"|"label create") : ;;
  *) echo "unexpected gh $*" >&2; exit 1 ;;
esac
GH
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH" GH_FIXTURE="$TMP/gql.json"
export ISSUES_NOW="$(jq -n '"2026-10-01T12:00:00Z" | fromdateiso8601')"
git add scripts && git -c user.name=t -c user.email=t@t commit -q -m init

# --- session-context.sh (SessionStart: stdout уходит в контекст)
expect_out() { # expect_out <has|lacks> <описание> <шаблон>
  local out
  out="$(bash "$HOOKS/session-context.sh" 2>&1)"
  if [ "$1" = has ] && ! printf '%s' "$out" | grep -q -- "$3"; then bad "$2 — нет «$3», вывод: $out"
  elif [ "$1" = lacks ] && printf '%s' "$out" | grep -q -- "$3"; then bad "$2 — лишнее «$3», вывод: $out"
  else ok "$2"; fi
}
expect_out has   "контекст: основная копия на main — предупреждение" "основной копии"
expect_out has   "контекст: нет remote — предупреждение, без падения" "нет remote origin"
git remote add origin https://github.com/team/plan.git
expect_out has   "контекст: мои задачи"                     "#3 BM25 по наименованиям"
expect_out has   "контекст: занятая другим area: с временем" "area:app: #5 mate с 2026-10-01 11:00 UTC"
expect_out lacks "контекст: свободная задача не занята"     "area:sources:"
expect_out has   "контекст: blocker виден"                  "#9 Демо не поднимается"
expect_out lacks "контекст: обычная задача не blocker"      "  #7 Никем не взята"
expect_out has   "контекст: кандидат на перехват > 45 мин"  "можно перехватить (без движения > 45 мин): #20 gone, 2 ч 0 мин"
expect_out lacks "контекст: брошенная area: не занята"      "area:data: #20"
GH_FAIL=1 expect_out has "контекст: GitHub недоступен — предупреждение" "GitHub недоступен"
mkdir -p docs && cat > docs/timeline.md <<'EOF'
- 2026-10-01 11:30 | Заморозка контрактов
- 2026-10-01 16:00 | Чек-поинт 1
EOF
SESSION_NOW="$(jq -n '"2026-10-01T08:00:00Z" | fromdateiso8601')" \
  expect_out has "контекст: ближайшая веха и время до неё" "2026-10-01 11:30 МСК — Заморозка контрактов (через 30 мин)"
SESSION_NOW="$(jq -n '"2026-10-01T09:00:00Z" | fromdateiso8601')" \
  expect_out has "контекст: прошедшая веха пропускается"   "Чек-поинт 1 (через 4 ч 0 мин)"
git worktree add -q .claude/worktrees/wt-test -b worktree-wt-test 2>/dev/null
(cd .claude/worktrees/wt-test && expect_out lacks "контекст: в worktree без предупреждения" "основной копии")

# --- check-issue.sh (PreToolUse: без взятой задачи не работаем)
expect_pre() { # expect_pre <block|pass> <описание> <json>
  local out code
  out="$(printf '%s' "$3" | bash "$HOOKS/check-issue.sh" 2>&1)"; code=$?
  if [ "$1" = block ] && [ "$code" != 2 ]; then bad "$2 — ожидался отказ (код 2), код $code, вывод: $out"
  elif [ "$1" = pass ] && [ "$code" != 0 ]; then bad "$2 — ожидался пропуск, код $code, вывод: $out"
  else ok "$2"; fi
}
edit() { printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$1"; }
bashcmd() { jq -cn --arg cwd "$1" --arg c "$2" '{tool_name: "Bash", cwd: $cwd, tool_input: {command: $c}}'; }
for n in 12 13 14 15; do git worktree add -q ".claude/worktrees/$n-x" -b "worktree-$n-x" 2>/dev/null; done
expect_pre block "задача: правка в основной копии"            "$(edit "$TMP/src/a.py")"
expect_pre block "задача: ветка без номера"                   "$(edit "$TMP/.claude/worktrees/wt-test/a.py")"
expect_pre pass  "задача: моя открытая"                       "$(edit "$TMP/.claude/worktrees/12-x/src/new/a.py")"
expect_pre block "задача: назначена на другого"               "$(edit "$TMP/.claude/worktrees/13-x/a.py")"
expect_pre block "задача: закрыта"                            "$(edit "$TMP/.claude/worktrees/14-x/a.py")"
expect_pre block "задача: такой нет"                          "$(edit "$TMP/.claude/worktrees/15-x/a.py")"
expect_pre pass  "задача: файл вне репозитория (scratchpad)"  "$(edit /tmp/scratch-note.md)"
expect_pre block "задача: git commit в основной копии"        "$(bashcmd "$TMP" "git add a && git commit -m x")"
expect_pre pass  "задача: git -C worktree commit"             "$(bashcmd "$TMP" "git -C $TMP/.claude/worktrees/12-x commit -m x")"
expect_pre pass  "задача: git -C \"путь\" commit в кавычках"  "$(bashcmd "$TMP" "git -C \"$TMP/.claude/worktrees/12-x\" commit -m x")"
expect_pre pass  "задача: обычная команда Bash"               "$(bashcmd "$TMP" "git status")"
expect_pre pass  "задача: git push внутри heredoc — данные"   "$(bashcmd "$TMP" "$(printf 'cat > /tmp/b.md <<EOF\nпотом git push\nEOF')")"
expect_pre pass  "задача: push --delete — уборка"             "$(bashcmd "$TMP" "git -C $TMP push origin --delete worktree-12-x")"
expect_pre pass  "задача: grep \"git push\" — текст, не команда" "$(bashcmd "$TMP" 'grep -rn "git push" GIT.md')"
expect_pre pass  "задача: git log | grep \"git commit\""       "$(bashcmd "$TMP" "git log | grep 'git commit'")"
expect_pre block "задача: git -c k=v commit в основной копии"  "$(bashcmd "$TMP" "git -c user.name=x commit -m y")"
expect_pre block "задача: git --no-pager commit"               "$(bashcmd "$TMP" "git --no-pager commit -m y")"
expect_pre block "задача: удаление ветки не освобождает соседний commit" "$(bashcmd "$TMP" "git push origin :old && git commit -m y")"
expect_pre block "задача: FOO=1 git commit"                     "$(bashcmd "$TMP" "FOO=1 git commit -m y")"
expect_pre pass  "задача: cd в worktree задачи && git commit"   "$(bashcmd "$TMP" "cd $TMP/.claude/worktrees/12-x && git commit -m y")"
expect_pre block "задача: cd в worktree, потом cd обратно"      "$(bashcmd "$TMP" "cd $TMP/.claude/worktrees/12-x && cd $TMP && git commit -m y")"
mkdir -p "$TMP/sp ace" && git -C "$TMP/sp ace" init -q
expect_pre pass  "задача: cd \"путь с пробелом\" в чужой репозиторий" "$(bashcmd "$TMP" "cd \"$TMP/sp ace\" && git commit -m y")"
expect_pre pass  "задача: сессия в подкаталоге worktree задачи"  "$(bashcmd "$TMP/.claude/worktrees/12-x/src" "git commit -m y")"
rm -f "$(git -C .claude/worktrees/13-x rev-parse --git-dir)/issue-check"
GH_FAIL=1 expect_pre pass "задача: GitHub недоступен — номер в ветке есть, пропуск" "$(edit "$TMP/.claude/worktrees/13-x/a.py")"
mkdir -p "$TMP/other" && git -C "$TMP/other" init -q
expect_pre pass  "задача: другой репозиторий"                 "$(edit "$TMP/other/a.py")"

# --- .githooks
if printf 'refs/heads/x 0000000000000000000000000000000000000000 refs/heads/x abc\n' \
  | bash "$REPO/.githooks/pre-push" origin url >/dev/null 2>&1; then ok "pre-push: удаление ветки пропускается"
else bad "pre-push: удаление ветки заблокировано"; fi
if printf 'refs/heads/main abc123 refs/heads/main def456\n' | bash "$REPO/.githooks/pre-push" origin url >/dev/null 2>&1; then
  bad "pre-push: push из основной копии пропущен"
else ok "pre-push: push из основной копии отклонён"; fi
mkdir -p .claude/hooks && cp "$HOOKS/check-data-files.sh" .claude/hooks/
if bash "$REPO/.githooks/pre-commit" >/dev/null 2>&1; then bad "pre-commit: коммит в основной копии пропущен"
else ok "pre-commit: коммит в основной копии отклонён"; fi
if (cd .claude/worktrees/12-x && mkdir -p .claude/hooks scripts && cp "$HOOKS/check-data-files.sh" .claude/hooks/ \
    && cp "$TMP/scripts/issues.sh" scripts/ && bash "$REPO/.githooks/pre-commit" >/dev/null 2>&1); then
  ok "pre-commit: worktree моей задачи — пропуск"
else bad "pre-commit: worktree моей задачи отклонён"; fi

# --- take: мягкая area:, 45 минут, --force
take_case() { # take_case <refuse|take> <описание> <шаблон вывода|""> <аргументы take…>
  local want="$1" desc="$2" pat="$3" out code edits notes
  shift 3
  : > "$TMP/gh.log"
  out="$(GH_LOG="$TMP/gh.log" bash scripts/issues.sh take "$@" 2>&1)"; code=$?
  edits="$(grep -c "^issue edit $1 " "$TMP/gh.log")"; notes="$(grep -c "^issue comment $1 " "$TMP/gh.log")"
  if [ "$want" = refuse ] && { [ "$code" = 0 ] || [ "$edits" != 0 ] || [ "$notes" != 0 ]; }; then
    bad "$desc — ожидался отказ без edit/comment (код $code, edit $edits, comment $notes), вывод: $out"
  elif [ "$want" = take ] && { [ "$code" != 0 ] || [ "$edits" != 1 ]; }; then
    bad "$desc — ожидалось назначение (код $code, edit $edits), вывод: $out"
  elif [ -n "$pat" ] && ! printf '%s' "$out" | grep -q -- "$pat"; then bad "$desc — нет «$pat», вывод: $out"
  else ok "$desc"; fi
}
take_case refuse "take: свежий PR (10 мин) — отказ"                 "take 17 --force" 17
take_case refuse "take: свежий коммит в ветке — отказ"              "11:30 UTC" 18
take_case refuse "take: ровно 45 мин — ещё отказ"                   "12:00 UTC" 23
take_case take   "take: 55 мин без движения — перехват"             "git worktree add .claude/worktrees/13-x -b worktree-13-x origin/worktree-13-x" 13
grep -q "^issue comment 13 .*без движения с 2026-10-01 11:05 UTC (55 мин назад).*worktree-13-x" "$TMP/gh.log" \
  && ok "take: перехват оставил комментарий с временем и веткой" || bad "take: нет комментария перехвата: $(cat "$TMP/gh.log")"
grep -q "^issue edit 13 .*--remove-assignee mate" "$TMP/gh.log" && ok "take: прежний исполнитель снят" \
  || bad "take: прежний исполнитель не снят: $(cat "$TMP/gh.log")"
take_case take   "take --force: прямое указание человека"           "по прямому указанию человека" 17 --force
grep -q "^issue comment 17 .*по прямому указанию человека" "$TMP/gh.log" && ok "take --force: комментарий оставлен" \
  || bad "take --force: нет комментария: $(cat "$TMP/gh.log")"
take_case take   "take: занятая area: — предупреждение, не отказ"   "area:app: #5 mate с 2026-10-01 11:00 UTC" 16
grep -q "^issue comment 16 " "$TMP/gh.log" && bad "take: свободная задача получила комментарий перехвата" \
  || ok "take: свободная задача без комментария перехвата"
take_case take   "take: брошенная area:data не предупреждает"       "#21 взята: me" 21
take_case take   "take: ветка старше задачи — не её ветка"          "origin/main" 24
grep -q "worktree-24-old" "$TMP/gh.log" && bad "take: старая ветка попала в комментарий" \
  || ok "take: старая ветка не попала в комментарий"
GH_GQL_BAD=1 take_case refuse "take: GitHub вернул ошибку — движение неизвестно" "движение неизвестно" 13
ISSUES_STALE_MIN=60 take_case refuse "take: порог ISSUES_STALE_MIN=60 — 55 мин свежие" "" 13
take_case refuse "take: закрытой задачи нет среди открытых"        "не найдена" 99
out="$(GH_LOG="$TMP/gh.log" bash scripts/issues.sh note 3 "BM25 готов" "RRF" "нет" 2>&1)"
grep -q '^issue comment 3 .*\*\*Сделано:\*\* BM25 готов' "$TMP/gh.log" && ok "note: сделано/дальше/блокер" \
  || bad "note: нет комментария: $out $(cat "$TMP/gh.log")"
git remote remove origin
out="$(bash scripts/issues.sh take 3 2>&1)"; code=$?
[ "$code" = 3 ] && printf '%s' "$out" | grep -q "нет remote" && ok "take без remote: предупреждение, код 3" \
  || bad "take без remote: код $code, вывод $out"
out="$(bash scripts/issues.sh board 2>&1)"; code=$?
[ "$code" = 0 ] && ok "board без remote: код 0" || bad "board без remote: код $code"
git remote add origin https://github.com/team/plan.git

# --- check-worktrees.sh (SubagentStop: секреты во всех worktree)
expect pass  "worktrees: секретов нигде нет"            check-worktrees.sh
echo 'token = "f9a8Xc72kLmQ01zYvB3n4dHs"' > .claude/worktrees/wt-test/bot.py
expect block "worktrees: секрет в чужом worktree"       check-worktrees.sh
rm .claude/worktrees/wt-test/bot.py

# --- check-upstream.sh: чужие коммиты в origin/main против моей работы
cd "$TMP" && git init -q --bare -b main remote.git
git clone -q remote.git mate 2>/dev/null
mate_push() { # mate_push <файл> <сообщение>
  (cd "$TMP/mate" && mkdir -p "$(dirname "$1")" && echo "$2" >> "$1" && git add "$1" &&
   git -c user.name=mate -c user.email=m@m commit -q -m "$2" && git push -q origin HEAD:main)
}
mate_push README.md "init"
git clone -q remote.git me && cd me && git checkout -q -b worktree-me origin/main
expect_up() { # expect_up <block|pass> <описание> [шаблон]
  local out
  out="$(echo '{}' | bash "$HOOKS/check-upstream.sh" stop 2>&1)"
  if [ "$1" = block ] && ! { printf '%s' "$out" | grep -q '"decision": *"block"' && printf '%s' "$out" | grep -q -- "$3"; }; then
    bad "$2 — ожидалась блокировка с «$3», вывод: $out"
  elif [ "$1" = pass ] && [ -n "$out" ]; then bad "$2 — ожидался пустой вывод, вывод: $out"
  else ok "$2"; fi
}
expect_up pass  "upstream: новых коммитов нет"
mate_push other.txt "app: чужой файл"
expect_up pass  "upstream: чужой коммит не пересекается"
echo mine > shared.txt
mate_push shared.txt "match: тот же файл"
expect_up block "upstream: пересечение с незакоммиченным файлом" "shared.txt"
expect_up pass  "upstream: то же состояние второй раз не блокирует"
mate_push src/contracts.py "data: поле в контракте"
expect_up block "upstream: изменён контракт — показать всегда" "src/contracts.py"
git remote set-url origin "$TMP/nope.git"
expect_up pass  "upstream: сеть недоступна — молчит"

[ "$FAIL" = 0 ] && echo "test-hooks: все проверки зелёные" || echo "test-hooks: ЕСТЬ ПАДЕНИЯ"
exit $FAIL
