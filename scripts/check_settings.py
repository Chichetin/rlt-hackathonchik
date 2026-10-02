"""The environment is read only in src/settings.py (make settings-check, #290).

AST, not grep: a mention in a docstring or a comment does not count, a lazy import inside a function does.
Files that still read it are on scripts/settings_allowlist.txt (one path per line, `#` comments); the list only
shrinks — an entry whose file no longer reads the environment is reported too, so it gets deleted.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = Path("scripts") / "settings_allowlist.txt"
HOME = "src/settings.py"
SKIP = ("node_modules", "dist", "__pycache__", ".venv")


def _reads_environment(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv") \
                and isinstance(node.value, ast.Name) and node.value.id == "os":
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "os" \
                and any(a.name in ("environ", "getenv") for a in node.names):
            return True
    return False


def readers(root: Path) -> list[str]:
    """Repo-relative paths of the files under src/ that read the environment, except src/settings.py."""
    found = []
    for p in sorted((root / "src").rglob("*.py")):
        parts = p.relative_to(root).parts
        rel = "/".join(parts)
        if rel == HOME or any(s in parts for s in SKIP) or parts[1] == "ui":
            continue
        if _reads_environment(ast.parse(p.read_text(encoding="utf-8"), filename=rel)):
            found.append(rel)
    return found


def allowed(root: Path) -> set[str]:
    path = root / ALLOWLIST
    if not path.exists():
        return set()
    lines = (ln.strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return {ln for ln in lines if ln and not ln.startswith("#")}


def check(root: Path) -> tuple[list[str], list[str]]:
    """(files that read the environment and are not allowed, allowlist entries that read nothing)."""
    found, allow = readers(root), allowed(root)
    return [f for f in found if f not in allow], sorted(allow - set(found))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the environment is read only in src/settings.py")
    ap.add_argument("--root", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    violations, stale = check(args.root)
    for f in violations:
        print(f"settings: {f} читает окружение — добавьте переменную в src/settings.py (и .env.example)")
    for f in stale:
        print(f"settings: {f} в {ALLOWLIST.as_posix()}, но окружение не читает — удалите строку")
    if violations or stale:
        return 1
    print(f"settings: окружение читается в src/settings.py и в {len(allowed(args.root))} файлах из allowlist")
    return 0


if __name__ == "__main__":
    sys.exit(main())
