"""Rolling-origin eval (#79): several time folds, each with its own cutoff, never into the holdout.

Fixture tests/fixtures/eval_cases (README.md there). Two folds on it:
  fold 1: cutoff 2025-07-01, cases of [07-01, 08-21): lots 10, 12, 13 (11 and 14 and 15 are excluded);
  fold 2: cutoff 2025-08-21, cases of [08-21, 10-01): lot 16 (lot 17 of 08-20 is excluded: ст.93).
Lots 20 (10-01) and 21 (11-01) are the holdout: never a case of any fold. Supplier 7802010038 (E4) first
takes part on 08-10, so fold 1 must not know it and fold 2 must.
"""

from __future__ import annotations

import copy
import os
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import duckdb
import pytest

from src.contracts import Query
from src.eval import baselines as B
from src.eval import build_cases as BC
from src.eval import folds as F
from src.eval.split import EvalCase, split_config
from src.ingest.build import build
from src.match import index
from src.match.store import clear_stores

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "eval_cases"
CUTOFF, MID, HOLDOUT_FROM = date(2025, 7, 1), date(2025, 8, 21), date(2025, 10, 1)
SPLIT = {"cutoff": CUTOFF.isoformat(), "holdout_from": HOLDOUT_FROM.isoformat()}
FOLDS_CFG = {"cutoffs": [CUTOFF.isoformat(), MID.isoformat()], "sample": 100, "seed": 1}
E1, E2, E3, E4 = "7802010006", "7802010013", "7802010020", "7802010038"
POP = {"method": "pop", "k": 20, "okpd_level": 4, "count": "participations", "fill": True}


@pytest.fixture(scope="module")
def processed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("folds") / "processed"
    build(FIXTURE, out, as_of=None, cutoff=CUTOFF)
    return out


@pytest.fixture
def env(processed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(processed))
    monkeypatch.setenv("DATA_STORE", "parquet")
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "indexes"))
    index.clear_memory_cache()
    clear_stores()
    B.clear_cache()
    yield processed
    B.clear_cache()
    index.clear_memory_cache()
    clear_stores()


@pytest.fixture
def folds() -> list[F.Fold]:
    return F.make_folds(FOLDS_CFG, SPLIT)


@pytest.fixture
def fold_cases(processed: Path, folds: list[F.Fold]) -> dict[int, list[EvalCase]]:
    return {k: v[0] for k, v in F.build_fold_cases(processed, folds, SPLIT, FOLDS_CFG["sample"], FOLDS_CFG["seed"]
                                                   ).items()}


def ids(cases: list[EvalCase]) -> set[str]:
    return {c.query.query_id for c in cases}


# --- windows ---------------------------------------------------------------------------------------------

def test_windows_are_contiguous_and_the_last_ends_at_holdout(folds: list[F.Fold]) -> None:
    assert [(f.index, f.cutoff, f.until) for f in folds] == [(1, CUTOFF, MID), (2, MID, HOLDOUT_FROM)]


def test_product_config_folds_never_reach_the_holdout() -> None:
    """configs/folds.yaml as shipped: rolling cutoffs before holdout_from, the last fold is the current dev."""
    split = split_config()
    cfg = F.load_folds_config()
    folds = F.make_folds(cfg, split)
    holdout_from = date.fromisoformat(str(split["holdout_from"]))
    assert len(folds) >= 3 and all(f.until <= holdout_from for f in folds)
    assert folds[-1].cutoff == date.fromisoformat(str(split["cutoff"])) and folds[-1].until == holdout_from
    assert (cfg["sample"], cfg["seed"]) == (BC.SAMPLE_N, BC.SEED)  # same sample rule as D-032: fold 3 = dev.jsonl


@pytest.mark.parametrize("cutoffs", [["2025-07-01", "2025-10-01"], ["2025-07-01", "2026-01-01"], ["2025-10-01"]])
def test_a_fold_starting_in_the_holdout_is_refused(cutoffs: list[str]) -> None:
    with pytest.raises(ValueError, match="holdout"):
        F.make_folds({**FOLDS_CFG, "cutoffs": cutoffs}, SPLIT)


