"""Pool of new companies from the MSP registry dump (#185): parquet build, Postgres schema `pool`, lookups.
The Postgres part needs TEST_DATABASE_URL (as tests/unit/test_db_load.py) and is skipped otherwise."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.db import pool
from src.enrich.evidence import check_counterparty
from src.sources import msp_dump
from src.sources.cache import SourceCache

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "msp_dump" / "sample.xml"
URL = os.environ.get("TEST_DATABASE_URL", "")
SCHEMA = "test_pool"  # never the app schema `pool`
FETCHED = 1_789_000_000.0

needs_pg = pytest.mark.skipif(not URL.startswith("postgres"), reason="TEST_DATABASE_URL не задан")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two members: the fixture and a later state of doc-ul-1 (same INN, new name, main OKVED among extras)."""
    tmp = tmp_path_factory.mktemp("pool")
    xml = FIXTURE.read_text(encoding="utf-8")
    later = (xml.replace('ДатаСост="10.09.2026" ДатаВклМСП="10.07.2021"',
                         'ДатаСост="11.09.2026" ДатаВклМСП="10.07.2021"')
                .replace("ТЕСТОВАЯ БУМАГА", "НОВАЯ БУМАГА")
                .replace('КодОКВЭД="46.76.1"', 'КодОКВЭД="17.12"'))
    zip_path = tmp / "dump.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("a.xml", xml)
        z.writestr("b.xml", later)
    msp_dump.build(zip_path, tmp / "parts", workers=1)
    pool.build(tmp / "parts", tmp / "pool", fetched_at=FETCHED)
    return tmp / "pool"


def _table(path: Path, name: str) -> list[dict]:
    return pq.read_table(path / f"{name}.parquet").to_pylist()


