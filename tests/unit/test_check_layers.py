"""scripts/check_layers.py (`make layers`): an import goes down the layers, never up (#288, D-106).

A toy src/ tree in tmp_path with its own layer table: direct, lazy, TYPE_CHECKING and relative
imports, a cycle, a module without a layer, the allowlist. The last test runs the real tree.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_layers.py"

# toy layers: base 0 < mid 1 < mid.special 2 (an exception inside the package) < top 3
TOY = {"src.base": 0, "src.mid": 1, "src.mid.special": 2, "src.top": 3}
BASE_FILES = {
    "src/__init__.py": "",
    "src/base/__init__.py": "",
    "src/base/types.py": "X = 1\n",
    "src/mid/__init__.py": "",
    "src/mid/core.py": "from src.base.types import X\n",
    "src/mid/core2.py": "from src.mid.core import X\n",
    "src/mid/special.py": "from src.mid import core\n",
    "src/top/__init__.py": "",
    "src/top/app.py": "from src.mid.special import X\nfrom src.base import types\n",
}


@pytest.fixture(scope="module")
def mod() -> Iterator[ModuleType]:
    name = "check_layers_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m  # @dataclass looks its module up here
    try:
        spec.loader.exec_module(m)
        yield m
    finally:
        del sys.modules[name]


def tree(tmp_path: Path) -> Path:
    for rel, text in BASE_FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def put(tmp_path: Path, rel: str, text: str) -> Path:
    """The toy tree with one file replaced or added."""
    root = tree(tmp_path)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return root


def bad(mod: ModuleType, root: Path) -> list:
    return mod.violations(mod.collect_edges(root, mod.find_modules(root)), TOY)


def pairs(edges: list) -> set[tuple[str, str]]:
    return {(e.src, e.dst) for e in edges}


def test_toy_tree_without_violations_is_clean(mod: ModuleType, tmp_path: Path) -> None:
    root = tree(tmp_path)
    assert bad(mod, root) == []
    report = mod.check(root, {}, TOY)
    assert report.new == [] and report.unassigned == [] and report.stale == []
    assert report.modules == 9 and report.edges == 5  # src, 3 packages, 5 modules; 5 imports between them


def test_direct_import_up_is_a_violation_with_file_and_line(mod: ModuleType, tmp_path: Path) -> None:
    root = put(tmp_path, "src/base/types.py", "X = 1\nfrom src.mid.core import X as Y\n")
    (e,) = bad(mod, root)
    assert (e.src, e.dst, e.path, e.line, e.how) == ("src.base.types", "src.mid.core", "src/base/types.py", 2, "top")


def test_lazy_import_up_is_seen(mod: ModuleType, tmp_path: Path) -> None:
    text = (
        "def f():\n"
        "    from src.mid.core import X\n"
        "    return X\n\n"
        "async def g():\n"
        "    import src.top.app\n\n"
        "class C:\n"
        "    def m(self):\n"
        "        from src.mid import core\n"
    )
    root = put(tmp_path, "src/base/types.py", text)
    got = {(e.dst, e.line, e.how) for e in bad(mod, root)}
    assert got == {("src.mid.core", 2, "lazy"), ("src.top.app", 6, "lazy"), ("src.mid.core", 10, "lazy")}


def test_import_after_a_function_is_top_level_again(mod: ModuleType, tmp_path: Path) -> None:
    text = "def f():\n    import os\n\nfrom src.mid.core import X\n"
    root = put(tmp_path, "src/base/types.py", text)
    assert [(e.line, e.how) for e in bad(mod, root)] == [(4, "top")]


def test_class_body_import_is_not_lazy(mod: ModuleType, tmp_path: Path) -> None:
    root = put(tmp_path, "src/base/types.py", "class C:\n    from src.mid.core import X\n")
    assert [e.how for e in bad(mod, root)] == ["top"]


def test_type_checking_import_counts(mod: ModuleType, tmp_path: Path) -> None:
    text = (
        "import typing\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from src.mid.core import X\n"
        "else:\n"
        "    from src.top.app import Y\n"
        "if typing.TYPE_CHECKING:\n"
        "    from src.top import app\n"
    )
    root = put(tmp_path, "src/base/types.py", text)
    got = {(e.dst, e.line, e.how) for e in bad(mod, root)}
    assert got == {("src.mid.core", 4, "type-checking"), ("src.top.app", 6, "top"), ("src.top.app", 8, "type-checking")}


def test_relative_imports_are_resolved(mod: ModuleType, tmp_path: Path) -> None:
    root = tree(tmp_path)
    (root / "src/mid/core.py").write_text(
        "from . import special\nfrom .special import X\nfrom ..top import app\nfrom ..base import types\n",
        encoding="utf-8",
    )
    (root / "src/mid/__init__.py").write_text("from .special import X\n", encoding="utf-8")
    got = {(e.src, e.dst, e.line) for e in bad(mod, root)}
    assert got == {
        ("src.mid.core", "src.mid.special", 1),  # L1 -> L2: from . import <module>
        ("src.mid.core", "src.mid.special", 2),  # from .special import X
        ("src.mid.core", "src.top.app", 3),  # from ..top import <module>: two dots, up to src
        ("src.mid", "src.mid.special", 1),  # in an __init__.py a single dot is the package itself
    }


def test_from_import_of_a_submodule_points_at_the_module(mod: ModuleType, tmp_path: Path) -> None:
    root = tree(tmp_path)
    (root / "src/base/types.py").write_text("from src.mid import special, Name\n", encoding="utf-8")
    assert pairs(bad(mod, root)) == {("src.base.types", "src.mid.special"), ("src.base.types", "src.mid")}


def test_cycle_only_the_upward_edge_is_a_violation(mod: ModuleType, tmp_path: Path) -> None:
    root = put(tmp_path, "src/base/types.py", "def f():\n    from src.top import app\n")
    # src.top.app already imports src.base.types (downward)
    assert pairs(mod.collect_edges(root, mod.find_modules(root))) >= {
        ("src.base.types", "src.top.app"),
        ("src.top.app", "src.base.types"),
    }
    assert pairs(bad(mod, root)) == {("src.base.types", "src.top.app")}


def test_third_party_stdlib_and_unknown_modules_are_ignored(mod: ModuleType, tmp_path: Path) -> None:
    text = (
        "import os\nimport yaml\nfrom pathlib import Path\n"
        "from srcery import x\nimport src_other\nfrom src.ghost import y\n"
    )
    root = put(tmp_path, "src/base/types.py", text)
    assert bad(mod, root) == []


def test_layer_is_the_longest_dotted_prefix(mod: ModuleType) -> None:
    assert mod.layer_of("src.mid.core", TOY) == 1
    assert mod.layer_of("src.mid.special", TOY) == 2
    assert mod.layer_of("src.mid.special.deep", TOY) == 2
    assert mod.layer_of("src.mid", TOY) == 1
    assert mod.layer_of("src.base_extra", TOY) is None  # a prefix is whole dotted segments
    assert mod.layer_of("src.elsewhere.x", TOY) is None
    assert mod.layer_of("src", TOY) == 0  # the empty root package


def test_module_without_a_layer_is_reported(mod: ModuleType, tmp_path: Path) -> None:
    root = put(tmp_path, "src/newpkg/x.py", "from src.top import app\n")
    report = mod.check(root, {}, TOY)
    assert report.unassigned == ["src.newpkg.x"]
    assert report.new == []  # edges of a module without a layer are not judged: the error is the missing layer


def test_allowlist_covers_only_the_listed_edge(mod: ModuleType, tmp_path: Path) -> None:
    root = put(tmp_path, "src/base/types.py", "from src.mid.core import X\nfrom src.top.app import Y\n")
    allowed = {("src.base.types", "src.mid.core"): 3}
    report = mod.check(root, allowed, TOY)
    assert pairs(report.allowed) == {("src.base.types", "src.mid.core")}
    assert pairs(report.new) == {("src.base.types", "src.top.app")}


def test_allowlist_line_without_a_violation_is_stale_not_an_error(mod: ModuleType, tmp_path: Path) -> None:
    root = tree(tmp_path)
    allowed = {("src.base.types", "src.mid.core"): 7, ("src.top.app", "src.base.types"): 8}  # 2nd is a legal edge
    report = mod.check(root, allowed, TOY)
    assert report.new == [] and report.allowed == []
    assert report.stale == [("src.base.types", "src.mid.core", 7), ("src.top.app", "src.base.types", 8)]


def test_load_allowlist(mod: ModuleType, tmp_path: Path) -> None:
    f = tmp_path / "allow.txt"
    f.write_text(
        "# header\n\nsrc.a -> src.b\n  src.c->src.d   # why\n# src.e -> src.f\n",
        encoding="utf-8",
    )
    assert mod.load_allowlist(f) == {("src.a", "src.b"): 3, ("src.c", "src.d"): 4}
    assert mod.load_allowlist(tmp_path / "missing.txt") == {}


@pytest.mark.parametrize("line", ["src.a src.b", "src.a ->", "-> src.b", "src.a -> src.b -> src.c", "src.a x -> src.b"])
def test_broken_allowlist_line_is_an_error_with_its_number(mod: ModuleType, tmp_path: Path, line: str) -> None:
    f = tmp_path / "allow.txt"
    f.write_text(f"# ok\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"allow\.txt:2"):
        mod.load_allowlist(f)


def test_unparsable_file_is_an_error_naming_the_file(mod: ModuleType, tmp_path: Path) -> None:
    root = put(tmp_path, "src/base/types.py", "def broken(:\n")
    with pytest.raises(ValueError, match=r"src/base/types\.py"):
        mod.collect_edges(root, mod.find_modules(root))


def test_node_modules_and_caches_are_skipped(mod: ModuleType, tmp_path: Path) -> None:
    root = put(tmp_path, "src/ui/node_modules/pkg/hook.py", "def broken(:\n")
    assert not any("node_modules" in name for name in mod.find_modules(root))


@pytest.fixture
def toy_layers(mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "LAYERS", TOY)
    monkeypatch.setattr(mod, "LAYER_NAMES", {})


def test_cli_clean_tree_exits_0(
    mod: ModuleType, toy_layers: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tree(tmp_path)
    assert mod.main(["--root", str(root)]) == 0
    assert "layers: OK" in capsys.readouterr().out


def test_cli_new_violation_exits_1_and_names_file_line_layers(
    mod: ModuleType, toy_layers: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = put(tmp_path, "src/base/types.py", "X = 1\n\ndef f():\n    from src.mid.core import X\n")
    assert mod.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "src/base/types.py:4: src.base.types (L0) -> src.mid.core (L1) [ленивый импорт]" in out
    assert "новых импортов вверх по слоям: 1" in out


def test_cli_allowlist_makes_it_green_and_stale_line_only_warns(
    mod: ModuleType, toy_layers: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = put(tmp_path, "src/base/types.py", "from src.mid.core import X\n")
    allow = root / "scripts" / "layers_allowlist.txt"
    allow.parent.mkdir()
    allow.write_text(
        "src.base.types -> src.mid.core  # old\nsrc.top.app -> src.mid.core  # fixed long ago\n", encoding="utf-8"
    )
    assert mod.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "layers: OK" in out and "допущенных нарушений 1" in out
    assert "устарела строка layers_allowlist.txt:2: src.top.app -> src.mid.core" in out


def test_cli_module_without_layer_exits_1(
    mod: ModuleType, toy_layers: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = put(tmp_path, "src/newpkg/x.py", "X = 1\n")
    assert mod.main(["--root", str(root)]) == 1
    assert "модули без слоя" in capsys.readouterr().out


def test_cli_broken_allowlist_and_broken_source_exit_2(
    mod: ModuleType, toy_layers: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tree(tmp_path)
    allow = tmp_path / "bad.txt"
    allow.write_text("src.a src.b\n", encoding="utf-8")
    assert mod.main(["--root", str(root), "--allowlist", str(allow)]) == 2
    assert "bad.txt:1" in capsys.readouterr().out
    (root / "src/base/types.py").write_text("def broken(:\n", encoding="utf-8")
    assert mod.main(["--root", str(root)]) == 2
    assert "src/base/types.py" in capsys.readouterr().out


def test_cli_all_prints_every_violation_in_allowlist_format(
    mod: ModuleType, toy_layers: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = put(tmp_path, "src/base/types.py", "from src.mid.core import X\n")
    assert mod.main(["--root", str(root), "--all"]) == 0
    assert capsys.readouterr().out.strip() == "src.base.types -> src.mid.core  # src/base/types.py:1 top"


def test_real_tree_passes_with_the_committed_allowlist(mod: ModuleType) -> None:
    """The guard `make layers` runs on the repository: every src module has a layer, no new upward import."""
    allowed = mod.load_allowlist(ROOT / "scripts" / "layers_allowlist.txt")
    report = mod.check(ROOT, allowed)
    assert report.unassigned == []
    assert [mod.describe(e) for e in report.new] == []
    assert report.modules > 50  # the scan really looked at src/
