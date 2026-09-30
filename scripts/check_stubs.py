"""`# STUB` in the demo path: warning before code freeze, error after (CLAUDE.md, «Проверки кодом»).

Demo path = src/ without src/ui/node_modules and tests. Freeze moment — CODE_FREEZE in
docs/timeline.md (ISO, Moscow time); STUB_STRICT=1 forces the error mode.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
SKIP = ("node_modules", "dist", "__pycache__", ".venv")
MARK = re.compile(r"^\s*(#|//)\s*STUB\b")  # the marker opens a line; mentions in prose do not count


def code_freeze() -> datetime | None:
    m = re.search(r"CODE_FREEZE:\s*(\S+)", (ROOT / "docs" / "timeline.md").read_text(encoding="utf-8"))
    return datetime.fromisoformat(m.group(1)).replace(tzinfo=MSK) if m else None


def find_stubs() -> list[str]:
    hits = []
    for p in sorted((ROOT / "src").rglob("*")):
        if not p.is_file() or any(s in p.parts for s in SKIP) or p.suffix not in (".py", ".ts", ".tsx"):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            if MARK.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
    return hits


def main() -> int:
    hits = find_stubs()
    if not hits:
        print("stubs: в демо-пути нет # STUB")
        return 0
    freeze = code_freeze()
    strict = os.environ.get("STUB_STRICT") == "1" or (freeze is not None and datetime.now(MSK) >= freeze)
    head = "ОШИБКА" if strict else "предупреждение"
    print(f"stubs: {head} — # STUB в демо-пути ({len(hits)}), code freeze {freeze}:")
    print("\n".join("  " + h for h in hits))
    return 1 if strict else 0


if __name__ == "__main__":
    sys.exit(main())
