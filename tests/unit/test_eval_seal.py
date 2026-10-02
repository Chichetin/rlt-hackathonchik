"""The lock on the holdout period: only `make eval-final` for the frozen config reads it."""

import re
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


def test_gate_refuses_a_tokenizer_override_but_not_an_empty_one(repo, monkeypatch):
    """BM25_TOKENIZER replaces the tokenizer of configs/search.yaml, so eval-final refuses it. An empty value is
    «not set» for search() too (src/settings.py), so it no longer blocks the gate (#297)."""
    monkeypatch.setenv("BM25_TOKENIZER", "snowball")
    with pytest.raises(PermissionError, match="BM25_TOKENIZER"):
        F.gate(R.config_sha256(repo), repo, repo / "log.md")
    monkeypatch.setenv("BM25_TOKENIZER", "")
    assert F.gate(R.config_sha256(repo), repo, repo / "log.md") == R.config_sha256(repo)


def test_gate_passes_frozen_clean_config(repo):
    assert F.gate(R.config_sha256(repo), repo, repo / "log.md") == R.config_sha256(repo)


def test_config_hash_changes_with_weights(repo):
    before = R.config_sha256(repo)
    p = repo / "configs" / "search.yaml"
    text = p.read_text(encoding="utf-8")
    changed = re.sub(r"product_fit: [0-9.]+", "product_fit: 0.5", text, count=1)
    assert changed != text
    p.write_text(changed, encoding="utf-8")
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


def _holdout_case(i: int):
    from src.eval.split import EvalCase

    return EvalCase.model_validate({"query": {"query_id": f"lot-{i}", "text": "x"}, "relevant_inns": [],
                                    "date": "2025-11-01"})


def test_holdout_sample_is_fixed_by_seed_not_by_order():
    """The sample is the n lowest sha256(seed:query_id): the same cases whatever the file order, other with
    another seed; no sample size (null) or a size above the holdout — every case (#145)."""
    cases = [_holdout_case(i) for i in range(50)]
    a = F.holdout_sample(cases, 7, seed=1)
    assert len(a) == 7 and [c.query.query_id for c in a] == sorted(c.query.query_id for c in a)
    assert [c.query.query_id for c in F.holdout_sample(cases[::-1], 7, seed=1)] == [c.query.query_id for c in a]
    assert F.holdout_sample(cases, 7, seed=2) != a
    assert F.holdout_sample(cases, None, seed=1) == cases and F.holdout_sample(cases, 500, seed=1) == cases


def test_final_main_evaluates_the_configured_sample(tmp_path, monkeypatch):
    """The whole holdout (88 848 cases, 0.25–0.30 s each) is a 6–7 h one-shot run — it does not fit the window
    before code freeze; make eval-final scores the sample fixed in configs/split.yaml and writes a compact
    result under the 5 MB commit guard (#145, docs/REVIEW.md)."""
    seen: list[int] = []
    monkeypatch.setattr(F, "gate", lambda confirm: "a" * 64)
    monkeypatch.setattr(F, "seen_file", lambda root: tmp_path / "seen")
    monkeypatch.setattr(F, "FINAL_LOG", tmp_path / "final-log.md")
    monkeypatch.setattr(F, "split_config", lambda: {**split_config(), "holdout_sample": 4, "holdout_seed": 9})
    monkeypatch.setattr(F, "load_cases", lambda part, cfg: [_holdout_case(i) for i in range(20)])
    monkeypatch.setattr(R, "RESULTS", tmp_path)
    monkeypatch.setattr(F, "ROOT", tmp_path)
    monkeypatch.setattr(R, "evaluate", lambda cases, cfg: seen.append(len(cases)) or {
        "n_queries": len(cases), "metrics": {"recall@10": {"value": 0.5, "ci95": [0.4, 0.6]}},
        "per_query": [{"query_id": c.query.query_id, "recall@10": 0.5} for c in cases]})
    monkeypatch.setattr(R, "git", lambda *a, **k: "")
    assert F.main() == 0
    assert seen == [4]
    rec = (tmp_path / f"final-{'a' * 8}.json").read_text(encoding="utf-8")
    assert '"sample": {' in rec and '"of": 20' in rec and '"seed": 9' in rec
    assert sum(line.startswith('    {"query_id"') for line in rec.splitlines()) == 4  # one line per case
    assert "| 4 из 20 (seed 9) |" in (tmp_path / "final-log.md").read_text(encoding="utf-8")  # a re-seed shows


@pytest.mark.parametrize("n", [-5, 0.5, "3000", True])
def test_final_refuses_a_broken_sample_before_the_marker(tmp_path, monkeypatch, n):
    """A typo in holdout_sample stops eval-final BEFORE the «already run» marker: the config is not burned."""
    monkeypatch.setattr(F, "gate", lambda confirm: "b" * 64)
    monkeypatch.setattr(F, "seen_file", lambda root: tmp_path / "seen")
    monkeypatch.setattr(F, "split_config", lambda: {**split_config(), "holdout_sample": n})
    assert F.main() == 2
    assert not (tmp_path / "seen").exists()


def test_the_cutoff_reader_is_in_the_hashed_method_code():
    """configs/split.yaml is read by src/ingest/split_config.py (#294): changing it changes the eval-final hash."""
    import src.ingest.split_config as sc

    rel = Path(sc.__file__).resolve().relative_to(R.ROOT).as_posix()
    assert any(rel == p or rel.startswith(p + "/") for p in R.METHOD_PATHS), rel
    assert split_config is sc.split_config  # the eval tools read the very same function


