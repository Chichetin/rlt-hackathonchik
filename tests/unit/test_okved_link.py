"""OKVED → OKPD2 link learned from history (#250): synthetic organizers' data + a synthetic MSP pool."""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from src.enrich import okved_link
from src.eval import okved_link as link_eval

CUTOFF, HOLDOUT = date(2025, 7, 1), date(2025, 10, 1)

# (lot, class, published, [(supplier, is_winner)])
LOTS = [
    (1, "21.20", "2025-01-10", [("A", True), ("B", False), ("C", False)]),
    (2, "17.12", "2025-02-01", [("D", True), ("E", False), ("F", False)]),
    (3, "21.20", "2025-08-01", [("G", True)]),  # dev, cold winner with the learned OKVED
    (4, "17.12", "2025-08-05", [("H", True)]),  # dev, cold winner with the generic OKVED
    (5, "21.20", "2025-08-10", [("A", True)]),  # dev, the winner is known from train — not cold
    (6, "21.20", "2025-11-01", [("X", True)]),  # holdout — never read
    (8, "17.12", "2025-07-15", [("B", False), ("D", True)]),  # after the cutoff: must not teach the link
]
# (inn, code, main, region)
OKVED = [
    ("A", "46.46.1", True, "78"), ("A", "17.12", False, "78"),  # the additional code is ignored
    ("B", "46.46", True, "78"), ("C", "46.90", True, "78"), ("D", "46.90", True, "47"), ("E", "46.90", True, "78"),
    ("F", "17.12", True, "78"), ("G", "46.46", True, "78"), ("H", "46.90", True, "78"), ("X", "46.46", True, "78"),
    ("N1", "46.46", True, "78"), ("N2", "46.46.2", True, "47"), ("N3", "46.46", True, "77"),  # not suppliers
]


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    processed, pool = tmp_path / "processed", tmp_path / "pool"
    processed.mkdir()
    pool.mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE p (lot_id BIGINT, supplier_inn VARCHAR, is_winner BOOLEAN, inn_valid BOOLEAN, "
                "publish_date DATE)")
    con.execute("CREATE TABLE i (lot_id BIGINT, okpd2_l4 VARCHAR)")
    con.execute("CREATE TABLE o (code VARCHAR, inn VARCHAR, main BOOLEAN, region_code VARCHAR)")
    for lot, cls, day, people in LOTS:
        con.execute("INSERT INTO i VALUES (?, ?)", [lot, cls])
        for inn, won in people:
            con.execute("INSERT INTO p VALUES (?, ?, ?, true, ?)", [lot, inn, won, day])
    con.execute("INSERT INTO p VALUES (1, 'BAD', false, false, '2025-01-10')")  # invalid INN is not a supplier
    con.executemany("INSERT INTO o VALUES (?, ?, ?, ?)", [(c, i, m, r) for i, c, m, r in OKVED])
    con.execute(f"COPY p TO '{(processed / 'participants.parquet').as_posix()}' (FORMAT parquet)")
    con.execute(f"COPY i TO '{(processed / 'items.parquet').as_posix()}' (FORMAT parquet)")
    con.execute(f"COPY o TO '{(pool / 'okved.parquet').as_posix()}' (FORMAT parquet)")
    return processed, pool


def _rows(processed: Path) -> dict[tuple[str, str], tuple[int, int, float, float]]:
    path = (processed / okved_link.TABLE).as_posix()
    cur = duckdb.connect().execute(
        f"SELECT cls, okved, suppliers, class_suppliers, share, lift FROM read_parquet('{path}')")
    return {(c, o): (n, cn, s, lift) for c, o, n, cn, s, lift in cur.fetchall()}