def test_build_keeps_one_latest_record_per_inn(built: Path) -> None:
    rows = {r["inn"]: r for r in _table(built, "companies")}
    assert sorted(rows) == ["007800000309", "0078000013", "0078000014"]
    assert rows["0078000013"]["name"] == 'ООО "НОВАЯ БУМАГА"' and rows["0078000013"]["src_file"] == "b.xml"
    manifest = json.loads((built / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["rows"] == {"companies": 3, "okved": 3, "company_class": 1}
    assert manifest["fetched_at"] == FETCHED and manifest["source_records"] == 6


def test_build_role_by_main_okved_and_no_locality_for_ip(built: Path) -> None:
    rows = {r["inn"]: r for r in _table(built, "companies")}
    assert rows["0078000013"]["kind"] == "manufacturer" and "обрабатывающее" in rows["0078000013"]["kind_why"]
    assert rows["007800000309"]["kind"] == "unknown"
    assert rows["0078000014"]["kind"] is None  # no main OKVED — no role
    assert rows["007800000309"]["city"] is None and rows["0078000013"]["city"] is None  # СПб: no city element


def test_build_okved_index_rows_main_and_extra_without_duplicates(built: Path) -> None:
    got = sorted((r["code"], r["inn"], r["main"]) for r in _table(built, "okved"))
    assert {r["inn"]: r["region_code"] for r in _table(built, "okved")} == {"0078000013": "78", "007800000309": "47"}
    # b.xml lists the main code 17.12 among the extras too: one row, marked main; no OKVED — no rows
    assert got == [("17.12", "0078000013", True), ("43.39", "007800000309", True), ("46.49.33", "0078000013", False)]


def test_build_company_class_from_products(built: Path) -> None:
    assert _table(built, "company_class") == [{
        "inn": "0078000013", "okpd2_class": "17", "okpd2": "17.12.14", "name": "Бумага прочая для печати и письма",
        "method": "СвПрод"}]


def test_build_is_idempotent(built: Path) -> None:
    before = {n: _table(built, n) for n in pool.TABLES}
    pool.build(built.parent / "parts", built, fetched_at=FETCHED)
    assert {n: _table(built, n) for n in pool.TABLES} == before
    assert not list(built.glob("*.tmp"))


def test_lookup_on_parquet(built: Path) -> None:
    got = pool.lookup(["0078000013", "007800000309", "7707083893"], pool_dir=built)
    assert sorted(got) == ["007800000309", "0078000013"]
    assert got["0078000013"]["okved_extra"] == ["46.49.33", "17.12"]


def test_candidates_by_okved_prefix_main_first_and_region(built: Path) -> None:
    assert pool.candidates(["17.12", "46.49"], pool_dir=built) == [
        {"inn": "0078000013", "code": "17.12", "main": True}]  # one row per INN: its best match
    assert pool.candidates(["46.49"], pool_dir=built) == [{"inn": "0078000013", "code": "46.49.33", "main": False}]
    assert pool.candidates(["43"], regions=["78"], pool_dir=built) == []
    assert pool.candidates(["43"], regions=["47"], pool_dir=built) == [
        {"inn": "007800000309", "code": "43.39", "main": True}]
    assert pool.candidates(["17", "43"], pool_dir=built, limit=1) == [  # main first, then by INN
        {"inn": "007800000309", "code": "43.39", "main": True}]
    for bad in ("4", "46.4%", "46.", "_6", "46.49.33.1"):  # not an OKVED prefix: an error, not "nothing found"
        with pytest.raises(ValueError):
            pool.candidates([bad], pool_dir=built)


def test_counterparties_put_pages_in_the_cache_and_pass_the_check(built: Path, tmp_path: Path) -> None:
    cache = SourceCache(tmp_path / "sources.sqlite")
    got = pool.counterparties(["0078000013", "0078000014", "007800000309", "7707083893"], cache, pool_dir=built)
    assert sorted(got) == ["007800000309", "0078000013"]  # bad checksum and unknown INN: no company
    for cp in got.values():
        assert check_counterparty(cp, cache.page_text) == []
        assert all(e.fetched_at.timestamp() == FETCHED for e in cp.evidence)
    page = cache.get(msp_dump.page_url("0078000013"), allow_stale=True)
    assert page is not None and page.fetched_at == FETCHED and "НОВАЯ БУМАГА" in page.body


def test_missing_pool_gives_nothing(tmp_path: Path) -> None:
    assert pool.lookup(["0078000013"], pool_dir=tmp_path / "none") == {}
    assert pool.candidates(["17"], pool_dir=tmp_path / "none") == []


def test_stats_counts_new_companies_and_suppliers_coverage(built: Path, tmp_path: Path) -> None:
    suppliers = tmp_path / "suppliers.parquet"
    pq.write_table(pa.table({"inn": ["0078000013", "7707083893"], "inn_valid": [True, True],
                             "lots_participated": [3, 7], "lots_won": [1, 4]}), suppliers)
    s = pool.stats(built, suppliers)
    assert s["companies"] == 3 and s["spb"] == 1 and s["lo"] == 1
    assert s["not_in_suppliers"] == 2 and s["not_in_suppliers_spb_lo"] == 1
    assert s["with_products"] == 1 and s["with_licences"] == 1
    assert s["suppliers_valid_inn"] == 2 and s["suppliers_in_pool"] == 1
    assert s["participations_in_pool"] == 3 and s["participations"] == 10
    assert s["wins_in_pool"] == 1 and s["wins"] == 5


@pytest.fixture()
def pg_pool(built: Path):
    counts = pool.load(built, URL, schema=SCHEMA)
    yield counts
    import psycopg

    with psycopg.connect(pool.pg_dsn(URL), autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}, {SCHEMA}_new CASCADE")


@needs_pg
def test_load_into_postgres_is_idempotent_and_matches_parquet(built: Path, pg_pool: dict) -> None:
    assert pg_pool == {"companies": 3, "okved": 3, "company_class": 1}
    assert pool.load(built, URL, schema=SCHEMA) == pg_pool
    inns = ["0078000013", "007800000309", "0078000014"]
    assert pool.lookup(inns, url=URL, schema=SCHEMA) == pool.lookup(inns, pool_dir=built)
    for args in (["17.12", "46.49"], ["46.49"], ["43"]):
        assert pool.candidates(args, url=URL, schema=SCHEMA) == pool.candidates(args, pool_dir=built)
    assert pool.candidates(["43"], regions=["78"], url=URL, schema=SCHEMA) == []


def test_load_refuses_foreign_schemas(built: Path) -> None:
    for bad in ("public", "data", "vec", "pg_x", "Pool;drop"):
        with pytest.raises(ValueError):
            pool.load(built, "postgresql://x", schema=bad)
