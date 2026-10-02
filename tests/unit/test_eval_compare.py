"""E6's predeclared 90% paired bootstrap and full candidate-pool ceiling (#318)."""

from __future__ import annotations

import json

import pytest

from src.eval import compare as C
from src.eval.metrics import bootstrap_ci


def record(exp: str, rows: list[dict]) -> dict:
    return {"exp_id": exp, "part": "dev", "per_query": rows}


def test_paired_90_percent_uses_query_differences_and_preserves_95_default():
    # Deliberately reverse B: joining by row order would produce a different distribution.
    a = record("A", [{"query_id": str(i), "winner@10": float(i % 3 > 0)} for i in range(30)])
    b = record("B", [{"query_id": str(i), "winner@10": float(i % 4 > 0)} for i in reversed(range(30))])
    diffs = [float(i % 3 > 0) - float(i % 4 > 0) for i in range(30)]
    narrow = C.paired(a, b, alpha=0.1)["all"]["metrics"]["winner@10"]
    default = C.paired(a, b)["all"]["metrics"]["winner@10"]
    assert narrow["n"] == 30 and narrow["delta"] == pytest.approx(sum(diffs) / len(diffs))
    assert narrow["ci90"] == list(bootstrap_ci(diffs, alpha=0.1)) and "ci95" not in narrow
    assert default["ci95"] == list(bootstrap_ci(diffs)) and "ci90" not in default
    assert default["ci95"][0] <= narrow["ci90"][0] <= narrow["ci90"][1] <= default["ci95"][1]


def test_candidate_ceiling_is_paired_on_winner_cases_and_sliced():
    a = record("A", [
        {"query_id": "q1", "channel": "ЭМ", "winner_new": False, "candidate_winner_hit": 1.0},
        {"query_id": "q2", "channel": "АИС ГЗ", "winner_new": True, "candidate_winner_hit": 0.0},
        {"query_id": "q3", "channel": "ЭМ", "winner_new": None, "candidate_winner_hit": None},
    ])
    b = record("B", [{**r, "candidate_winner_hit": 0.0 if i < 2 else None}
                     for i, r in enumerate(a["per_query"])])
    table = C.paired(a, b, alpha=0.1)
    assert table["all"]["metrics"]["candidate_winner_hit"]["delta"] == 0.5
    assert table["all"]["metrics"]["candidate_winner_hit"]["n"] == 2
    assert table["channel=ЭМ,winner=known"]["metrics"]["candidate_winner_hit"]["delta"] == 1.0
    assert table["winner=new"]["metrics"]["candidate_winner_hit"]["delta"] == 0.0
    old = record("OLD", [{"query_id": r["query_id"]} for r in a["per_query"]])
    assert "candidate_winner_hit" not in C.paired(a, old)["all"]["metrics"]


@pytest.mark.parametrize("alpha", [0, 1, -0.1, float("nan")])
def test_invalid_alpha_is_rejected(alpha):
    with pytest.raises(ValueError, match="alpha"):
        C.paired(record("A", []), record("B", []), alpha=alpha)


def test_compare_cli_labels_the_requested_interval(tmp_path, capsys):
    for exp, value in (("A", 1.0), ("B", 0.0)):
        (tmp_path / f"{exp}.json").write_text(json.dumps(record(exp, [
            {"query_id": "q", "winner@10": value, "candidate_winner_hit": value}
        ])), encoding="utf-8")
    assert C.main(["A", "B", "--alpha", ".1"], results=tmp_path) == 0
    output = capsys.readouterr().out
    assert "90% CI" in output and "95% CI" not in output and "candidate_winner_hit" in output
    assert C.main(["A", "B"], results=tmp_path) == 0
    assert "95% CI" in capsys.readouterr().out
    assert C.main(["A", "B", "--alpha", "2"], results=tmp_path) == 2
    assert "alpha" in capsys.readouterr().err
