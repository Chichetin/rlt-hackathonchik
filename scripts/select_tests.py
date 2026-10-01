"""Which checks a change needs: pytest files and e2e, by the import graph of src (CLAUDE.md, «Проверки»).

    python scripts/select_tests.py changed [base-ref]          → changed paths vs merge-base (default origin/main):
                                                                 branch commits, staged, unstaged, untracked, deleted
    python scripts/select_tests.py pytest <changed files...>   → test files, one per line; ALL = whole suite
    python scripts/select_tests.py pyright <changed files...>  → changed .py + every src/tests module importing them
    python scripts/select_tests.py e2e <changed files...>      → exit 0 if the demo e2e is affected, 1 if not

A test is affected when it, a conftest.py above it, or any src module it imports — directly or
transitively, including imports inside functions and fixtures — changed. The graph only knows .py
files in src/ and tests/; documents, the UI, hooks and shell scripts cannot affect pytest; anything
else (configs/, eval/cases/, fixtures, pyproject, a deleted or renamed module, a test helper) selects
the whole suite: a missed test is worse than an extra run. Pure stdlib: runs the same on Windows and
Linux without the project venv.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Changes pytest cannot see: documents, the UI (tsc, vitest, e2e), hooks and shell scripts (test-hooks).
PYTEST_NEUTRAL_PREFIXES = ("src/ui/", ".claude/", ".githooks/", ".github/", "docs/", "research/", "pitch/")
PYTEST_NEUTRAL_SUFFIXES = (".md", ".sh")
# The demo scenario (src/ui/e2e/demo.spec.ts) runs on the fixtures through the API and the built UI;
# its recipe lives in the Makefile.
E2E_PREFIXES = ("src/ui/", "tests/fixtures/", "configs/", "pyproject.toml", "uv.lock", "Makefile")
# The API and what playwright.config.ts webServer runs before it (ingest of the fixture, rmsp seed).
E2E_ENTRIES = ("src/api/app.py", "src/ingest/build.py", "src/sources/rmsp.py")
UI_SKIP = ("src/ui/node_modules/", "src/ui/dist/", "src/ui/test-results/")


def _norm(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./")


def _module_file(dotted: str) -> str | None:
    """src.match.index → src/match/index.py (or the package __init__.py)."""
    base = ROOT.joinpath(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand.relative_to(ROOT).as_posix()
    return None


def _package_inits(rel: str) -> list[str]:
    """Importing src/match/index.py also runs src/__init__.py and src/match/__init__.py."""
    parts = rel.split("/")[:-1]
    inits = ["/".join(parts[: i + 1]) + "/__init__.py" for i in range(len(parts))]
    return [p for p in inits if (ROOT / p).is_file() and p != rel]


@cache
def _parse(rel: str) -> ast.Module | None:
    try:
        return ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError, ValueError):
        return None


def _resolve(dotted: list[str]) -> set[str]:
    out: set[str] = set()
    for name in dotted:
        if name == "src" or name.startswith("src."):
            f = _module_file(name)
            if f:
                out.add(f)
                out.update(_package_inits(f))
    return out


def _import_names(node: ast.Import | ast.ImportFrom, rel: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if node.level:
        pkg = rel.rsplit("/", 1)[0].split("/")
        mod = ".".join(pkg[: len(pkg) - node.level + 1] + ([node.module] if node.module else []))
    else:
        mod = node.module or ""
    return [mod] + [f"{mod}.{a.name}" for a in node.names]  # from src.match import index


def _imports_in(tree: ast.AST, rel: str) -> set[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names += _import_names(node, rel)
    return _resolve(names) - {rel}


@cache
def imports_of(rel: str) -> frozenset[str]:
    """src/*.py files imported anywhere in the file (top level, functions, fixtures)."""
    tree = _parse(rel)
    return frozenset(_imports_in(tree, rel)) if tree else frozenset()


def _arg_names(tree: ast.AST) -> set[str]:
    funcs = (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return {a.arg for fn in funcs for a in fn.args.args}


@cache
def conftest_deps(rel: str) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """(src files every test below gets, src files per fixture/helper name).

    A module-level import used only inside a fixture reaches only the tests that request that
    fixture (directly or through other fixtures); autouse fixtures and top-level code reach all.
    """
    tree = _parse(rel)
    if tree is None:
        return frozenset(), {}
    bindings: dict[str, set[str]] = {}
    funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    common: set[str] = set()
    rest: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = _import_names(node, rel)[0]
            for alias in node.names:
                if isinstance(node, ast.Import):
                    bound, files = alias.asname or alias.name.split(".")[0], _resolve([alias.name])
                else:  # a submodule (from src.match import index) or a name defined in mod
                    bound, files = alias.asname or alias.name, _resolve([f"{mod}.{alias.name}"]) or _resolve([mod])
                if files:
                    bindings.setdefault(bound, set()).update(files)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node
        else:
            rest.append(node)

    def used(tree_: ast.AST) -> set[str]:
        return {n.id for n in ast.walk(tree_) if isinstance(n, ast.Name)} | _arg_names(tree_)

    for node in rest:
        for name in used(node):
            common |= bindings.get(name, set())

    resolved: dict[str, frozenset[str]] = {}

    def deps(name: str, stack: frozenset[str] = frozenset()) -> frozenset[str]:
        if name in resolved:
            return resolved[name]
        fn = funcs[name]
        out = _imports_in(fn, rel)
        for ref in used(fn) - stack - {name}:
            out |= bindings.get(ref, set())
            if ref in funcs:
                out |= deps(ref, stack | {name})
        resolved[name] = frozenset(out)
        return resolved[name]

    for name, fn in funcs.items():
        # autouse fixtures and pytest_* hooks act on every test below this conftest
        if name.startswith("pytest_") or any("autouse" in ast.unparse(d) for d in fn.decorator_list):
            common |= deps(name)
    return frozenset(common), {name: deps(name) for name in funcs}


def _requested(test: str) -> set[str]:
    """Fixture names a test file may request: function args and string constants (usefixtures)."""
    tree = _parse(test)
    if tree is None:
        return set()
    names = _arg_names(tree)
    names |= {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return names


@cache
def closure(rel: str) -> frozenset[str]:
    seen: set[str] = set()
    stack = [rel]
    while stack:
        cur = stack.pop()
        for dep in imports_of(cur):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return frozenset(seen)


def _conftests(test: str) -> list[str]:
    parts = test.split("/")[:-1]
    found = ["/".join(parts[: i + 1]) + "/conftest.py" for i in range(len(parts))]
    return [c for c in found if (ROOT / c).is_file()]


def _all_tests() -> list[str]:
    return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").rglob("test_*.py"))


def _in_graph(path: str) -> bool:
    """An existing src module, test file or conftest — the import graph knows who depends on it.

    A deleted (or renamed-away) module leaves no file to trace importers to; a test helper or
    tests/**/__init__.py is not modelled.
    """
    if not path.endswith(".py") or not (ROOT / path).is_file():
        return False
    if path.startswith("src/"):
        return True
    name = path.rsplit("/", 1)[-1]
    return path.startswith("tests/") and (name.startswith("test_") or name == "conftest.py")


def select_pytest(changed: list[str]) -> list[str]:
    for f in changed:
        if not (f.startswith(PYTEST_NEUTRAL_PREFIXES) or f.endswith(PYTEST_NEUTRAL_SUFFIXES) or _in_graph(f)):
            return ["ALL"]
    changed_set = set(changed)
    picked: list[str] = []
    for test in _all_tests():
        conftests = _conftests(test)
        direct = set(imports_of(test))
        for conf in conftests:
            common, per_fixture = conftest_deps(conf)
            direct |= common
            for name in _requested(test) & per_fixture.keys():
                direct |= per_fixture[name]
        deps = {test, *conftests, *direct}.union(*(closure(f) for f in direct))
        if deps & changed_set:
            picked.append(test)
    return picked


def select_pyright(changed: list[str]) -> list[str]:
    """Changed .py plus every src/tests module importing them: a changed signature breaks callers."""
    touched = {f for f in changed if f.endswith(".py")}
    out = {f for f in touched if (ROOT / f).is_file() and f.startswith(("src/", "tests/"))}
    for p in [*ROOT.glob("src/**/*.py"), *ROOT.glob("tests/**/*.py")]:
        rel = p.relative_to(ROOT).as_posix()
        if closure(rel) & touched:
            out.add(rel)
    return sorted(out)


def needs_e2e(changed: list[str]) -> bool:
    for f in changed:
        if f.startswith(E2E_PREFIXES) and not f.startswith(UI_SKIP):
            return True
        if f.startswith("src/") and f.endswith(".py") and not (ROOT / f).exists():
            return True  # a deleted module may be imported on the demo path
    deps = set(E2E_ENTRIES).union(*(closure(e) for e in E2E_ENTRIES))
    return any(f in deps for f in changed)


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True)
    return [line for line in out.stdout.splitlines() if line.strip()]


def changed_files(base_ref: str = "origin/main") -> list[str]:
    """Raises CalledProcessError when base_ref is unknown (no remote, no fetch)."""
    base = _git("merge-base", "HEAD", base_ref)[0]
    # core.quotepath=off: Cyrillic file names come out as-is, not as octal escapes.
    q = ("-c", "core.quotepath=off")
    # --no-renames: a rename must also list the old path, or its forgotten importers go unseen.
    paths = _git(*q, "diff", "--name-only", "--no-renames", base)
    paths += _git(*q, "diff", "--name-only", "--no-renames", "--cached")
    paths += _git(*q, "ls-files", "--others", "--exclude-standard")
    return sorted({_norm(p) for p in paths})


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("changed", "pytest", "pyright", "e2e"):
        print(__doc__, file=sys.stderr)
        return 2
    if argv[1] == "changed":
        try:
            print("\n".join(changed_files(argv[2] if len(argv) > 2 else "origin/main")))
        except (subprocess.CalledProcessError, IndexError, OSError) as exc:
            print(f"select_tests: нет базы для сравнения ({exc})", file=sys.stderr)
            return 3
        return 0
    changed = [_norm(f) for f in argv[2:] if f.strip()]
    if argv[1] == "e2e":
        return 0 if needs_e2e(changed) else 1
    print("\n".join(select_pyright(changed) if argv[1] == "pyright" else select_pytest(changed)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
