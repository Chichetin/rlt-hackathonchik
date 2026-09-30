#!/usr/bin/env bash
# Stop hook: ход не считается завершённым, если в файлах, которые могут попасть
# в коммит (tracked + untracked, не игнорируемые), лежит похожее на секрет значение.
# Ключи API (LLM, DaData, поиск) живут только в .env. См. CLAUDE.md, «Guardrails».
# Ложное срабатывание: дописать в строку комментарий `secret-scan: allow`.
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || exit 0  # хук может стартовать из подкаталога

mapfile -t FILES < <(git ls-files -co --exclude-standard 2>/dev/null | grep -v '^\.claude/hooks/')
[ "${#FILES[@]}" -eq 0 ] && exit 0

FOUND=""

for f in "${FILES[@]}"; do
  case "$(basename "$f")" in
    .env.example) ;;
    .env|.env.*) FOUND+="$f: файл окружения не в .gitignore"$'\n' ;;
  esac
done

EXISTING=()
for f in "${FILES[@]}"; do [ -f "$f" ] && EXISTING+=("$f"); done

if [ "${#EXISTING[@]}" -gt 0 ]; then
  HITS="$(grep -I -n -i -E \
      -e '-----BEGIN [A-Z ]*PRIVATE KEY-----' \
      -e '(token|secret|password|passwd|api[_-]?key)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9_.+/=-]{16,}' \
      -e 'gh[pousr]_[A-Za-z0-9]{36}' \
      -e 'sk-[A-Za-z0-9_-]{20,}' \
      -- "${EXISTING[@]}" 2>/dev/null \
    | grep -v -i -E 'secret-scan: allow|your|example|changeme|placeholder|xxxx|<[a-z_]+>|\$\{|process\.env|os\.environ|getenv' \
    | cut -d: -f1,2)"
  [ -n "$HITS" ] && FOUND+="$(printf '%s' "$HITS" | sed 's/$/: похоже на секрет/')"$'\n'
fi

[ -z "$FOUND" ] && exit 0

jq -n --arg found "$FOUND" \
  '{decision: "block", reason: ("Возможные секреты в файлах, которые попадут в коммит (значения не выводятся):\n" + $found + "Вынести в .env (в .gitignore) и .env.example с плейсхолдером. Ложное срабатывание — пометить строку `secret-scan: allow`.")}'
exit 0
