"""The lock on the holdout period: only `make eval-final` for the frozen config reads it."""

import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from src.eval import final as F
from src.eval import run as R
from src.eval.split import SealedError, load_cases, split_config, unseal
from src.match import search as search_mod

ROOT = Path(__file__).resolve().parents[2]


def test_holdout_is_sealed_for_regular_code():
    with pytest.raises(SealedError):
        load_cases("holdout")


def test_eval_run_cannot_take_final_id(capsys):
    assert R.main(["--exp", "final-x"]) == 2


def test_dev_cases_load_and_respect_dates():
    cases = load_cases("dev")
    assert cases and all(c.date < date.fromisoformat(split_config()["holdout_from"]) for c in cases)


def test_dev_case_from_holdout_period_is_a_leak(tmp_path):
    p = tmp_path / "dev.jsonl"
    p.write_text('{"query": {"query_id": "q", "text": "x"}, "relevant_inns": [], "date": "2026-08-01"}\n')
    cfg = {**split_config(), "cases": {"dev": str(p), "holdout": str(p)}}
    with pytest.raises(ValueError, match="split leak"):
        load_cases("dev", cfg)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clean git repo with the frozen paths, so the gate checks a real tree."""
    for rel in ("configs/search.yaml", "configs/split.yaml"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    (tmp_path / "src" / "match").mkdir(parents=True)
    (tmp_path / "src" / "match" / "a.py").write_text("x = 1\n")
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(search_mod, "IS_STUB", False)
    return tmp_path


def test_gate_refuses_without_confirm(repo):
    with pytest.raises(PermissionError, match="нет EVAL_FINAL_CONFIRM"):
        F.gate(None, repo, repo / "log.md")


def test_gate_refuses_wrong_hash(repo):
    with pytest.raises(PermissionError, match="не совпадает"):
        F.gate("0" * 64, repo, repo / "log.md")


def test_gate_refuses_dirty_tree(repo):
    (repo / "src" / "match" / "a.py").write_text("x = 2\n")
    with pytest.raises(PermissionError, match="не чистое"):
        F.gate(R.config_sha256(repo), repo, repo / "log.md")


def test_gate_refuses_second_run_of_same_config(repo):
    sha = R.config_sha256(repo)
    (repo / "log.md").write_text(f"| t | c | {sha} |\n")
    with pytest.raises(PermissionError, match="уже оценивалась"):
        F.gate(sha, repo, repo / "log.md")


def test_gate_refuses_stub_search(repo, monkeypatch):
    monkeypatch.setattr(search_mod, "IS_STUB", True)
    with pytest.raises(PermissionError, match="STUB"):
        F.gate(R.config_sha256(repo), repo, repo / "log.md")


def test_gate_passes_frozen_clean_config(repo):
    assert F.gate(R.config_sha256(repo), repo, repo / "log.md") == R.config_sha256(repo)


def test_config_hash_changes_with_weights(repo):
    before = R.config_sha256(repo)
    p = repo / "configs" / "search.yaml"
    text = p.read_text(encoding="utf-8").replace("product_fit: 0.762", "product_fit: 0.4")
    p.write_text(text, encoding="utf-8")
    assert R.config_sha256(repo) != before


def test_unseal_is_scoped(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"query": {"query_id": "q", "text": "x"}, "relevant_inns": [], "date": "2026-08-01"}\n')
    cfg = {**split_config(), "cases": {"dev": str(p), "holdout": str(p)}}
    with unseal():
        assert len(load_cases("holdout", cfg)) == 1
    with pytest.raises(SealedError):
        load_cases("holdout", cfg)


def test_final_main_refuses_in_this_repo(monkeypatch, capsys):
    monkeypatch.delenv(F.CONFIRM_ENV, raising=False)
    assert F.main() == 2
    assert "отказ" in capsys.readouterr().err


def test_gate_refuses_without_git(tmp_path, monkeypatch):
    for rel in ("configs/search.yaml", "configs/split.yaml"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    monkeypatch.setattr(R, "ROOT", tmp_path)
    with pytest.raises(PermissionError, match="git недоступен"):
        F.gate("x", tmp_path, tmp_path / "log.md")


def test_seen_marker_in_git_dir_blocks_rerun_even_with_clean_log(repo):
    sha = R.config_sha256(repo)
    F.seen_file(repo).write_text(f"{sha} earlier attempt\n")
    with pytest.raises(PermissionError, match="уже оценивалась"):
        F.gate(sha, repo, repo / "log.md")


def test_config_hash_changes_with_committed_code(repo):
    before = R.config_sha256(repo)
    (repo / "src" / "match" / "a.py").write_text("x = 2\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-qam", "method change"], check=True)
    assert R.config_sha256(repo) != before


def test_config_hash_ignores_ui_code(repo):
    before = R.config_sha256(repo)
    (repo / "src" / "ui").mkdir()
    (repo / "src" / "ui" / "App.tsx").write_text("export {}\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "ui"], check=True)
    assert R.config_sha256(repo) == before


def test_config_hash_ignores_line_endings(repo):
    """Same config checked out with CRLF (Windows, core.autocrlf) and LF (Linux) → same sha256."""
    paths = [repo / rel for rel in R.CONFIG_FILES]
    for p in paths:
        p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n"))
    lf = R.config_sha256(repo)
    for p in paths:
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    assert R.config_sha256(repo) == lf
