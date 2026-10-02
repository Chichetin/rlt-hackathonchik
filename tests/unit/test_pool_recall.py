"""Recall of the «Новые» pool (#187): the winner's history hidden, candidates of the OKVED plan, random baselines."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.contracts import Query
from src.eval import pool_recall
from src.eval.pool_recall import Candidate, hit_probability, rank
from src.eval.split import EvalCase

CUTOFF = date(2025, 7, 1)
# inn: (entity, region, main OKVED, additional OKVED)
COMPANIES = {
    "7800000001": ("ЮЛ", "78", "17.12", []),  # the winner, known in organizers' data → hidden, must be found
    "7800000002": ("ЮЛ", "78", "17.12", []),  # known, not a winner → dropped as in the product
    "4700000003": ("ЮЛ", "47", "46.76.1", []),  # wholesale of paper, neighbour region
    "7800000004": ("ЮЛ", "78", "62.01", ["17.12.2"]),  # the class by an additional OKVED only
    "7800000005": ("ЮЛ", "78", "62.01", []),  # in the region, another OKVED
    "7700000006": ("ЮЛ", "77", "17.12", []),  # the class, another region
}

REPORT = {"7800000004": ["17.12.2"]}  # reports the paper class although its main OKVED is IT
EMPLOYEES = {"4700000003": 250}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pool_dir, processed = tmp_path / "pool", tmp_path / "processed"
    pool_dir.mkdir()
    processed.mkdir()
    pq.write_table(pa.table({"inn": list(COMPANIES), "entity": [v[0] for v in COMPANIES.values()],
                             "region_code": [v[1] for v in COMPANIES.values()],
                             "okved_main": [v[2] for v in COMPANIES.values()],
                             "okved_report": [REPORT.get(i, []) for i in COMPANIES],
                             "employees": [EMPLOYEES.get(i) for i in COMPANIES]}),
                   pool_dir / "companies.parquet")
    okved = [(main, inn, True, reg) for inn, (_, reg, main, _) in COMPANIES.items()]
    okved += [(code, inn, False, reg) for inn, (_, reg, _, extra) in COMPANIES.items() for code in extra]
    pq.write_table(pa.table({k: [r[i] for r in okved] for i, k in enumerate(("code", "inn", "main", "region_code"))}),
                   pool_dir / "okved.parquet")
    pq.write_table(pa.table({"inn": ["7800000001", "7800000002"]}),
                   processed / f"suppliers_{CUTOFF.isoformat()}.parquet")
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(processed))
    monkeypatch.setenv("POOL_DIR", str(pool_dir))
    return pool_dir


def _case(qid: str, winner: str | None, okpd2: list[str]) -> EvalCase:
    q = Query(query_id=qid, text="бумага офисная", okpd2=okpd2, region="Санкт-Петербург", customer_inn="7801234567")
    return EvalCase(query=q, relevant_inns=[winner] if winner else ["7800000005"], date=date(2025, 8, 1),
                    grades={winner: 2} if winner else {})


def test_hit_probability_is_exact() -> None:
    assert hit_probability(10, 1) == 1.0
    assert hit_probability(20, 1) == pytest.approx(0.5)
    assert hit_probability(20, 2) == pytest.approx(1 - (10 * 9) / (20 * 19))
    assert hit_probability(0, 1) == 0.0 and hit_probability(50, 0) == 0.0


def test_rank_main_before_additional_and_kinds_in_turn() -> None:
    cands = [Candidate("3", "class", False, "78"), Candidate("1", "class", True, "78"),
             Candidate("2", "wholesale", True, "78"), Candidate("0", "class", True, "78")]
    weights = {"product_fit": 1.0, "geography": 0.0}
    assert rank(cands, "78", weights, 0.5) == ["0", "1", "2", "3"]  # 1.0, 1.0 (by INN), 0.6, 0.5
    assert rank(cands, "78", weights, 0.5, k=2) == ["0", "2"]  # a distributor takes the second slot


def test_winner_history_hidden_and_known_dropped(env: Path) -> None:
    cases = [_case("found", "7800000001", ["17.12.14.110"]), _case("far", "7700000006", ["17.12.14.110"]),
             _case("no-okpd2", "7800000001", []), _case("no-winner", None, ["17.12.14.110"])]
    res = pool_recall.evaluate(cases, {"weights": {"product_fit": 1.0}}, CUTOFF)
    assert res["skipped"] == {"no_winner": 1, "no_okpd2": 1}
    found, far = res["per_query"]
    # candidates: the hidden winner, the wholesaler of ЛО, the additional-OKVED company; not the known one
    assert found["n_candidates"] == 3 and found["covered"] == 1.0 and found["winner@10"] == 1.0
    assert found["winner_known"] is True
    assert found["n_region"] == 4  # 5 companies of СПб+ЛО, minus 2 known, plus the hidden winner
    assert far == far | {"in_pool": 1.0, "in_region": 0.0, "covered": 0.0, "winner@10": 0.0,
                         "random_region@10": 0.0, "n_candidates": 2}
    assert res["metrics"]["winner@10"]["value"] == 0.5
    assert set(res["slices"]) >= {"winner=known(hidden)", "entity=ЮЛ"}


def test_missing_pool_is_an_error(env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("POOL_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(FileNotFoundError, match="make pool-load"):
        pool_recall.evaluate([_case("x", "7800000001", ["17.12"])], {}, CUTOFF)


def test_product_channel_main_okved_and_ul_only(env: Path) -> None:
    """N-pool-recall-ul: the additional-OKVED company is not a candidate; the region holds ЮЛ only."""
    res = pool_recall.evaluate([_case("found", "7800000001", ["17.12.14.110"])],
                               {"weights": {"product_fit": 1.0}, "main_only": True, "entities": ["ЮЛ"]}, CUTOFF)
    (row,) = res["per_query"]
    assert row["n_candidates"] == 2 and row["covered"] == 1.0  # the winner and the wholesaler; not 7800000004
    res = pool_recall.evaluate([_case("found", "7800000001", ["17.12.14.110"])], {"entities": ["ИП"]}, CUTOFF)
    assert res["per_query"][0]["n_region"] == 0 and res["per_query"][0]["covered"] == 0.0


def test_lift_has_a_bootstrap_interval() -> None:
    rows = [{"covered": c, "random_cover": 0.1} for c in (1.0, 0.0, 1.0, 1.0)]
    out = pool_recall.lift(rows)
    assert out["value"] == pytest.approx(7.5) and out["ci95"][0] <= 7.5 <= out["ci95"][1]


def test_signal_order_and_paired_delta(env: Path) -> None:
    """order: {report: 1} puts the reporting company first; {size: 1} — the 250-employee wholesaler (#377)."""
    case = _case("found", "7800000001", ["17.12.14.110"])
    base = pool_recall.evaluate([case], {"weights": {"product_fit": 1.0}}, CUTOFF)
    report = pool_recall.evaluate([case], {"order": {"report": 1.0}}, CUTOFF)
    size = pool_recall.evaluate([case], {"order": {"size": 1.0}}, CUTOFF)
    assert base["per_query"][0]["winner_pos"] == 0  # main class OKVED, the lowest INN of the tier
    assert report["per_query"][0]["winner_pos"] == 2  # 7800000004 first, then by INN: 4700000003, the winner
    assert size["per_query"][0]["winner_pos"] == 1  # 4700000003 first
    d = pool_recall.paired(report, base)
    assert d["n_queries"] == 1 and d["winner_rr"]["delta"] == pytest.approx(1 / 3 - 1.0)
    with pytest.raises(ValueError, match="неизвестные сигналы"):
        pool_recall.evaluate([case], {"order": {"magic": 1.0}}, CUTOFF)
