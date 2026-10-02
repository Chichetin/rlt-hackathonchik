"""scripts/check_settings.py: the environment is read only in src/settings.py (make settings-check, #290)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_settings.py"


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_settings", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def tree(root: Path, files: dict[str, str], allow: str = "") -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "settings_allowlist.txt").write_text(allow, encoding="utf-8")
    return root


@pytest.mark.parametrize("code", [
    "import os\nX = os.environ['A']\n",
    "import os\nX = os.environ.get('A', 'b')\n",
    "import os\nX = os.getenv('A')\n",
    "from os import environ\nX = environ['A']\n",
    "from os import getenv\nX = getenv('A')\n",
    "import os\nif 'A' in os.environ:\n    pass\n",
    "import os as o\nX = o.environ\n",
    "from os import *\n",
    "import os\nX = os.environb\n",
    "import os\nX = os.getenvb(b'A')\n",
    "from os import environ as e\nX = e\n",
])
def test_every_way_to_read_the_environment_is_found(mod: ModuleType, tmp_path: Path, code: str) -> None:
    root = tree(tmp_path, {"src/match/x.py": code})
    violations, stale = mod.check(root)
    assert violations == ["src/match/x.py"] and stale == []


def test_mentions_in_strings_and_comments_do_not_count(mod: ModuleType, tmp_path: Path) -> None:
    root = tree(tmp_path, {"src/match/x.py": '"""reads os.environ["A"] later"""\n# os.getenv("B")\nX = "os.environ"\n'})
    assert mod.check(root) == ([], [])


def test_settings_module_and_the_ui_are_exempt(mod: ModuleType, tmp_path: Path) -> None:
    root = tree(tmp_path, {"src/settings.py": "import os\nX = os.environ\n",
                           "src/ui/e2e/x.py": "import os\nX = os.environ\n",
                           "src/ui/node_modules/p/x.py": "import os\nX = os.environ\n"})
    assert mod.check(root) == ([], [])


def test_an_allowlisted_file_is_tolerated(mod: ModuleType, tmp_path: Path) -> None:
    root = tree(tmp_path, {"src/sources/old.py": "import os\nX = os.getenv('A')\n"},
                allow="# migrated in #297\n\nsrc/sources/old.py\n")
    assert mod.check(root) == ([], [])


def test_an_allowlist_entry_that_no_longer_reads_the_environment_is_stale(mod: ModuleType, tmp_path: Path) -> None:
    root = tree(tmp_path, {"src/sources/old.py": "X = 1\n"}, allow="src/sources/old.py\nsrc/gone.py\n")
    violations, stale = mod.check(root)
    assert violations == [] and stale == ["src/gone.py", "src/sources/old.py"]


def test_main_exit_codes(mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clean = tree(tmp_path / "a", {"src/x.py": "X = 1\n"})
    assert mod.main(["--root", str(clean)]) == 0
    dirty = tree(tmp_path / "b", {"src/x.py": "import os\nX = os.environ\n"})
    assert mod.main(["--root", str(dirty)]) == 1
    assert "src/x.py" in capsys.readouterr().out


def test_the_repository_has_no_new_environment_reads(mod: ModuleType) -> None:
    """The real tree: every read outside src/settings.py is on the allowlist, and the list has no dead lines."""
    violations, stale = mod.check(SCRIPT.parents[1])
    assert violations == [] and stale == []