@pytest.mark.parametrize("cutoffs", [[], ["2025-08-21", "2025-07-01"], ["2025-07-01", "2025-07-01"]])
def test_cutoffs_must_be_a_strictly_increasing_list(cutoffs: list[str]) -> None:
    with pytest.raises(ValueError, match="cutoffs"):
        F.make_folds({**FOLDS_CFG, "cutoffs": cutoffs}, SPLIT)


# --- cases -----------------------------------------------------------------------------------------------

def test_cases_of_a_fold_lie_in_its_window_and_see_only_before_its_cutoff(
        folds: list[F.Fold], fold_cases: dict[int, list[EvalCase]]) -> None:
    assert ids(fold_cases[1]) == {"lot-10", "lot-12", "lot-13"}
    assert ids(fold_cases[2]) == {"lot-16"}
    for f in folds:
        assert fold_cases[f.index]
        assert all(f.cutoff <= c.date < f.until and c.query.as_of == f.cutoff for c in fold_cases[f.index])
    everything = ids(fold_cases[1]) | ids(fold_cases[2])
    assert not everything & {"lot-20", "lot-21"}  # holdout
    assert "lot-1" not in everything  # history before the first cutoff


def test_fold_window_is_half_open_and_holdout_lots_are_never_cases(processed: Path) -> None:
    every_lot = BC.read_lots(processed, CUTOFF)  # includes lots 20 and 21 of the holdout
    assert {lot.lot_id for lot in every_lot} >= {20, 21}
    narrow = F.Fold(1, CUTOFF, date(2025, 8, 12))  # lot 12 (08-11) in, lot 13 (08-12) is the next window's
    assert ids(F.fold_cases(every_lot, narrow, 100, 1)[0]) == {"lot-10", "lot-12"}
    last = F.Fold(2, MID, HOLDOUT_FROM)  # lot 20 is dated 10-01: not in [08-21, 10-01)
    assert ids(F.fold_cases(every_lot, last, 100, 1)[0]) == {"lot-16"}


def test_folds_do_not_share_cases(fold_cases: dict[int, list[EvalCase]]) -> None:
    assert not ids(fold_cases[1]) & ids(fold_cases[2])


def test_last_fold_sample_is_the_dev_sample(processed: Path, tmp_path: Path) -> None:
    """The fold of the split cutoff is the current dev: the same cases as build_cases (same stratified sample)."""
    cfg = {**SPLIT, "cases": {p: str(tmp_path / f"{p}.jsonl") for p in ("dev", "dev_full", "real", "holdout")}}
    BC.build_all(processed, cfg, sample_n=2, seed=1, real_per_channel=1)
    dev = {c.query.query_id for c in map(EvalCase.model_validate_json, open(cfg["cases"]["dev"], encoding="utf-8"))}
    last = F.make_folds({"cutoffs": [CUTOFF.isoformat()]}, SPLIT)
    got, eligible = F.build_fold_cases(processed, last, SPLIT, 2, 1)[1]
    assert len(dev) == 2 and eligible == 4 and ids(got) == dev


def test_sample_is_stratified_and_capped(processed: Path, folds: list[F.Fold]) -> None:
    got, eligible = F.build_fold_cases(processed, folds, SPLIT, 2, 1)[1]
    assert eligible == 3 and len(got) == 2
    assert F.build_fold_cases(processed, folds, SPLIT, 2, 1)[1][0] == got  # deterministic


def _case(lot: int, day: date, as_of: date | None) -> EvalCase:
    return EvalCase(query=Query(query_id=f"lot-{lot}", text="бумага", as_of=as_of), relevant_inns=[E1], date=day)


@pytest.mark.parametrize("bad", [
    _case(1, date(2025, 6, 30), CUTOFF),        # before the window: a lot the features already contain
    _case(2, MID, CUTOFF),                      # the next fold's window
    _case(3, HOLDOUT_FROM, CUTOFF),             # holdout
    _case(4, date(2025, 7, 10), MID),           # features past the cutoff of the fold
    _case(5, date(2025, 7, 10), None),          # as_of is not pinned
])
def test_check_fold_cases_catches_a_leak(folds: list[F.Fold], bad: EvalCase) -> None:
    F.check_fold_cases(folds[0], [_case(9, date(2025, 7, 10), CUTOFF)], HOLDOUT_FROM)  # the honest one passes
    with pytest.raises(ValueError, match="fold 1"):
        F.check_fold_cases(folds[0], [bad], HOLDOUT_FROM)


