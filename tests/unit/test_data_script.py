"""scripts/data.sh (`make data`): refusals that need no network — an unknown part and a missing gh."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "data.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not installed")


def run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(BASH), str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8",
                          env=env, check=False)


def test_unknown_part_is_refused_before_any_download(tmp_path):
    env = {**os.environ, "DATA_PROCESSED_DIR": str(tmp_path / "p"), "DATA_RAW_DIR": str(tmp_path / "r")}
    res = run(["nope"], env)
    assert res.returncode == 1 and "неизвестная часть 'nope'" in res.stderr
    assert not (tmp_path / "p").exists() and not (tmp_path / "r").exists()


def test_without_gh_says_what_to_install_and_writes_nothing(tmp_path):
    # PATH with bash's own directory tools only: a directory that has no gh
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("dirname", "mktemp", "rm"):
        found = shutil.which(name)
        if found:
            try:
                (tools / name).symlink_to(found)
            except OSError:  # Windows without the symlink privilege
                pytest.skip("symlinks are not available")
    env = {"PATH": str(tools), "DATA_PROCESSED_DIR": str(tmp_path / "p"), "DATA_RAW_DIR": str(tmp_path / "r")}
    res = run(["processed"], env)
    assert res.returncode == 1 and "нужен GitHub CLI (gh)" in res.stderr
    assert not (tmp_path / "p").exists()
