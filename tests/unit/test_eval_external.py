"""Comparison with another team's lot list (#342): the same lots, history before 2025-01-01, no sealed lots."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb

from src.eval import build_cases as BC
from src.eval import external as EX
from src.eval.build_cases import Lot

CUTOFF, HOLDOUT_FROM = date(2025, 1, 1), date(2025, 10, 1)
W, L = "7802010006", "7802010013"


def _lot(lot_id: int, d: date, channel: str = "ЭМ", participants: list | None = None) -> Lot:
    parts = [(W, True, True), (L, False, True)] if participants is None else participants
    return Lot(lot_id, d, channel, f"Поставка товара {lot_id}", None, "7801000013", 100.0,
               [("Бумага офисная", "17.12.14")], parts)


def test_selection_keeps_listed_lots_in_file_order_and_drops_sealed_period() -> None:
    lots = [_lot(1, date(2025, 3, 1)), _lot(2, date(2025, 9, 30)), _lot(3, date(2025, 10, 1)),
            _lot(4, date(2025, 5, 1))]
    cases, stats = EX.select_cases(lots, [4, 3, 1, 2, 99], CUTOFF, HOLDOUT_FROM)
    assert [c.query.query_id for c in cases] == ["lot-4", "lot-1", "lot-2"]
    assert stats["listed"] == 5 and stats["not_in_data"] == 1 and stats["holdout_period"] == 1
    assert all(c.query.as_of == CUTOFF for c in cases)


def test_selection_requires_a_winner_with_valid_inn() -> None:
    no_winner = _lot(1, date(2025, 3, 1), participants=[(L, False, True)])
    bad_winner = _lot(2, date(2025, 3, 1), participants=[(W, True, False), (L, False, True)])
    ok = _lot(3, date(2025, 3, 1))
    cases, stats = EX.select_cases([no_winner, bad_winner, ok], [1, 2, 3], CUTOFF, HOLDOUT_FROM)
    assert [c.query.query_id for c in cases] == ["lot-3"]
    assert stats["no_winner"] == 2


def test_selection_never_returns_a_case_before_the_cutoff() -> None:
    cases, stats = EX.select_cases([_lot(1, date(2024, 12, 31))], [1], CUTOFF, HOLDOUT_FROM)
    assert cases == [] and stats["before_cutoff"] == 1


def test_sealed_lot_is_counted_by_date_before_its_participants_are_looked_at() -> None:
    """The date check precedes the winner check: a sealed lot without a winner is `holdout_period`, not `no_winner`."""
    sealed_no_winner = _lot(1, date(2025, 11, 1), participants=[(L, False, True)])
    _, stats = EX.select_cases([sealed_no_winner], [1], CUTOFF, HOLDOUT_FROM)
    assert stats["holdout_period"] == 1 and stats["no_winner"] == 0


def test_lots_are_classified_by_dates_without_being_loaded() -> None:
    dates = {1: date(2025, 11, 1), 2: date(2024, 12, 31), 3: date(2025, 5, 1)}
    cases, stats = EX.select_cases([], [1, 2, 3, 4], CUTOFF, HOLDOUT_FROM, dates)
    assert cases == []
    assert (stats["holdout_period"], stats["before_cutoff"], stats["not_in_data"]) == (1, 1, 2)  # 3: window, not loaded


def test_read_ids_drops_duplicates_and_takes_the_first_n(tmp_path: Path) -> None:
    f = tmp_path / "lots.csv"
    f.write_text("lot_id\n5\n7\n5\n9\n", encoding="utf-8")
    assert EX.read_ids(f, None) == [5, 7, 9]
    assert EX.read_ids(f, 2) == [5, 7]


def test_publish_dates_reads_only_the_listed_notices(tmp_path: Path) -> None:
    duckdb.sql(f"COPY (SELECT * FROM (VALUES (1, DATE '2025-03-01'), (2, DATE '2025-11-01'), (3, DATE '2025-04-01'))"
               f" t(lot_id, publish_date)) TO '{(tmp_path / 'notices.parquet').as_posix()}' (FORMAT parquet)")
    assert EX.publish_dates(tmp_path, [1, 2]) == {1: date(2025, 3, 1), 2: date(2025, 11, 1)}


def test_read_lots_does_not_load_lots_on_or_after_end(tmp_path: Path) -> None:
    def put(name: str, sql: str) -> None:
        duckdb.sql(f"COPY ({sql}) TO '{(tmp_path / (name + '.parquet')).as_posix()}' (FORMAT parquet)")

    put("notices", "SELECT * FROM (VALUES (1, DATE '2025-01-01', 'ЭМ', 's', 'p', '7800000001', 1.0),"
                   " (2, DATE '2025-09-30', 'ЭМ', 's', 'p', '7800000001', 1.0),"
                   " (3, DATE '2025-10-01', 'ЭМ', 's', 'p', '7800000001', 1.0),"
                   " (4, DATE '2025-12-01', 'ЭМ', 's', 'p', '7800000001', 1.0))"
                   " t(lot_id, publish_date, channel, subject, procedure_name, customer_inn, start_price)")
    put("items", "SELECT 1 AS lot_id, 'x' AS product_name, '01.11' AS okpd2_code")
    put("participants", "SELECT * FROM (VALUES (3, '7800000002', true, true), (2, '7800000003', true, true))"
                        " t(lot_id, supplier_inn, is_winner, inn_valid)")
    got = BC.read_lots(tmp_path, date(2025, 1, 1), date(2025, 10, 1))
    assert [x.lot_id for x in got] == [1, 2]  # the lot of 2025-10-01 itself is the sealed period
    assert [x.lot_id for x in BC.read_lots(tmp_path, date(2025, 1, 2), date(2025, 10, 1))] == [2]
    assert [x.lot_id for x in BC.read_lots(tmp_path, date(2025, 1, 1))] == [1, 2, 3, 4]