def test_check_fold_cases_refuses_a_window_past_the_holdout() -> None:
    wide = F.Fold(1, CUTOFF, date(2025, 12, 1))
    with pytest.raises(ValueError, match="holdout"):
        F.check_fold_cases(wide, [_case(1, date(2025, 11, 1), CUTOFF)], HOLDOUT_FROM)


# --- features: history strictly before the cutoff of the fold -------------------------------------------

def test_history_grows_with_the_fold_and_never_sees_its_own_period(
        env: Path, folds: list[F.Fold], fold_cases: dict[int, list[EvalCase]]) -> None:
    body = F.run_folds(POP, folds, fold_cases, HOLDOUT_FROM, ranked_limit=100)
    by_fold = {k: {q["query_id"]: q["ranked"] for q in body["per_query"] if q["fold"] == k} for k in (1, 2)}
    # fold 1 (cutoff 07-01): only lot 1 is history; E4 first takes part on 08-10 — in the fold's own window
    assert by_fold[1]["lot-10"] == [E1, E2] and all(E4 not in r for r in by_fold[1].values())
    # fold 2 (cutoff 08-21): lots of fold 1's window are history now, lot 17 (08-20) too, E4 is known
    assert by_fold[2]["lot-16"] == [E2, E4, E1, E3]
    assert [f["history"]["lots"] for f in body["folds"]] == [1, 7]  # lots with participants before each cutoff


def test_history_boundary_is_strict(env: Path) -> None:
    assert F.history_stats(env, date(2025, 8, 20))["to"] == date(2025, 8, 14)  # lot 17 (08-20) is not yet history
    assert F.history_stats(env, date(2025, 8, 21))["to"] == date(2025, 8, 20)


def test_run_folds_refuses_cases_of_another_fold(env: Path, folds: list[F.Fold],
                                                 fold_cases: dict[int, list[EvalCase]]) -> None:
    with pytest.raises(ValueError, match="fold 1"):
        F.run_folds(POP, folds, {1: fold_cases[2], 2: fold_cases[2]}, HOLDOUT_FROM)


def test_an_empty_fold_is_an_error_not_a_zero(env: Path, folds: list[F.Fold],
                                              fold_cases: dict[int, list[EvalCase]]) -> None:
    with pytest.raises(ValueError, match="fold 2: .* нет кейсов"):
        F.run_folds(POP, folds, {1: fold_cases[1], 2: []}, HOLDOUT_FROM)


def test_search_method_runs_on_the_supplier_aggregates_of_each_fold(
        env: Path, folds: list[F.Fold], fold_cases: dict[int, list[EvalCase]]) -> None:
    from src.match import search as search_mod

    cfg = {**search_mod.load_config(), "k": 20}
    assert not (env / "suppliers_2025-08-21.parquet").exists()
    body = F.run_folds(cfg, folds, fold_cases, HOLDOUT_FROM, ranked_limit=100)
    assert (env / "suppliers_2025-08-21.parquet").exists()
    assert [q["query_id"] for q in body["per_query"]] == ["lot-10", "lot-12", "lot-13", "lot-16"]
    one = next(q for q in body["per_query"] if q["query_id"] == "lot-10")
    assert one["recall@10"] > 0 and E4 not in one["ranked"]


# --- supplier aggregates per fold -------------------------------------------------------------------------

