"""P-01 (#211): accuracy of the OKPD2 predicted from text, on the synthetic ingest fixture.

Dev lots of the fixture: 6 «Бумага офисная А4» (17.12.14.110, paper is in the history) and 8 «Услуги связи»
(61.10.11.110, no telecom in the history). Lot 7 is in the holdout window and must never be read.
"""

from __future__ import annotations

from datetime import date

from src.contracts import Query
from src.eval import okpd2_predict as P
from src.eval.split import EvalCase

CUTOFF = date(2025, 7, 1)


def _case(lot_id: int, text: str, day: date) -> EvalCase:
    return EvalCase(query=Query(query_id=f"lot-{lot_id}", text=text, as_of=CUTOFF), relevant_inns=[], date=day)


def test_level_hit_levels_and_coarse_truth():
    assert [P.level_hit("17.12.14.110", "17.12.14.190", lv) for lv in P.LEVELS] == [1.0, 1.0, 1.0, 0.0]
    assert P.level_hit("17.12.14.110", "17.29.1", "subcategory") == 0.0
    assert P.level_hit("33.12.18.000", "33.12.1", "subcategory") == 1.0
    assert P.level_hit("33.12.18.000", "33.12", "subcategory") is None  # truth coarser than the level
    assert P.level_hit(None, "17.12.14.110", "class") == 0.0


def test_dev_lots_measured_without_holdout(search_data):
    cases = [_case(6, "Бумага офисная А4", date(2025, 8, 1)), _case(8, "Услуги связи", date(2025, 9, 1))]
    body = P.evaluate(cases, CUTOFF)
    pos = {r["lot_id"]: r for r in body["per_query"]["positions"]}
    assert set(pos) == {6, 8}  # lot 7 (holdout window) is not read
    assert pos[6]["pred"] == "17.12.14.110" and pos[6]["hit"] == {"division": 1.0, "class": 1.0, "subcategory": 1.0,
                                                                  "code": 1.0}
    assert pos[8]["hit"]["class"] == 0.0  # no telecom before the cutoff: whatever is predicted is wrong
    m = body["metrics"]["positions"]
    assert m["n"] == 2 and m["accuracy"]["code"]["mean"] == 0.5
    assert body["baseline_code"] == "17.12.14.110"  # 2 history lots of paper vs 2 of cartridges: ties by code
    lots = {r["lot_id"]: r for r in body["per_query"]["lots"]}
    assert lots[6]["hit"]["class"] == 1.0 and lots[6]["truth"] == ["17.12.14.110"]