def test_build_counts_main_okved_of_class_suppliers_before_as_of(dirs):
    processed, pool = dirs
    assert okved_link.build(CUTOFF, processed, pool) == {"rows": 4, "classes": 2}
    rows = _rows(processed)
    # 21.20: A and B (46.46.1 → class level 46.46) of three suppliers; 46.46 is the OKVED of 2 of 6 train suppliers
    assert rows[("21.20", "46.46")] == (2, 3, pytest.approx(2 / 3), pytest.approx(2.0))
    assert rows[("21.20", "46.90")] == (1, 3, pytest.approx(1 / 3), pytest.approx(2 / 3))
    assert rows[("17.12", "46.90")] == (2, 3, pytest.approx(2 / 3), pytest.approx(4 / 3))
    assert rows[("17.12", "17.12")] == (1, 3, pytest.approx(1 / 3), pytest.approx(2.0))  # F; A's additional 17.12 — no
    assert ("17.12", "46.46") not in rows  # lot 8 (B in a 17.12 lot) is after the cutoff


def test_later_as_of_reads_later_lots_and_holdout_is_refused(dirs, capsys):
    processed, pool = dirs
    okved_link.build(date(2025, 8, 1), processed, pool)
    assert _rows(processed)[("17.12", "46.46")][0] == 1  # now lot 8 counts
    code = okved_link.main(["build", "--as-of", "2025-12-01", "--processed", str(processed), "--pool", str(pool)])
    assert code == 2 and "holdout" in capsys.readouterr().err


def test_okveds_applies_thresholds_and_explains(dirs):
    processed, pool = dirs
    okved_link.build(CUTOFF, processed, pool)
    table = processed / okved_link.TABLE
    got = okved_link.okveds("21.20", table, min_lift=1.5, min_suppliers=2)
    assert [(x.okved, x.suppliers, x.class_suppliers) for x in got] == [("46.46", 2, 3)]
    assert got[0].why("21.20") == ("основной ОКВЭД 46.46 — у 2 из 3 поставщиков класса 21.20 в истории закупок "
                                  "(в 2.0 раза чаще, чем в среднем)")
    assert okved_link.okveds("21.20", table, min_lift=2.5, min_suppliers=2) == []  # lift 2.0 is below
    assert okved_link.okveds("21.20", table, min_lift=1.5, min_suppliers=3) == []  # two suppliers are too few
    both = okved_link.okveds("17.12", table, min_lift=1.0, min_suppliers=1)
    assert [x.okved for x in both] == ["46.90", "17.12"]  # the most frequent first
    assert [x.okved for x in okved_link.okveds("17.12", table, min_lift=1.0, min_suppliers=1, top=1)] == ["46.90"]
    assert okved_link.okveds("99.99", table) == [] and okved_link.okveds("21.20", processed / "absent.parquet") == []


def test_rule_d036_is_class_plus_wholesale_row():
    assert ("21.20", "21.20") in okved_link.rule_d036(["21.20"])
    assert all(cls == "21.20" for cls, _ in okved_link.rule_d036(["21.20"]))


def test_evaluate_counts_only_cold_dev_winners_and_never_the_holdout(dirs):
    processed, pool = dirs
    res = link_eval.evaluate(CUTOFF, HOLDOUT, processed, pool, min_lift=1.5, min_suppliers=2)
    # cold dev winners: G (lot 3) and H (lot 4); A (lot 5) and D (lot 8) are known from train, X (lot 6) is holdout
    assert (res["n"], res["cold_winner_pairs"], res["cold_in_pool_share"]) == (2, 2, 1.0)
    assert res["rules"]["class"]["hit"] == 0.0  # nobody has OKVED 21.20 / 17.12
    assert res["rules"]["learned"]["hit"] == 0.5  # G by 46.46 for 21.20; H's 46.90 has lift 1.33 — not selected
    assert res["rules"]["class+learned"]["hit"] == 0.5
    # СПб+ЛО pool companies with main OKVED 46.46: A, B, G, X, N1, N2 (N3 is Moscow) — for lot 3; lot 4 selects none
    assert res["rules"]["learned"]["candidates_mean"] == 3.0
    assert res["deltas"]["learned - d036"]["delta"] == res["rules"]["learned"]["hit"] - res["rules"]["d036"]["hit"]
    assert res["link"] == {"rows": 4, "classes": 2}
