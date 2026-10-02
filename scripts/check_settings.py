"""The environment is read only in src/settings.py (make settings-check, #290).

AST, not grep: a mention in a docstring or a comment does not count, a lazy import inside a function does.
No exceptions: the allowlist of files not migrated yet emptied out and was deleted in #297.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = "src/settings.py"
SKIP = ("node_modules", "dist", "__pycache__", ".venv")


ENV_NAMES = ("environ", "environb", "getenv", "getenvb")


def _reads_environment(tree: ast.AST) -> bool:
    os_names = {"os"} | {a.asname for n in ast.walk(tree) if isinstance(n, ast.Import)
                         for a in n.names if a.name == "os" and a.asname}  # `import os as o`
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ENV_NAMES \
                and isinstance(node.value, ast.Name) and node.value.id in os_names:
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "os" \
                and any(a.name in ENV_NAMES or a.name == "*" for a in node.names):
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the environment is read only in src/settings.py")
    ap.add_argument("--root", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    violations = readers(args.root)
    for f in violations:
        print(f"settings: {f} читает окружение — добавьте переменную в src/settings.py (и .env.example)")
    if violations:
        return 1
    print("settings: окружение читается только в src/settings.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
