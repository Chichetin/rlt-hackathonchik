"""Co-bidding fact (#508): the partner with the most shared ЭМ lots, thresholds, as_of, АИС ГЗ, no break."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Origin
from src.enrich.evidence import strip_unverified
from src.match import copartner as cobid
from src.match import pipeline
from src.match import store as store_mod

A, B, C = "7801234564", "7801234571", "7801234589"


def _rows() -> list[tuple]:
    rows = []  # lot_id, inn, is_winner, publish_date, channel
    for lot in range(1, 7):  # ЭМ lots 1-6: A and B together, B wins 1-3, C wins 4-6 (C bids on 4-6 as well)
        d = date(2025, lot, 10)
        rows += [(lot, A, False, d, "ЭМ"), (lot, B, lot <= 3, d, "ЭМ")]
        if lot > 3:
            rows.append((lot, C, True, d, "ЭМ"))
    rows.append((7, A, True, date(2025, 7, 1), "ЭМ"))  # A alone: A has 7 ЭМ lots, 6 shared with B
    rows += [(8, A, True, date(2025, 8, 1), "АИС ГЗ"), (8, B, True, date(2025, 8, 1), "АИС ГЗ")]  # not counted
    return rows


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> store_mod.ParquetStore:
    values = ", ".join(f"({lot}, '{inn}', {str(w).lower()}, DATE '{d}', '{ch}')" for lot, inn, w, d, ch in _rows())
    duckdb.sql(f"""COPY (SELECT lot_id::BIGINT lot_id, inn supplier_inn, w is_winner, d publish_date, ch channel
        FROM (VALUES {values}) v(lot_id, inn, w, d, ch)) TO '{(tmp_path / "participants.parquet").as_posix()}'""")
    st = store_mod.ParquetStore(tmp_path)
    monkeypatch.setattr(cobid, "get_store", lambda: st)
    return st


def match(inn: str, origin: Origin = "history") -> CounterpartyMatch:
    cp = Counterparty(inn=inn, name=f"ИНН {inn}")
    return CounterpartyMatch(counterparty=cp, score=1.0, origin=origin, components=[])


def test_partner_share_and_partner_wins(store):
    (m,) = cobid.with_cobid([match(A)], None)
    (ev,) = m.counterparty.evidence
    assert ev.source == "data:participants" and ev.url == "data:participants#6"
    assert f"вместе с ИНН {B} в 86% своих лотов ЭМ (6 из 7)" in ev.fact
    assert f"ИНН {B} побеждает в 50%" in ev.fact  # B won 3 of 6 shared; the АИС ГЗ lot 8 is not counted
    assert "сговор" not in ev.fact.lower() and "картел" not in ev.fact.lower()
    s = m.counterparty.stats
    assert s["cobid_partner_inn"] == B and s["cobid_lots"] == 6
    assert s["cobid_share"] == pytest.approx(6 / 7) and s["cobid_partner_win_rate"] == pytest.approx(0.5)
    assert s["cobid_jaccard"] == pytest.approx(6 / 7)  # B bids in the same 6 ЭМ lots


def test_partner_with_most_shared_lots_wins_over_a_smaller_one(store):
    (m,) = cobid.with_cobid([match(B)], None)  # B: 6 ЭМ lots, A in 6, C in 3
    assert m.counterparty.stats["cobid_partner_inn"] == A and m.counterparty.stats["cobid_share"] == 1.0


def test_as_of_cuts_later_lots_and_the_threshold(store):
    # before 2025-05-01: lots 1-4 shared (4 < 5) -> no fact; A has 4 own lots
    (m,) = cobid.with_cobid([match(A)], date(2025, 5, 1))
    assert m.counterparty.evidence == [] and "cobid_lots" not in m.counterparty.stats
    # before 2025-06-01: lots 1-5 shared (5 = threshold, 5 of 5) -> the fact, the lot 6 itself excluded
    (m,) = cobid.with_cobid([match(A)], date(2025, 6, 1))
    assert m.counterparty.stats["cobid_lots"] == 5 and m.counterparty.evidence[0].url.endswith("#5")


def test_share_below_half_gives_no_fact(store, tmp_path):
    extra = ", ".join(f"({100 + i}, '{A}', true, DATE '2025-01-01', 'ЭМ')" for i in range(8))
    duckdb.sql(f"""COPY (SELECT * FROM '{(tmp_path / "participants.parquet").as_posix()}' UNION ALL
        SELECT * FROM (VALUES {extra}) v) TO '{(tmp_path / "p2.parquet").as_posix()}'""")
    (tmp_path / "participants.parquet").unlink()
    (tmp_path / "p2.parquet").rename(tmp_path / "participants.parquet")  # A: 15 own lots, 6 shared = 40 %
    (m,) = cobid.with_cobid([match(A)], None)
    assert m.counterparty.evidence == []


def test_not_for_discovered_and_a_failure_does_not_break(store, monkeypatch):
    d = match(A, origin="discovered")
    assert cobid.with_cobid([d], None) == [d]

    def boom():
        raise RuntimeError("db is down")
    monkeypatch.setattr(cobid, "get_store", boom)
    m = match(A)
    assert cobid.with_cobid([m], None) == [m]


def test_fact_survives_the_evidence_check(store):
    (m,) = pipeline.with_cobid([match(A)], None)
    cp = strip_unverified(m.counterparty, lambda url: None)
    assert any(isinstance(e, Evidence) and e.source == "data:participants" and "вместе с ИНН" in e.fact
               for e in cp.evidence)
