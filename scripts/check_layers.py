"""Layer guard for src/: an import goes down the layers or stays in its own, never up (D-106, #288).

    python scripts/check_layers.py [--root DIR] [--allowlist FILE] [--all]      (make layers)

Every module of src/ gets a layer from LAYERS (longest dotted prefix wins); a module of layer Lk may
import modules of layers <= k. The scan is `ast` over every file, so imports inside functions (lazy)
and under `if TYPE_CHECKING:` are seen too — the interpreter would not notice them, but they are
still a dependency. Violations that already exist are listed edge by edge (module -> module) in
scripts/layers_allowlist.txt; a new one is an error, a line removed from the list is never one.
A list line that no longer matches a real violation is reported as stale (exit 0).

Not seen: imports built at run time (`importlib.import_module("src.x")`) — there are none in src/.
Pure stdlib: runs the same on Windows and Linux without the project venv.

Exit: 0 clean, 1 new violation or a module without a layer, 2 broken allowlist or unparsable file.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = Path(__file__).with_name("layers_allowlist.txt")
TOP = "src"
SKIP_DIRS = frozenset({"node_modules", "dist", "__pycache__", ".venv"})

# Dotted prefix -> layer; the longest prefix wins, so a package line is the default for its modules and
# a deeper line is an exception. A module with no prefix here is an error: new packages are placed on
# purpose. Keep in step with docs/project-structure.md, «Правила импортов».
LAYERS: dict[str, int] = {
    # L0: types and settings every layer may read
    "src.contracts": 0,
    "src.regions": 0,  # one table of regions (#292)
    "src.settings": 0,
    # L1: external sources, storage, the ingest core, the task queue
    "src.sources": 1,
    "src.db": 1,
    "src.llm": 1,
    "src.ingest": 1,  # build, load, notice, okpd2, profile, split_config, testset
    "src.worker.queue": 1,
    # L2: the match core (store, index, search, fallback, filters, signals, profiles, text)
    "src.match": 2,
    # L3: enrichment and discovery of new companies
    "src.enrich": 3,
    # L4: orchestration of the product use cases
    "src.match.pipeline": 4,
    "src.ingest.predict": 4,
    # L5: entry points — HTTP and the worker process
    "src.api": 5,
    "src.worker": 5,
    # L6: tools — they may import the product, nothing of the product imports them
    "src.eval": 6,
    "src.match.ranker_train": 6,
    "src.ui": 6,  # build-time helpers of the UI (#628 gen-light-theme.py): the product never imports them  # offline ranker training (#198): reads eval cases; the product never imports it
}
LAYER_NAMES = {
    0: "контракты, настройки",
    1: "источники, БД, ядро ingest",
    2: "ядро match",
    3: "enrich",
    4: "оркестрация",
    5: "api, worker",
    6: "инструменты",
}

Allowed = dict[tuple[str, str], int]


@dataclass(frozen=True)
class Edge:
    """One import: `src` imports `dst` at `path:line`; `how` is top, lazy (inside a function) or type-checking."""

    src: str
    dst: str
    path: str
    line: int
    how: str


@dataclass
class Report:
    modules: int
    edges: int
    unassigned: list[str] = field(default_factory=list)  # modules no LAYERS prefix covers
    new: list[Edge] = field(default_factory=list)  # upward imports missing from the allowlist: errors
    allowed: list[Edge] = field(default_factory=list)  # upward imports the allowlist covers
    stale: list[tuple[str, str, int]] = field(default_factory=list)  # allowlist lines with no violation behind


def layer_of(module: str, layers: dict[str, int] | None = None) -> int | None:
    """Layer of a dotted module name by the longest matching prefix; None when no prefix matches."""
    table = LAYERS if layers is None else layers
    if module == TOP:
        return 0  # the empty root package
    best: str | None = None
    for prefix in table:
        if (module == prefix or module.startswith(prefix + ".")) and (best is None or len(prefix) > len(best)):
            best = prefix
    return table[best] if best is not None else None


def _module_name(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def find_modules(root: Path) -> dict[str, Path]:
    """Dotted name -> file for every .py under root/src (node_modules and caches skipped)."""
    out: dict[str, Path] = {}
    for p in sorted((root / TOP).rglob("*.py")):
        rel = p.relative_to(root)
        if SKIP_DIRS.intersection(rel.parts):
            continue
        out[_module_name(rel)] = p
    return out


def _resolve(name: str, modules: dict[str, Path]) -> str | None:
    """Longest dotted prefix of `name` that is a module of src: `src.a.b.Name` -> `src.a.b`."""
    parts = name.split(".")
    while parts:
        cand = ".".join(parts)
        if cand in modules:
            return cand
        parts.pop()
    return None


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


class _Imports(ast.NodeVisitor):
    """Every import statement of a module with the context it runs in (class bodies run at import)."""

    def __init__(self) -> None:
        self.found: list[tuple[ast.Import | ast.ImportFrom, str]] = []
        self._depth = 0  # function nesting
        self._tc = 0  # `if TYPE_CHECKING:` nesting

    def _how(self) -> str:
        return "lazy" if self._depth else ("type-checking" if self._tc else "top")

    def visit_Import(self, node: ast.Import) -> None:
        self.found.append((node, self._how()))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.found.append((node, self._how()))

    def _function(self, node: ast.AST) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking(node.test):
            self.generic_visit(node)
            return
        self._tc += 1
        for stmt in node.body:
            self.visit(stmt)
        self._tc -= 1
        for stmt in node.orelse:
            self.visit(stmt)


def _targets(node: ast.Import | ast.ImportFrom, module: str, is_package: bool, modules: dict[str, Path]) -> set[str]:
    """Modules of src a statement imports: `from src.m import a` -> src.m.a if that is a module, else src.m."""
    if isinstance(node, ast.Import):
        names = [a.name for a in node.names if a.name == TOP or a.name.startswith(TOP + ".")]
        return {t for n in names if (t := _resolve(n, modules))}
    if node.level:
        pkg = module.split(".") if is_package else module.split(".")[:-1]
        keep = len(pkg) - node.level + 1
        if keep < 1:
            return set()  # beyond the top package: not ours to judge
        base = ".".join(pkg[:keep] + ([node.module] if node.module else []))
    else:
        base = node.module or ""
    if base != TOP and not base.startswith(TOP + "."):
        return set()  # third party or stdlib
    out: set[str] = set()
    for alias in node.names:
        sub = f"{base}.{alias.name}"
        target = sub if sub in modules else _resolve(base, modules)
        if target:
            out.add(target)
    return out


def collect_edges(root: Path, modules: dict[str, Path]) -> list[Edge]:
    edges: list[Edge] = []
    for name, path in modules.items():
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"{rel}: файл не разобран ({exc})") from exc
        visitor = _Imports()
        visitor.visit(tree)
        for node, how in visitor.found:
            for dst in sorted(_targets(node, name, path.name == "__init__.py", modules)):
                if dst != name:
                    edges.append(Edge(name, dst, rel, node.lineno, how))
    return edges


def violations(edges: Iterable[Edge], layers: dict[str, int] | None = None) -> list[Edge]:
    """Edges that go up: the importer's layer is lower than the imported module's."""
    out = []
    for e in edges:
        low, high = layer_of(e.src, layers), layer_of(e.dst, layers)
        if low is not None and high is not None and low < high:
            out.append(e)
    return out


def load_allowlist(path: Path) -> Allowed:
    """`src.a -> src.b  # why`, one edge per line; blank lines and # comments are ignored."""
    out: Allowed = {}
    if not path.is_file():
        return out
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("->")]
        if len(parts) != 2 or not all(parts) or any(" " in p for p in parts):
            raise ValueError(f"{path.name}:{n}: ожидается `src.модуль -> src.модуль`, а не «{raw.strip()}»")
        out[(parts[0], parts[1])] = n
    return out


