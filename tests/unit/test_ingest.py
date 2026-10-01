"""Ingest on the synthetic fixture (tests/fixtures/ingest/README.md): counts, dedup, INN flag, as_of."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from src.enrich.evidence import check_counterparty, strip_unverified
from src.ingest.build import build
from src.ingest.load import counterparties, suppliers_path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ingest"
CUTOFF = date(2025, 7, 1)
A, B, D, BAD = "7801000012", "7801000020", "780100000090", "7801000013"


@pytest.fixture(scope="module")
def processed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("processed")
    build(FIXTURE, out, as_of=None, cutoff=CUTOFF)
    return out


def _rows(path: Path, sql: str) -> list[tuple]:
    return duckdb.sql(sql.replace("{p}", path.as_posix())).fetchall()


def test_files_and_counts(processed: Path) -> None:
    for name in ("notices", "participants", "items", "suppliers", f"suppliers_{CUTOFF}"):
        assert (processed / f"{name}.parquet").exists(), name
    # 16 participant lines with one duplicate (lot 3, A); 14 item lines with one exact duplicate.
    assert _rows(processed / "notices.parquet", "SELECT count(*) FROM '{p}'") == [(9,)]
    assert _rows(processed / "participants.parquet", "SELECT count(*) FROM '{p}'") == [(15,)]
    assert _rows(processed / "items.parquet", "SELECT count(*) FROM '{p}'") == [(13,)]


def test_duplicate_participant_keeps_winner(processed: Path) -> None:
    rows = _rows(
        processed / "participants.parquet", f"SELECT is_winner FROM '{{p}}' WHERE lot_id = 3 AND supplier_inn = '{A}'"
    )
    assert rows == [(True,)]


def test_okpd2_levels(processed: Path) -> None:
    rows = dict(
        (r[0], r[1:])
        for r in _rows(processed / "items.parquet", "SELECT okpd2_code, okpd2_l2, okpd2_l4, okpd2_l6 FROM '{p}'")
    )
    assert rows["17.12.14.110"] == ("17", "17.12", "17.12.14")
    assert rows["33.12.1"] == ("33", "33.12", None)
    assert rows["11.07"] == ("11", "11.07", None)


def test_invalid_inn_flagged_not_dropped(processed: Path) -> None:
    assert _rows(processed / "suppliers.parquet", f"SELECT inn_valid FROM '{{p}}' WHERE inn = '{BAD}'") == [(False,)]
    cp = counterparties([BAD], processed=processed)[BAD]
    assert cp.inn is None and cp.evidence == []


def test_aggregates_all_history(processed: Path) -> None:
    path = processed / "suppliers.parquet"
    cols = "lots_participated, lots_won, customers, customers_spb, first_seen, last_seen"
    assert _rows(path, f"SELECT {cols} FROM '{{p}}' WHERE inn = '{A}'") == [
        (5, 3, 2, 1, date(2024, 1, 20), date(2025, 10, 15))
    ]
    assert _rows(path, f"SELECT median_start_price FROM '{{p}}' WHERE inn = '{B}'") == [(120000.0,)]


def test_as_of_cuts_history(processed: Path) -> None:
    """suppliers_<cutoff> sees nothing published on or after the cutoff (no leak, D-018)."""
    path = processed / f"suppliers_{CUTOFF}.parquet"
    assert _rows(path, "SELECT max(last_seen) FROM '{p}'")[0][0] < CUTOFF
    assert _rows(path, f"SELECT lots_participated, lots_won FROM '{{p}}' WHERE inn = '{A}'") == [(3, 3)]
    assert _rows(path, f"SELECT lots_participated, lots_won, win_rate FROM '{{p}}' WHERE inn = '{D}'") == [(1, 0, 0.0)]
    assert _rows(path, f"SELECT top_okpd2 FROM '{{p}}' WHERE inn = '{A}'") == [("17.12,11.07",)]


def test_counterparty_evidence_and_stats(processed: Path) -> None:
    now = counterparties([A, "0000000000"], processed=processed)
    assert set(now) == {A}  # unknown INN is absent, not invented
    cp = now[A]
    assert cp.inn == A and cp.name == f"ИНН {A}" and cp.kpp == "780101001" and cp.region is None
    assert cp.stats["lots_participated"] == 5 and cp.stats["last_seen"] == "2025-10-15"
    [ev] = cp.evidence
    assert ev.url == "data:participants#7" and ev.field == "inn" and ev.value == A
    # The API passes every card through strip_unverified(): data: Evidence must survive it.
    assert check_counterparty(cp, lambda url: None) == []
    assert strip_unverified(cp, lambda url: None).inn == A

    past = counterparties([A], as_of=CUTOFF, processed=processed)[A]
    assert past.stats["lots_participated"] == 3
    assert past.evidence[0].url == "data:participants#3"  # latest lot before the cutoff


def test_crlf_and_lf_build_the_same(tmp_path: Path) -> None:
    """Export from Windows (CRLF) and Linux (LF) give identical tables, whatever git checked out."""
    tables = {}
    for eol in (b"\n", b"\r\n"):
        raw, out = tmp_path / f"raw{len(eol)}", tmp_path / f"out{len(eol)}"
        raw.mkdir()
        for src in FIXTURE.glob("*.csv"):
            (raw / src.name).write_bytes(src.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", eol))
        build(raw, out, as_of=None, cutoff=CUTOFF)
        tables[eol] = [
            _rows(out / f"{name}.parquet", "SELECT * FROM '{p}' ORDER BY ALL")
            for name in ("notices", "participants", "items", "suppliers")
        ]
    assert tables[b"\n"] == tables[b"\r\n"]
    assert all(t for t in tables[b"\n"])


def test_unbuilt_as_of_is_a_clear_error(processed: Path) -> None:
    with pytest.raises(FileNotFoundError, match="src.ingest.build"):
        suppliers_path(date(2025, 1, 1), processed)


def test_supplier_profile_keys(processed: Path) -> None:
    cols = (
        "okpd2_classes, top_class_share, distinct_codes, top_customer_share, repeat_win_share, "
        "active_months, em_offers, em_wins, aisgz_contracts, entity_type, kpp_count, avg_rivals"
    )
    assert _rows(processed / "suppliers.parquet", f"SELECT {cols} FROM '{{p}}' WHERE inn = '{A}'") == [
        (3, 0.8, 4, 0.6, 0.3333, 5, 5, 3, 0, "ЮЛ", 1, 1.2)
    ]
    ip = _rows(processed / "suppliers.parquet", "SELECT entity_type FROM '{p}' WHERE inn = '780100000090'")
    assert ip == [("ИП",)]
    st = counterparties([A], processed=processed)[A].stats
    assert st["top_class_share"] == 0.8 and st["entity_type"] == "ЮЛ"
