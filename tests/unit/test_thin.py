"""Thin OKPD2 groups (#122, D-095): metric on a hand-built mini dataset, the threshold edges."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from src.eval import thin as T


def make_inn(base: str) -> str:
    """10-digit legal-entity INN from 9 digits: the checksum digit of the tax service."""
    w = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    return base + str(sum(int(d) * k for d, k in zip(base, w, strict=True)) % 11 % 10)


A, B, C, D = (make_inn(b) for b in ("770708389", "780100001", "780200002", "780300001"))


def _valid(inn: str) -> bool:
    from src.contracts import inn_is_valid
    return inn_is_valid(inn)


def build(tmp_path: Path, lots: list[tuple[int, str, str, str, list[str]]]) -> Path:
    """lots: (lot_id, class, channel, publish_date, [supplier inns])."""
    con = duckdb.connect()
    con.execute("CREATE TABLE items (lot_id BIGINT, okpd2_l4 VARCHAR)")
    con.execute("CREATE TABLE notices (lot_id BIGINT, channel VARCHAR, publish_date DATE)")
    con.execute("CREATE TABLE participants (lot_id BIGINT, supplier_inn VARCHAR, channel VARCHAR, inn_valid BOOLEAN)")
    for lot, cls, channel, day, inns in lots:
        con.execute("INSERT INTO items VALUES (?, ?)", [lot, cls])
        con.execute("INSERT INTO notices VALUES (?, ?, ?)", [lot, channel, day])
        for inn in inns:
            con.execute("INSERT INTO participants VALUES (?, ?, ?, ?)", [lot, inn, channel, _valid(inn)])
    for name in ("items", "notices", "participants"):
        con.execute(f"COPY {name} TO '{(tmp_path / (name + '.parquet')).as_posix()}' (FORMAT parquet)")
    con.close()
    return tmp_path


def test_valid_fixture_inns():
    assert all(_valid(i) for i in (A, B, C, D)), "fixture INNs must pass the checksum"


def test_group_metrics(tmp_path):
    em, day = "ЭМ", "2025-01-10"
    p = build(tmp_path, [
        (1, "17.12", em, day, [A]),
        (2, "17.12", em, day, [A]),
        (3, "17.12", em, day, [A, B, C]),
        (4, "17.12", em, day, [B, C]),
        (5, "20.30", em, day, [A, B]),
    ])
    g = {x.okpd2: x for x in T.compute_groups(p, "2025-07-01")}
    paper = g["17.12"]
    assert (paper.lots, paper.single_share, paper.median_offers, paper.suppliers) == (4, 0.5, 1.5, 3)
    assert paper.per_100_lots == 75.0
    assert g["20.30"].single_share == 0.0 and g["20.30"].suppliers == 2


def test_only_channel_and_period_before_cutoff(tmp_path):
    p = build(tmp_path, [
        (1, "17.12", "ЭМ", "2025-06-30", [A]),            # last day before the cutoff: counted
        (2, "17.12", "ЭМ", "2025-07-01", [A, B]),         # cutoff day: dev period, not read
        (3, "17.12", "АИС ГЗ", "2025-01-01", [C]),         # other channel
        (4, "17.12", "ЭМ", "2025-12-01", [D]),            # holdout
    ])
    (g,) = T.compute_groups(p, "2025-07-01")
    assert (g.lots, g.suppliers) == (1, 1)


def test_invalid_inn_is_not_an_offer(tmp_path):
    p = build(tmp_path, [(1, "17.12", "ЭМ", "2025-01-10", [A, "1234567890"])])
    (g,) = T.compute_groups(p, "2025-07-01")
    assert g.single_share == 1.0 and g.suppliers == 1


def test_lot_with_two_groups_counts_in_both(tmp_path):
    p = build(tmp_path, [(1, "17.12", "ЭМ", "2025-01-10", [A])])
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 1::BIGINT AS lot_id, '17.12' AS okpd2_l4 UNION ALL SELECT 1, '20.30') TO "
                f"'{(p / 'items.parquet').as_posix()}' (FORMAT parquet)")
    con.close()
    groups = T.compute_groups(p, "2025-07-01")
    assert sorted(g.okpd2 for g in groups) == ["17.12", "20.30"]
    assert sum(g.lots for g in groups) == 2  # lot-group pairs
    assert T.distinct_lots(T.lots_by_group(p, "2025-07-01"), groups) == 1  # but one lot


@pytest.mark.parametrize("lots, single, per100, thin", [
    (50, 0.5, 25.0, True),      # all three on the edge: thin (>=, >=, <=)
    (49, 0.5, 25.0, False),     # one lot short
    (50, 0.4999, 25.0, False),  # a hair of competition more
    (50, 0.5, 25.01, False),    # a hair wider pool
])
def test_threshold_edges(lots, single, per100, thin):
    g = T.Group("17.12", lots, single, 1.0, 10, per100)
    assert T.is_thin(g, T.Thresholds()) is thin


def test_thin_groups_sorted_by_demand_and_sensitivity():
    gs = [T.Group("a", 60, 0.9, 1.0, 5, 8.0), T.Group("b", 90, 0.9, 1.0, 5, 5.0),
          T.Group("c", 90, 0.2, 2.0, 50, 55.0), T.Group("d", 90, 0.9, 1.0, 5, 5.0)]
    assert [g.okpd2 for g in T.thin_groups(gs, T.Thresholds())] == ["b", "d", "a"]  # lots desc, then code
    rows = T.sensitivity(gs, T.Thresholds())
    assert {(r["param"], r["value"]): r["thin_groups"] for r in rows}[("min_lots", 100)] == 0
    assert {(r["param"], r["value"]): r["thin_groups"] for r in rows}[("min_lots", 50)] == 3


def test_record_counts_thin_and_top():
    gs = [T.Group("a", 60, 0.9, 1.0, 5, 8.0), T.Group("b", 90, 0.9, 1.0, 5, 5.0), T.Group("c", 10, 1.0, 1.0, 1, 10.0)]
    rec = T.build_record("X", gs, T.Thresholds(), "2025-07-01", "ЭМ", top_n=1)
    assert rec["metrics"]["thin_groups"] == 2 and rec["population"]["eligible_groups"] == 2
    assert [g["okpd2"] for g in rec["top"]] == ["b"]
    assert date.fromisoformat(rec["created_at"][:10])
