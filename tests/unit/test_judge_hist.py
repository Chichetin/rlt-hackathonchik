"""Independent relevance check of the «С опытом» top-10 (#301): blind packets, import checks, paired precision."""

from __future__ import annotations

import json

import pytest

from src.eval import judge_hist as J

INFO = {i: {"lots_total": n, "history": [{"name": f"позиция {n}", "lots": n}]}
        for i, n in (("7801000001", 3), ("7801000002", 5), ("7801000003", 1))}


def test_packet_is_blind_and_covers_the_union():
    ranked = {"A": ["7801000001", "7801000002"], "B": ["7801000002", "7801000003"]}
    packet, rec = J.make_packet("q1", "бумага", ranked, INFO)
    text = json.dumps(packet, ensure_ascii=False)
    assert len(packet["candidates"]) == 3  # a supplier shown by both systems is judged once
    assert all(inn not in text for inn in INFO)  # no INN, system or rank in what the judge sees
    assert "A" not in packet and '"rank"' not in text and "system" not in text
    assert {rec["keys"][k] for k in rec["ranked"]["A"]} == {"7801000001", "7801000002"}
    assert {c["key"] for c in packet["candidates"]} == set(rec["keys"])


def test_packet_order_is_deterministic_but_not_the_rank_order():
    ranked = {"A": sorted(INFO)}
    first, _ = J.make_packet("q1", "x", ranked, INFO)
    again, _ = J.make_packet("q1", "x", ranked, INFO)
    assert first == again
    keys_by_rank = [J.make_packet(f"q{n}", "x", ranked, INFO)[1]["ranked"]["A"] for n in range(8)]
    assert len({tuple(k) for k in keys_by_rank}) > 1  # the keys do not follow the rank order across queries


def test_precision_skips_null_and_returns_none_when_nothing_is_labelled():
    assert J.precision({"c01": 1, "c02": 0, "c03": None}, ["c01", "c02", "c03"]) == 0.5
    assert J.precision({"c01": None}, ["c01"]) is None


def test_score_pairs_systems_by_query_and_ignores_unlabelled_queries():
    records = {
        "q1": {"ranked": {"A": ["c01", "c02"], "B": ["c03", "c04"]}},
        "q2": {"ranked": {"A": ["c01"], "B": ["c02"]}},
        "q3": {"ranked": {"A": ["c01"], "B": ["c02"]}},
    }
    labels = {"q1": {"c01": 1, "c02": 1, "c03": 0, "c04": 1},  # A 1.0, B 0.5
              "q2": {"c01": 1, "c02": 0},  # A 1.0, B 0.0
              "q3": {"c01": None, "c02": 1}}  # A has nothing labelled: the query does not count
    got = J.score_records(labels, records)
    assert got["n_queries"] == 2 and set(got["per_query"]) == {"q1", "q2"}
    assert got["precision_at_10"]["A"]["mean"] == 1.0 and got["precision_at_10"]["B"]["mean"] == 0.25
    assert got["paired"]["A - B"]["mean"] == 0.75 and got["null_labels"] == 1


def test_import_rejects_an_answer_that_does_not_match_the_packet(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    (packets / "q1.json").write_text(json.dumps({"query_id": "q1", "candidates": [{"key": "c01"}, {"key": "c02"}]}),
                                     encoding="utf-8")
    answer = tmp_path / "a.json"
    labels = tmp_path / "labels.json"
    answer.write_text(json.dumps({"query_id": "q1", "labels": {"c01": {"label": 1, "reason": "x"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="не совпадает"):
        J.import_answers([answer], "m", labels, packets)
    answer.write_text(json.dumps({"query_id": "q1", "labels": {"c01": {"label": 2}, "c02": {"label": 0}}}),
                      encoding="utf-8")
    with pytest.raises(ValueError, match="вне 0/1/null"):
        J.import_answers([answer], "m", labels, packets)
    answer.write_text(json.dumps({"query_id": "q1", "labels": {"c01": {"label": 1}, "c02": {"label": None}}}),
                      encoding="utf-8")
    J.import_answers([answer], "m", labels, packets)
    J.import_answers([answer], "m", labels, packets)  # idempotent
    assert json.loads(labels.read_text(encoding="utf-8"))["queries"] == {"q1": {"c01": 1, "c02": None}}