def test_supplier_aggregates_of_a_fold_equal_the_ingest_ones(processed: Path, tmp_path: Path) -> None:
    day = date(2025, 8, 12)  # lot 13 is dated 08-12: strictly before means it is not counted
    out = F.ensure_suppliers(processed, day)
    ref = tmp_path / "ref"
    build(FIXTURE, ref, as_of=None, cutoff=day)
    a, b = (f"SELECT * FROM read_parquet('{p.as_posix()}')" for p in (out, ref / "suppliers_2025-08-12.parquet"))
    assert duckdb.sql(f"({a}) EXCEPT ({b})").fetchall() == [] and duckdb.sql(f"({b}) EXCEPT ({a})").fetchall() == []
    assert duckdb.sql(f"SELECT max(last_seen) FROM ({a})").fetchone()[0] == date(2025, 8, 11)  # type: ignore[index]


def test_ensure_suppliers_is_idempotent_and_rebuilds_a_stale_file(processed: Path) -> None:
    path = F.ensure_suppliers(processed, date(2025, 8, 15))
    first = path.stat().st_mtime_ns
    time.sleep(0.01)
    assert F.ensure_suppliers(processed, date(2025, 8, 15)).stat().st_mtime_ns == first
    manifest = processed / "manifest.json"
    old = manifest.read_text(encoding="utf-8")
    try:  # the ingest was rebuilt: the stamp of the file no longer matches
        manifest.write_text(old.replace('"built_at": "', '"built_at": "9'), encoding="utf-8")
        assert F.ensure_suppliers(processed, date(2025, 8, 15)).stat().st_mtime_ns > first
    finally:
        manifest.write_text(old, encoding="utf-8")


def test_ensure_suppliers_keeps_the_file_of_the_ingest(processed: Path) -> None:
    """suppliers_<split cutoff> comes from `src.ingest.build` (no stamp): left as it is."""
    path = processed / "suppliers_2025-07-01.parquet"
    before = (path.stat().st_mtime_ns, os.stat(path).st_size)
    assert F.ensure_suppliers(processed, CUTOFF) == path
    assert (path.stat().st_mtime_ns, os.stat(path).st_size) == before


# --- aggregation and comparison ---------------------------------------------------------------------------

def row(qid: str, fold: int, r: float, **extra: object) -> dict:
    return {"query_id": qid, "fold": fold, "recall@10": r, "mrr": r, "ndcg@10": r, "channel": "ЭМ", **extra}


def test_macro_ci_degenerates_to_the_plain_bootstrap_for_one_fold() -> None:
    from src.eval.metrics import bootstrap_ci

    vals = [0.0, 1.0, 0.5, 0.25, 1.0, 0.0]
    assert F.macro_bootstrap_ci([vals]) == pytest.approx(bootstrap_ci(vals))


def test_macro_ci_weights_folds_equally_and_is_zero_width_on_constants() -> None:
    assert F.macro_bootstrap_ci([[1.0] * 5, [0.0] * 50]) == (0.5, 0.5)  # not the pooled 5/55
    lo, hi = F.macro_bootstrap_ci([[0.0, 1.0] * 20, [0.0, 1.0] * 20])
    assert lo < 0.5 < hi and 0.0 < lo and hi < 1.0


def test_summary_is_the_mean_of_fold_means_with_spread() -> None:
    rows = {1: [row("a", 1, 1.0)] * 5, 2: [row("b", 2, 0.0)] * 50, 3: [row("c", 3, 0.5)] * 10}
    s = F.summarize(rows)
    assert s["metrics"]["recall@10"]["value"] == pytest.approx(0.5)  # (1 + 0 + 0.5) / 3, not pooled
    sp = s["spread"]["recall@10"]
    assert (sp["min"], sp["max"]) == (0.0, 1.0) and sp["std"] == pytest.approx(0.5)
    assert sp["per_fold"] == pytest.approx([1.0, 0.0, 0.5])


def _record(exp: str, vals: dict[int, list[float]]) -> dict:
    rows = [row(f"{k}-{i}", k, v, winner_new=bool(i % 2)) for k, vs in vals.items() for i, v in enumerate(vs)]
    return {"exp_id": exp, "part": "folds", "per_query": rows}