def check(root: Path, allowed: Allowed, layers: dict[str, int] | None = None) -> Report:
    modules = find_modules(root)
    edges = collect_edges(root, modules)
    bad = violations(edges, layers)
    present = {(e.src, e.dst) for e in bad}
    return Report(
        modules=len(modules),
        edges=len(edges),
        unassigned=sorted(m for m in modules if layer_of(m, layers) is None),
        new=[e for e in bad if (e.src, e.dst) not in allowed],
        allowed=[e for e in bad if (e.src, e.dst) in allowed],
        stale=[(s, d, n) for (s, d), n in allowed.items() if (s, d) not in present],
    )


def _label(module: str, layers: dict[str, int] | None) -> str:
    layer = layer_of(module, layers)
    return f"L{layer}, {LAYER_NAMES[layer]}" if layers is None and layer in LAYER_NAMES else f"L{layer}"


def describe(e: Edge, layers: dict[str, int] | None = None) -> str:
    tag = {"lazy": " [ленивый импорт]", "type-checking": " [TYPE_CHECKING]"}.get(e.how, "")
    return f"{e.path}:{e.line}: {e.src} ({_label(e.src, layers)}) -> {e.dst} ({_label(e.dst, layers)}){tag}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("--root", type=Path, default=ROOT, help="корень репозитория (внутри — src/)")
    ap.add_argument("--allowlist", type=Path, default=None, help="файл с допущенными рёбрами")
    ap.add_argument("--all", action="store_true", help="напечатать все нарушения в формате allowlist и выйти")
    args = ap.parse_args(argv)
    root: Path = args.root.resolve()
    allow_path: Path = args.allowlist or root / "scripts" / ALLOWLIST.name
    try:
        if args.all:
            for e in violations(collect_edges(root, find_modules(root))):
                print(f"{e.src} -> {e.dst}  # {e.path}:{e.line} {e.how}")
            return 0
        report = check(root, load_allowlist(allow_path))
    except ValueError as exc:
        print(f"layers: ОШИБКА — {exc}")
        return 2

    for m, n, ln in report.stale:
        print(f"layers: устарела строка {allow_path.name}:{ln}: {m} -> {n} (нарушения уже нет — строку можно удалить)")
    if report.unassigned or report.new:
        if report.unassigned:
            print(f"layers: ОШИБКА — модули без слоя ({len(report.unassigned)}); добавьте префикс в LAYERS "
                  "(scripts/check_layers.py) и в docs/project-structure.md:")
            print("\n".join("  " + m for m in report.unassigned))
        if report.new:
            print(f"layers: ОШИБКА — новых импортов вверх по слоям: {len(report.new)}")
            print("\n".join("  " + describe(e) for e in report.new))
            print("  Импорт идёт только вниз или внутри слоя: вынесите нужное вниз (общий тип — в src/contracts.py) "
                  "или передайте зависимость аргументом.")
            print("  Схема слоёв — docs/project-structure.md, «Правила импортов», D-106.")
        return 1
    print(f"layers: OK — модулей {report.modules}, импортов {report.edges}, "
          f"допущенных нарушений {len(report.allowed)} ({allow_path.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
