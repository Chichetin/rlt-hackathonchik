"""Latency measurement for the slide of item 8 (Q-22, #49): percentiles and the record on the ingest fixture."""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.contracts import Query
from src.eval import latency
from src.eval.split import EvalCase


def test_percentile_is_nearest_rank_of_measured_values():
    ms = [float(x) for x in range(1, 101)]  # 1..100
    assert latency.percentile(ms, 50) == 50.0 and latency.percentile(ms, 95) == 95.0
    assert latency.percentile(ms, 100) == 100.0 and latency.percentile([7.0], 95) == 7.0
    assert latency.percentile([3.0, 1.0, 2.0], 50) == 2.0
    with pytest.raises(ValueError):
        latency.percentile([], 50)


def test_latency_record_on_fixture(search_data, monkeypatch, tmp_path):
    texts = ["бумага офисная а4", "картридж", "ремонт", "вода питьевая", "бумага"] * 3
    cases = [EvalCase(query=Query(query_id=f"q{i}", text=t, as_of=date(2025, 7, 1)), date=date(2025, 8, 1),
                      relevant_inns=[]) for i, t in enumerate(texts)]
    monkeypatch.setattr(latency, "load_cases", lambda part, cfg: cases)
    monkeypatch.setattr(latency, "RESULTS", tmp_path)
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "c.sqlite"))
    assert latency.main(["--exp", "latency-test", "--n", "8"]) == 0
    rec = json.loads((tmp_path / "latency-test.json").read_text(encoding="utf-8"))
    assert rec["store"] == "parquet" and rec["retriever"] == "bm25" and rec["kind"] == "latency"
    assert rec["n_queries"] == 8 and len(rec["per_query"]) == 8  # the 5 warm-up queries are not counted
    assert [q["query_id"] for q in rec["per_query"]] == [f"q{i}" for i in range(5, 13)]
    lat = rec["latency_ms"]
    assert 0 < lat["p50"] <= lat["p95"] <= lat["max"]
    assert any(q["total"] > 0 for q in rec["per_query"])  # product mode (as_of=None) finds suppliers


def test_same_eval_reports_differing_queries():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "same_eval.py"
    spec = importlib.util.spec_from_file_location("same_eval", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    a = {"metrics": {"mrr": {"value": 0.5}},
         "per_query": [{"query_id": "q1", "mrr": 1.0}, {"query_id": "q2", "mrr": 0.0}]}
    assert mod.diff(a, json.loads(json.dumps(a))) == []
    b = {"metrics": {"mrr": {"value": 0.25}},
         "per_query": [{"query_id": "q1", "mrr": 0.5}, {"query_id": "q2", "mrr": 0.0}]}
    bad = mod.diff(a, b)
    assert len(bad) == 2 and bad[0].startswith("mrr") and bad[1].startswith("q1")
