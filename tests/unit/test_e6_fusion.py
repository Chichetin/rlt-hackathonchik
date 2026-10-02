"""Fixed offline E6 fusion: no label-driven retrieval, no dev/holdout or lineage mixing."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date
from unittest.mock import Mock

import pytest

from scripts import e6_fusion as F
from src.contracts import Query
from src.eval.split import EvalCase

CUTOFF = date(2025, 7, 1)
DEV_END = date(2025, 10, 1)


def case(**query_changes) -> EvalCase:
    query = Query(query_id="lot-6", text="Бумага", okpd2=["1712"], customer_inn="buyer", as_of=CUTOFF)
    return EvalCase(query=query.model_copy(update=query_changes), date=date(2025, 8, 1),
                    relevant_inns=["c"], grades={"c": 2})


def records() -> tuple[dict, dict]:
    text = {
        "exp_id": "E6-base", "part": "dev", "n_queries": 1,
        "commit": "frozen-commit", "config_sha256": "frozen-sha", "tokenizer_version": "prefix5-v1",
        "store": "parquet", "config": {"k": 20, "retriever": "bm25", "tokenizer": "prefix5",
                                        "ranker": {"kind": "lgbm", "model": "frozen-model.txt"},
                                        "candidates": {"customer": 0, "okpd2": 0}},
        "candidate_inns": ["b", "a"],
        "per_query": [{"query_id": "lot-6", "candidate_ids": [0, 1], "n_candidates": 2,
                       "ranked": ["a", "b"], "winner_new": False}],
    }
    pool = copy.deepcopy(text)
    pool["exp_id"] = "E6-cust-untrained"
    pool["config"]["candidates"]["customer"] = 30
    pool["candidate_inns"].append("c")
    pool["per_query"][0].update(candidate_ids=[0, 1, 2], n_candidates=3, ranked=["c", "b", "a"])
    return text, pool


def store(*inns: str) -> Mock:
    history = Mock()
    history.name = "parquet"
    history.customer_suppliers.return_value = [(inn, 1, 1, 1, "Бумага", date(2024, 1, 1)) for inn in inns]
    return history


def test_fusion_deduplicates_sources_and_intersects_before_assigning_ranks():
    assert F.fuse(["b", "b", "a"], ["a", "a"], ["a", "b", "a", "c"]) == ["a", "b", "c"]
    # The excluded supplier neither enters the answer nor shifts the eligible customer's rank.
    assert F.fuse(["b"], ["oracle", "a"], ["a", "b"]) == ["a", "b"]
    assert F.fuse([], [], ["b", "a", "b"]) == ["a", "b"]
    assert F.fuse(["a"], ["b"], []) == []


def test_relevant_and_winner_labels_do_not_change_order_or_eligible_suppliers():
    text, pool = records()
    history = store("oracle", "c", "a", "c")
    original = F.replay(text, pool, [case()], CUTOFF, DEV_END, history)
    changed = case().model_copy(update={"relevant_inns": ["a", "oracle"], "grades": {"a": 2, "oracle": 2}})
    alternate = F.replay(text, pool, [changed], CUTOFF, DEV_END, history)

    assert original["per_query"][0]["ranked"] == alternate["per_query"][0]["ranked"] == ["a", "b", "c"]
    assert original["metrics"]["winner_mrr"]["value"] == pytest.approx(1 / 3)
    assert alternate["metrics"]["winner_mrr"]["value"] == 1
    assert original["candidate_inns"] == pool["candidate_inns"]
    assert original["per_query"][0]["candidate_ids"] == pool["per_query"][0]["candidate_ids"]
    history.customer_suppliers.assert_called_with("buyer", ["17.12"], CUTOFF, 30, 6)


@pytest.mark.parametrize("changes", [{}, {"customer_inn": None}, {"okpd2": []}])
def test_missing_history_uses_base_trace_order_not_saved_model_order(changes):
    text, pool = records()
    history = store()
    body = F.replay(text, pool, [case(**changes)], CUTOFF, DEV_END, history)
    assert body["per_query"][0]["ranked"] == ["b", "a", "c"]
    if changes:
        history.customer_suppliers.assert_not_called()


def test_ranking_truncates_at_twenty_but_retains_complete_candidate_trace():
    text, pool = records()
    for record in (text, pool):
        record["candidate_inns"] = [f"supplier-{i:02}" for i in range(25)]
        record["per_query"][0].update(candidate_ids=list(range(25)), n_candidates=25)
    body = F.replay(text, pool, [case()], CUTOFF, DEV_END, store())
    row = body["per_query"][0]
    assert row["ranked"] == text["candidate_inns"][:20]
    assert row["candidate_ids"] == list(range(25)) and row["n_candidates"] == 25


@pytest.mark.parametrize("bad", ["case_set", "duplicate_case", "lineage", "method", "k", "pool",
                                 "missing_trace", "truncated_trace", "negative_index", "holdout"])
def test_incompatible_source_results_are_rejected_before_history_is_read(bad):
    text, pool = records()
    if bad == "case_set":
        pool["per_query"][0]["query_id"] = "lot-7"
    elif bad == "duplicate_case":
        pool["per_query"].append(copy.deepcopy(pool["per_query"][0]))
        pool["n_queries"] = 2
    elif bad == "lineage":
        pool["commit"] = "different"
    elif bad == "method":
        pool["config"]["ranker"]["model"] = "retrained.txt"
    elif bad == "k":
        pool["config"]["k"] = 10
    elif bad == "pool":
        pool["config"]["candidates"]["okpd2"] = 20
    elif bad == "missing_trace":
        del text["per_query"][0]["candidate_ids"]
    elif bad == "truncated_trace":
        text["per_query"][0]["n_candidates"] = 200
    elif bad == "negative_index":
        text["per_query"][0]["candidate_ids"][0] = -1
    else:
        text["part"] = pool["part"] = "holdout"
    history = store()
    with pytest.raises(ValueError):
        F.replay(text, pool, [case()], CUTOFF, DEV_END, history)
    history.customer_suppliers.assert_not_called()


@pytest.mark.parametrize("changes", [{"as_of": date(2025, 8, 1)}, {"as_of": date(2025, 6, 1)},
                                     {"query_id": "unknown"}, {"filters": {"region": "Москва"}}])
def test_replay_refuses_cases_from_another_history_window_or_case_set(changes):
    text, pool = records()
    with pytest.raises(ValueError):
        F.replay(text, pool, [case(**changes)], CUTOFF, DEV_END, store())


def test_cli_writes_standard_record_with_source_hashes_and_never_overwrites(tmp_path, monkeypatch):
    text, pool = records()
    text_path, pool_path = tmp_path / "text.json", tmp_path / "pool.json"
    for path, record in ((text_path, text), (pool_path, pool)):
        path.write_text(json.dumps(record), encoding="utf-8")
    dev = tmp_path / "dev.jsonl"
    dev.write_text(case().model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(F, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(F, "split_config", lambda: {"cutoff": str(CUTOFF), "holdout_from": str(DEV_END),
                                                    "cases": {"dev": str(dev)}})
    monkeypatch.setattr(F, "get_store", lambda: store("c", "a"))
    monkeypatch.setattr(F.R, "_store", lambda: "parquet")
    monkeypatch.setattr(F.R, "_safe_sha", lambda: "current-sha")
    monkeypatch.setattr(F.R, "git", lambda *args, **kwargs: "test-commit" if args[0] == "rev-parse" else "")
    args = ["--text-result", str(text_path), "--pool-result", str(pool_path),
            "--cutoff", str(CUTOFF), "--exp", "E6-fusion-synthetic"]

    assert F.main(args) == 0
    out = F.RESULTS / "E6-fusion-synthetic.json"
    saved = out.read_bytes()
    record = json.loads(saved)
    assert record["config"]["fusion"]["rank_constant"] == 60
    assert record["config"]["ranker"] is None and record["config"]["k"] == 20
    assert record["offline_replay"]["experimental"] is True
    text_hash = hashlib.sha256(text_path.read_bytes()).hexdigest()
    assert record["offline_replay"]["sources"]["text_result"]["sha256"] == text_hash
    assert record["candidate_inns"] == pool["candidate_inns"]
    assert set(F.METRICS) | set(F.WINNER_METRICS) <= record["metrics"].keys()
    assert F.main(args) == 2
    assert out.read_bytes() == saved


def test_cli_requires_explicit_cutoff():
    with pytest.raises(SystemExit):
        F.main(["--text-result", "text.json", "--pool-result", "pool.json", "--exp", "E6-fusion"])