def test_tokenizer_override_changes_the_signature(repo, monkeypatch):
    """#440: BM25_TOKENIZER changes the method, so it changes the signature; without it the hash is as before."""
    monkeypatch.delenv("BM25_TOKENIZER", raising=False)
    plain = R.config_sha256(repo)
    monkeypatch.setenv("BM25_TOKENIZER", "snowball")
    snowball = R.config_sha256(repo)
    monkeypatch.setenv("BM25_TOKENIZER", "prefix5")
    assert len({plain, snowball, R.config_sha256(repo)}) == 3
    monkeypatch.delenv("BM25_TOKENIZER")
    assert R.config_sha256(repo) == plain


def test_every_module_the_evaluation_imports_is_in_the_hashed_method_code():
    """A module the backtest runs but the eval-final hash does not cover lets a changed method pass the gate under
    the old sha (#448: src/settings.py and src/regions.py were outside). Walk the import graph from src.eval.run —
    all imports, lazy ones too (scripts/check_layers.py) — and require every reached file under METHOD_PATHS."""
    import importlib.util
    import sys

    name = "check_layers_for_seal"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "check_layers.py")
    assert spec and spec.loader
    layers = importlib.util.module_from_spec(spec)
    sys.modules[name] = layers  # @dataclass looks its module up here
    try:
        spec.loader.exec_module(layers)
        modules = layers.find_modules(ROOT)
        graph: dict[str, set[str]] = {}
        for e in layers.collect_edges(ROOT, modules):
            graph.setdefault(e.src, set()).add(e.dst)
    finally:
        del sys.modules[name]
    reached, todo = set(), ["src.eval.run", "src.eval.final"]  # make eval and make eval-final
    while todo:
        m = todo.pop()
        if m not in reached:
            reached.add(m)
            todo.extend(graph.get(m, ()))
    files = {modules[m].relative_to(ROOT).as_posix() for m in reached}
    outside = sorted(f for f in files if f != "src/__init__.py"
                     and not any(f == p or f.startswith(p + "/") for p in R.METHOD_PATHS))
    assert not outside, f"eval импортирует код вне METHOD_PATHS: {outside}"
    assert {"src/settings.py", "src/regions.py", "src/match/search.py"} <= files  # the walk really went through


def test_ranker_model_is_part_of_the_signature_and_of_the_clean_tree(repo):
    """The product ranks with a learned model file (D-103): retraining it under the same name is another method, so it
    changes the hash, and an uncommitted edit is refused like an edit of the code (#451)."""
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    model = repo / "models" / "ranker" / "m.txt"
    model.parent.mkdir(parents=True)
    model.write_text("tree 1\n", encoding="utf-8")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "model"], check=True)
    before = R.config_sha256(repo)
    model.write_text("tree 2\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="не чистое"):
        F.gate(before, repo, repo / "log.md")
    subprocess.run([*git, "commit", "-qam", "retrained"], check=True)
    assert R.config_sha256(repo) != before


def test_the_configured_ranker_model_is_tracked_and_hashed():
    """The model `configs/search.yaml` names must sit under METHOD_PATHS and be committed: an untracked file would
    not enter `git ls-tree`, and the signature would not name the method (#451)."""
    import yaml

    spec = yaml.safe_load((ROOT / "configs" / "search.yaml").read_text(encoding="utf-8")).get("ranker")
    if not spec:
        pytest.skip("ранжировщик выключен (ranker: null) — модели в методе нет")
    if subprocess.run(["git", "-C", str(ROOT), "rev-parse"], capture_output=True).returncode != 0:
        pytest.skip("не git-копия (архив) — проверять нечего")
    rel = spec["model"]
    assert any(rel == p or rel.startswith(p + "/") for p in R.METHOD_PATHS), rel
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", rel], capture_output=True)
    assert tracked.returncode == 0, f"{rel} не в git"


def test_dev_results_are_dirty_on_an_uncommitted_ranker_model(repo):
    """A dev result records `dirty` for the very paths the eval-final gate freezes, the ranker models too: a retrained
    model left uncommitted made `make eval` say dirty: false, and `make parity` did not warn (#455)."""
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    model = repo / "models" / "ranker" / "m.txt"
    model.parent.mkdir(parents=True)
    model.write_text("tree 1\n", encoding="utf-8")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "model"], check=True)
    assert R.tree_dirty() is False
    model.write_text("tree 2\n", encoding="utf-8")
    assert R.tree_dirty() is True
    assert R.result_record("x", "dev", {}, {})["dirty"] is True
    assert F.FROZEN_PATHS == R.FROZEN_PATHS  # one list for the gate and for the results



def test_dirty_is_computed_in_one_place():
    """A new eval tool copied from an old one would compute `dirty` over its own paths again (#455): `git status
    --porcelain` stays in run.tree_dirty, the eval-final gate and fns_coverage (whole repo, tracked files on purpose,
    #396 — it already sees a modified tracked model)."""
    allowed = {"src/eval/run.py", "src/eval/final.py", "src/eval/fns_coverage.py"}
    found = {p.relative_to(ROOT).as_posix() for d in ("src", "scripts") for p in (ROOT / d).rglob("*.py")
             if "node_modules" not in p.parts and '"--porcelain"' in p.read_text(encoding="utf-8")}
    assert found <= allowed, f"dirty считается вне run.tree_dirty: {sorted(found - allowed)}"