def test_compare_gives_a_paired_delta_per_fold_and_a_macro() -> None:
    a = _record("A", {1: [1.0, 1.0, 1.0, 1.0], 2: [0.0, 1.0, 0.0, 1.0]})
    b = _record("B", {1: [0.0, 0.0, 0.0, 0.0], 2: [0.0, 1.0, 0.0, 1.0]})
    out = F.compare_folds(a, b)
    assert out["folds"][1]["all"]["metrics"]["recall@10"]["delta"] == pytest.approx(1.0)
    assert out["folds"][2]["all"]["metrics"]["recall@10"]["delta"] == pytest.approx(0.0)
    m = out["macro"]["all"]["metrics"]["recall@10"]
    assert m["delta"] == pytest.approx(0.5) and m["folds_better"] == 1 and m["n_folds"] == 2
    assert 0.0 <= m["ci95"][0] <= m["delta"] <= m["ci95"][1]  # the interval is of A - B, not B - A
    assert "winner=new" in out["macro"]  # slices are compared per fold too
    with pytest.raises(ValueError, match="разные"):
        F.compare_folds(a, _record("C", {1: [1.0], 2: [0.0]}))


# --- record -----------------------------------------------------------------------------------------------

def test_record_has_a_block_per_fold_and_the_aggregate(
        env: Path, folds: list[F.Fold], fold_cases: dict[int, list[EvalCase]]) -> None:
    body = F.run_folds(POP, folds, fold_cases, HOLDOUT_FROM)
    assert body["n_queries"] == 4 and set(body["metrics"]) == {"recall@10", "mrr", "ndcg@10"}
    f1, f2 = body["folds"]
    assert (f1["fold"], f1["cutoff"], f1["until"], f1["n_queries"], f1["eligible"]) == (1, "2025-07-01",
                                                                                         "2025-08-21", 3, 3)
    assert (f2["fold"], f2["cutoff"], f2["until"], f2["n_queries"]) == (2, "2025-08-21", "2025-10-01", 1)
    assert f1["history"] == {"lots": 1, "from": "2025-03-01", "to": "2025-03-01"}
    assert f1["metrics"]["recall@10"]["ci95"] and "channel=ЭМ" in f1["slices"]
    # lot 13 (АИС ГЗ): its winner E3 has no history before 07-01 — cold start; lots 10 and 12 have
    assert f1["cold_start_share"] == pytest.approx(1 / 3, abs=1e-4) and f2["cold_start_share"] == 0.0
    assert "ranked" not in body["per_query"][0]  # a 3-fold file stays small: per-case metrics only
    assert body["protocol"]["holdout_from"] == "2025-10-01" and body["protocol"]["cutoffs"] == [
        "2025-07-01", "2025-08-21"]
    assert copy.deepcopy(body["protocol"]) == body["protocol"]


def test_run_py_part_folds_writes_the_record(env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from src.eval import run as R

    cfg = tmp_path / "folds.yaml"
    cfg.write_text(f'cutoffs: ["{CUTOFF}", "{MID}"]\nsample: 100\nseed: 1\n', encoding="utf-8")
    monkeypatch.setattr(F, "FOLDS_CONFIG", cfg)
    monkeypatch.setattr(F, "split_config", lambda: SPLIT)
    monkeypatch.setattr(R, "RESULTS", tmp_path / "results")
    assert R.main(["--exp", "RO-test", "--part", "folds", "--baseline", "B-pop"]) == 0
    rec = json.loads((tmp_path / "results" / "RO-test.json").read_text(encoding="utf-8"))
    assert rec["part"] == "folds" and rec["config"]["baseline"] == "B-pop" and rec["store"] == "parquet"
    assert [f["n_queries"] for f in rec["folds"]] == [3, 1] and rec["protocol"]["sample"] == 100
    assert {q["fold"] for q in rec["per_query"]} == {1, 2} and rec["metrics"]["recall@10"]["ci95"]
    assert "mean" in capsys.readouterr().out
    # the aggregate is what the compare command reads
    (tmp_path / "results" / "RO-test2.json").write_text(json.dumps(rec), encoding="utf-8")
    assert F.main(["compare", "RO-test", "RO-test2"]) == 0
    assert "mean Δ" in capsys.readouterr().out
    assert F.main(["compare", "RO-test", "no-such"]) == 2
