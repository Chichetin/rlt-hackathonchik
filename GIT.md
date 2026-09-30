# GIT.md — правила ведения Git

Источник: раздел 2 `docs/source/hackathon-research-workflow.md` («Git и секреты»), переписан под
worktree + PR (D-002): trunk-based и `git pull --rebase` из документа **не используются**.

Цель: `main` в любой момент запускается (`make demo`), e2e демо зелёный, сданную версию можно
однозначно восстановить. Правило: **закончил одно логическое действие — проверь, закоммить и
запушь ветку.**

## Автономность агента

Агент коммитит, **пушит сам** (D-016), открывает PR и **после вердикта `reviewer` «merge» сам его
вливает** — без запроса подтверждения (`permissions.allow` в `.claude/settings.json`). Push в
`main` тоже не запрещён: обычный путь в `main` — PR после `reviewer`, прямой push — для
bootstrap, срочной починки сломанного `main` или по прямому указанию человека. Запрещено только
переписывать историю. Разрешение опирается на проверки, а не отменяет их:

- перед коммитом и push — `git fetch --prune` и просмотр обеих сторон расхождения;
- в коммит — только текущий шаг: `git add <явные пути>`. Никогда `git add -A`, `git add .`,
  `git commit -a`: в дереве может лежать чужая незакоммиченная работа;
- `--force`/`+refspec`, `--no-verify`, удаление `main`, `rebase`, `amend`, `reset --hard`,
  `clean`, `pull --rebase` — запрещены (`permissions.deny`); только по явной просьбе человека;
- push, как и коммит, — только из worktree взятой задачи (`check-issue.sh`, `.githooks/pre-push`);
- секреты, `.env`, `data/`, `cache/`, `indexes/`, `node_modules`, сборки не коммитятся
  (`.gitignore`, Stop-хук `check-secrets.sh`, страж `check-data-files.sh`).

## Задача → worktree

Основная копия (ветка `main`) — не рабочее место: там сидят человек и другие сессии. **Любая
задача, меняющая файлы, делается в своём worktree на своей ветке; одна задача — один worktree — один
PR.** Работы без задачи нет — ни одной строки в документе, ни срочной починки `main` (для починки
сначала задача с меткой `blocker`, это минута).

```bash
bash scripts/issues.sh take <N>                 # взять (или перехватить, см. CLAUDE.md)
git fetch --prune
git worktree add .claude/worktrees/<N>-<тема> -b worktree-<N>-<тема> origin/main
cp .env .claude/worktrees/<N>-<тема>/ 2>/dev/null
cd .claude/worktrees/<N>-<тема> && uv sync --frozen --extra pg && (cd src/ui && npm ci)
# Claude Code сразу в worktree: claude -w <N>-<тема>
```

`<тема>` — 2–3 слова латиницей: `12-bm25-names`. Перехваченная задача — продолжение её ветки:
`git fetch origin <ветка> && git worktree add .claude/worktrees/<N>-<тема> -b <ветка> origin/<ветка>`.

Правило держат:

| Где | Что проверяет | Для кого |
|---|---|---|
| `.claude/hooks/check-issue.sh` (PreToolUse) | правка файла, `git commit`, `git push` — только в worktree `worktree-<N>-…`, задача #N открыта и на мне | Claude Code и субагенты |
| `.githooks/pre-commit`, `pre-push` | то же + страж данных | люди, IDE, другие агенты; подключает `make setup` |
| `bash scripts/issues.sh check [путь]` | то же вручную | перед началом работы |

Сверка с GitHub кешируется на 30 минут; GitHub недоступен — пропуск с предупреждением (номер в
ветке уже есть). `--no-verify` — только по явной просьбе человека.

## Финиш: ветка → PR → main

```bash
make check                                  # lint, typecheck, pytest, vitest, хуки, OpenAPI, # STUB
make e2e                                    # если затронуты API или UI; из worktree: E2E_PORT=8766
git fetch --prune && git merge origin/main  # main ушёл вперёд → проверки заново
git add <пути> && git commit -m "<area>: <что закончено>"
git push -u origin HEAD
gh pr create --fill --body-file <описание по .github/pull_request_template.md>   # Closes #<N>
```

Затем субагент `reviewer` (только diff против `origin/main`). Вердикт:

- `merge` → `gh pr merge <PR> --merge --delete-branch` (без squash и rebase — обычный merge-коммит),
  затем в основной копии, если там никто не работает, `git pull --ff-only`;
- `доработать` → правки в той же ветке, повторный `reviewer`;
- два отклонения подряд → арбитр `jury`.

**Merge — строго по одному:** перед каждым — `git merge origin/main` в ветке и `make check`.
Сломанный `main` чинит тот, чей merge его сломал, — раньше своей задачи. PR, ломающий e2e демо, не
вливается.

## Коммиты

Формат: `<area>: <что закончено>`, по-русски. Префиксы: `data:`, `match:`, `sources:`, `app:`,
`eval:`, `infra:` (Docker, CI, зависимости, `.claude/`), `docs:`, `pitch:`, `fix:`. Один коммит —
один осмысленный шаг. Трейлеров соавторства ИИ (`Co-Authored-By`, «Generated with…») нет.

## Уборка

Ветку и worktree удаляет тот, кто их завёл, если **оба** условия выполнены:

1. ветка влита: `git fetch --prune && git rev-list --count origin/main..<ветка>` → `0`;
2. в worktree нет незакоммиченного (`git -C <путь> status --porcelain` → пусто) и он не открыт
   другой сессией.

```bash
git worktree remove .claude/worktrees/<N>-<тема>     # без --force, из основной копии
git branch -D worktree-<N>-<тема>
git worktree prune
```

Чужой worktree с незакоммиченными изменениями или ветку с невлитыми коммитами не трогать никогда.

## Перед началом работы

```bash
git fetch --prune && git status -sb
git log --oneline HEAD..origin/main      # что пришло от команды
bash scripts/issues.sh board             # мои задачи, занятые area:, blocker, перехват
```

## Remote и сдача

`origin` — `https://github.com/Chichetin/rlt-hackathonchik`. Хостинг и формат сдачи
`[ОБНОВИТЬ 01.10]` (возможно, понадобится зеркало на площадку организаторов — отдельная задача
`area:app`). Тег сдачи — после скилла `submission-checklist`, на commit из `origin/main`:

```bash
git fetch --prune
git tag -a submit-final origin/main -m "Сдача: <commit>, демо пройдено <дата>"
git push origin submit-final
```

## Безопасный откат

| Ситуация | Действие |
|---|---|
| Вернуть свой файл | `git restore <path>` после просмотра diff |
| Отменить влитый коммит | `git revert <hash>` в новой ветке задачи → PR |
| Посмотреть старое состояние | `git switch --detach <tag-or-hash>` |
