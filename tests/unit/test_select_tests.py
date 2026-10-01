"""scripts/select_tests.py: only tests of the touched modules run (make check-changed, Stop hook)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "select_tests.py"

TREE = {
    "src/__init__.py": "",
    "src/base/__init__.py": "",
    "src/base/util.py": "X = 1\n",
    "src/feat/__init__.py": "",
    "src/feat/core.py": "from src.base.util import X\n",
    "src/feat/rel.py": "from .core import X\n",
    "src/other/__init__.py": "",
    "src/other/lone.py": "Y = 2\n",
    "src/api/__init__.py": "",
    "src/api/app.py": "from src.feat import rel\n",
    "tests/__init__.py": "",
    "tests/unit/__init__.py": "",
    "tests/unit/conftest.py": (
        "import pytest\n"
        "from src.feat.core import X\n\n"
        "@pytest.fixture\n"
        "def heavy():\n"
        "    return X\n\n"
        "@pytest.fixture\n"
        "def lazy():\n"
        "    from src.other import lone\n"
        "    return lone\n"
    ),
    "tests/unit/test_core.py": "def test_a(heavy):\n    assert heavy\n",
    "tests/unit/test_lone.py": "from src.other.lone import Y\n\ndef test_b():\n    assert Y\n",
    "tests/unit/test_lazy.py": "def test_c(lazy):\n    assert lazy\n",
}


@pytest.fixture
def sel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for rel, text in TREE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("select_tests_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def _short(paths: list[str]) -> set[str]:
    return {p.rsplit("/", 1)[-1] for p in paths}


def test_transitive_import_through_fixture(sel: ModuleType) -> None:
    # util ← core ← conftest fixture `heavy` ← test_core; test_lone/test_lazy do not touch util.
    assert _short(sel.select_pytest(["src/base/util.py"])) == {"test_core.py"}


def test_import_inside_fixture_reaches_only_its_users(sel: ModuleType) -> None:
    assert _short(sel.select_pytest(["src/other/lone.py"])) == {"test_lone.py", "test_lazy.py"}


def test_windows_separators_and_docs(sel: ModuleType) -> None:
    assert _short(sel.select_pytest([sel._norm(r"src\other\lone.py")])) == {"test_lone.py", "test_lazy.py"}
    assert sel.select_pytest(["docs/ui.md", "README.md"]) == []


def test_test_file_and_conftest(sel: ModuleType) -> None:
    assert _short(sel.select_pytest(["tests/unit/test_lone.py"])) == {"test_lone.py"}
    assert _short(sel.select_pytest(["tests/unit/conftest.py"])) == {"test_core.py", "test_lone.py", "test_lazy.py"}


def test_autouse_fixture_reaches_everyone(sel: ModuleType, tmp_path: Path) -> None:
    conf = tmp_path / "tests/unit/conftest.py"
    autouse = "\n@pytest.fixture(autouse=True)\ndef env():\n    from src.base import util\n"
    conf.write_text(conf.read_text(encoding="utf-8") + autouse, encoding="utf-8")
    assert _short(sel.select_pytest(["src/base/util.py"])) == {"test_core.py", "test_lone.py", "test_lazy.py"}


def test_pytest_hook_in_conftest_reaches_everyone(sel: ModuleType, tmp_path: Path) -> None:
    conf = tmp_path / "tests/unit/conftest.py"
    hook = "\ndef pytest_configure(config):\n    from src.base import util\n"
    conf.write_text(conf.read_text(encoding="utf-8") + hook, encoding="utf-8")
    assert _short(sel.select_pytest(["src/base/util.py"])) == {"test_core.py", "test_lone.py", "test_lazy.py"}


@pytest.mark.parametrize(
    "path",
    [
        "configs/search.yaml",
        "tests/fixtures/ingest/x.csv",
        "eval/cases/dev.jsonl",  # data in git read by code, not imported
        "src/feat/deleted.py",  # no such file: who imported it is unknown
        "tests/unit/helpers.py",  # test helpers are not modelled
        "tests/unit/__init__.py",
        "docker-compose.yml",
    ],
)
def test_whole_suite_when_graph_is_blind(sel: ModuleType, path: str) -> None:
    (sel.ROOT / "tests/unit/helpers.py").write_text("", encoding="utf-8")
    assert sel.select_pytest(["docs/ui.md", path]) == ["ALL"]


def test_rename_lists_the_old_path_so_importers_are_not_lost(sel: ModuleType, tmp_path: Path) -> None:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "core.hooksPath="]
    subprocess.run([*git, "init", "-q", "-b", "main", "."], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "."], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=tmp_path, check=True)
    subprocess.run([*git, "mv", "src/other/lone.py", "src/other/alone.py"], cwd=tmp_path, check=True)
    changed = sel.changed_files("HEAD")
    assert {"src/other/lone.py", "src/other/alone.py"} <= set(changed)
    assert sel.select_pytest(changed) == ["ALL"]  # test_lone.py still imports src.other.lone


def test_pyright_covers_importers_of_changed_module(sel: ModuleType) -> None:
    got = set(sel.select_pyright(["src/base/util.py", "docs/ui.md"]))
    # importers of util, transitively, including the conftest that imports src.feat.core
    expected = {"src/base/util.py", "src/feat/core.py", "src/feat/rel.py", "src/api/app.py", "tests/unit/conftest.py"}
    assert expected <= got
    assert "src/other/lone.py" not in got and "tests/unit/test_lone.py" not in got


def test_e2e_follows_api_graph(sel: ModuleType) -> None:
    assert sel.needs_e2e(["src/base/util.py"])  # util ← core ← rel (relative import) ← api/app
    assert not sel.needs_e2e(["src/other/lone.py"])
    assert sel.needs_e2e(["src/ui/src/App.tsx"])
    assert sel.needs_e2e(["Makefile"])  # the e2e and demo recipes live there
    assert sel.needs_e2e(["src/feat/deleted.py"])
    assert not sel.needs_e2e(["src/ui/dist/index.html", "docs/ui.md"])
