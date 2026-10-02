"""Companies outside the SME registry in the pool (#219): scripts/large_pool.py, `large` of src/db/pool.py,
discover.from_pool() — the Evidence of such a company passes src/enrich/evidence.py without a network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.contracts import Query
from src.db import pool
from src.enrich import discover as discover_mod
from src.enrich.evidence import check_counterparty
from src.sources import bo_nalog, msp_dump, pb_nalog
from src.sources.cache import CachedPage, SourceCache

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("large_pool", ROOT / "scripts" / "large_pool.py")
assert spec and spec.loader
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)

BO_INN, PB_INN, MSP_INN, GONE_INN, FAR_INN = "7801000012", "7801000020", "7805000060", "7803000040", "7804000050"
WON_INN = "7810000072"  # a winner of the organizers' data: the `gap` list
DUMP_AT = 1_789_000_000.0


def bo_body(inn: str, okved: str = "17.12", region: str = "САНКТ-ПЕТЕРБУРГ") -> str:
    return bo_nalog.flatten(inn, {"content": [{
        "inn": f"<strong>{inn}</strong>", "shortName": 'АО "БУМАГА"', "ogrn": "1027800000011",
        "statusCode": "ACTIVE", "statusDate": "2002-10-01", "okved2": okved, "region": region}]})


def pb_body(inn: str) -> str:
    row = {"inn": inn, "namec": 'ПАО "КАРТОН"', "ogrn": "1027800000022", "okved2main": "17.12"}
    vyp = {"НаимЮЛСокр": 'ПАО "КАРТОН"', "ОГРН": "1027800000022", "КПП": "470301001", "КодОКВЭД": "17.12",
           "НаимОКВЭД": "Производство бумаги и картона", "sulst_name_ex": "Действующая организация",
           "rsmpcategory": "0"}
    return pb_nalog.flatten(row, vyp)


def page(url: str, body: str, at: float = 1_790_000_000.0) -> CachedPage:
    return CachedPage(url=url, body=body, fetched_at=at, status=200)


def test_inns_of_the_fixture_pass_the_checksum():
    from src.contracts import inn_is_valid

    assert all(inn_is_valid(i) for i in (BO_INN, PB_INN, MSP_INN, GONE_INN, FAR_INN, WON_INN))


def test_large_row_says_only_what_the_page_says():
    bo = pool.large_row(BO_INN, "bo", page(bo_nalog.search_url(BO_INN), bo_body(BO_INN)), DUMP_AT)
    assert bo is not None and (bo["name"], bo["okved_main"], bo["region_code"]) == ('АО "БУМАГА"', "17.12", "78")
    assert bo["msp_checked_at"] == DUMP_AT and bo["url"] == bo_nalog.search_url(BO_INN)
    pb = pool.large_row(PB_INN, "pb", page(pb_nalog.card_url(PB_INN), pb_body(PB_INN)))
    assert pb is not None and (pb["name"], pb["okved_main"], pb["region_code"]) == ('ПАО "КАРТОН"', "17.12", "47")
    assert bo["list"] == "candidates" and pool.large_active(bo) and pool.large_active(pb)
    lo = pool.large_row(FAR_INN, "bo", page("u", bo_body(FAR_INN, region="ЛЕНИНГРАДСКАЯ")), None, "gap")
    assert lo is not None and (lo["region_code"], lo["list"], lo["msp_checked_at"]) == ("47", "gap", None)
    far = pool.large_row(FAR_INN, "bo", page(bo_nalog.search_url(FAR_INN), bo_body(FAR_INN, region="МОСКВА")))
    assert far is not None and far["region_code"] is None  # not a region of src/regions.py: no code is guessed
    empty = page(bo_nalog.search_url(GONE_INN), bo_nalog.flatten(GONE_INN, {"content": []}))
    assert pool.large_row(GONE_INN, "bo", empty) is None  # the source does not name the company: no row
    assert pool.large_row("7801000013", "bo", page("u", bo_body("7801000013"))) is None  # INN fails the checksum


@pytest.fixture
def pool_dir(tmp_path: Path) -> Path:
    """A pool with the `large` table only (the registry dump is not needed to offer companies outside it)."""
    out = tmp_path / "pool"
    out.mkdir()
    (out / "manifest.json").write_text(json.dumps({"fetched_at": DUMP_AT}), encoding="utf-8")
    cache = SourceCache(tmp_path / "collected.sqlite")
    cache.put(bo_nalog.search_url(BO_INN), bo_body(BO_INN), fetched_at=1_790_000_000.0)
    cache.put(bo_nalog.search_url(PB_INN), bo_nalog.flatten(PB_INN, {"content": []}))
    cache.put(pb_nalog.card_url(PB_INN), pb_body(PB_INN), fetched_at=1_790_000_100.0)
    cache.put(bo_nalog.search_url(GONE_INN), bo_nalog.flatten(GONE_INN, {"content": []}))
    cache.put(L.miss_url(GONE_INN), "query.inn: x\nanswer.inn_rows: 0", status=404)
    cache.put(bo_nalog.search_url(WON_INN), bo_body(WON_INN), fetched_at=1_790_000_000.0)
    cache.put(bo_nalog.search_url(MSP_INN), bo_body(MSP_INN))
    lists = {"gap": [WON_INN, GONE_INN], "candidates": [BO_INN, PB_INN, FAR_INN, MSP_INN]}
    stats = L.build(lists, cache, out / "large.parquet", in_registry={MSP_INN})
    # GONE: neither source names it; FAR: not asked yet; MSP: the dump has its record — it is not «не МСП»
    assert stats == {"asked": 6, "rows": 3, "bo": 2, "pb": 1, "with_okved": 3, "with_region": 3}
    return out


def _suppliers(tmp_path: Path, inns: list[str]) -> Path:
    out = tmp_path / "processed"
    out.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE s AS SELECT unnest(?) AS inn", [inns])
    con.execute(f"COPY s TO '{out.as_posix()}/suppliers.parquet' (FORMAT parquet)")
    con.close()
    return out


def test_from_pool_offers_a_company_outside_the_registry_with_evidence(pool_dir, tmp_path):
    cache = SourceCache(tmp_path / "product.sqlite")  # empty: the pages come from the table, not the network
    q = Query(query_id="q", text="бумага", okpd2=["17.12.14.110"], region="Санкт-Петербург")
    found, relation, checked = discover_mod.from_pool(
        q, q.okpd2, predicted=False, weights={"product_fit": 0.7, "geography": 0.3},
        processed=_suppliers(tmp_path, [MSP_INN]), cache=cache, pool_dir=pool_dir)
    # own region first; WON_INN has the same OKVED but is of the `gap` list — picked by its wins, never offered
    assert checked is True and [m.counterparty.inn for m in found] == [BO_INN, PB_INN]
    by = {m.counterparty.inn or "": m for m in found}
    for inn, m in by.items():
        assert check_counterparty(m.counterparty, cache.page_text) == [], inn
        assert m.origin == "discovered" and relation[inn] == "class" and m.counterparty.is_msp is False
    bo, pb = by[BO_INN], by[PB_INN]
    assert bo.counterparty.region == "Санкт-Петербург" and bo.counterparty.name == 'АО "БУМАГА"'
    msp = [e for e in bo.counterparty.evidence if e.field == "msp"]
    assert [(e.url, e.value) for e in msp] == [(msp_dump.page_url(BO_INN), "inn_rows: 0")]
    assert msp[0].fetched_at.timestamp() == DUMP_AT  # the time the dump was downloaded, not «now»
    geo = {c.name: c for c in bo.components}["geography"]
    assert geo.value == 1.0 and geo.explanation.startswith("ГИР БО: ")
    # the card states «not in the registry» itself: its own Evidence, no second one from the dump
    assert [e.url for e in pb.counterparty.evidence if e.field == "msp"] == [pb_nalog.card_url(PB_INN)]
    assert {c.name: c for c in pb.components}["geography"].explanation.startswith("Карточка ФНС: ")

    known = _suppliers(tmp_path / "k", [BO_INN, PB_INN])
    again, _, _ = discover_mod.from_pool(q, q.okpd2, predicted=False, weights={"product_fit": 1.0}, processed=known,
                                         cache=cache, pool_dir=pool_dir)
    assert again == []  # already suppliers in the organizers' data: not «новые»


def test_large_pages_keep_a_newer_answer_and_a_dump_record(pool_dir, tmp_path):
    cache = SourceCache(tmp_path / "product.sqlite")
    newer = bo_body(BO_INN, okved="46.49", region="ЛЕНИНГРАДСКАЯ").replace("ACTIVE", "LIQUIDATION_STAGE")
    cache.put(bo_nalog.search_url(BO_INN), newer, fetched_at=1_790_500_000.0)
    cache.put(msp_dump.page_url(BO_INN), f"row.{BO_INN}.inn: {BO_INN}", fetched_at=DUMP_AT)
    cache.put(pb_nalog.card_url(PB_INN), "search.inn: old", fetched_at=1_780_000_000.0)  # older than the table
    rows = pool.large_pages([BO_INN, PB_INN, "not-an-inn"], cache, pool_dir=pool_dir)
    assert list(rows) == [BO_INN, PB_INN]
    row = rows[BO_INN]  # the whole row is read from the newer answer, not its body alone
    assert (row["body"], row["fetched_at"], row["region_code"], row["okved_main"], row["status"]) == (
        newer, 1_790_500_000.0, "47", "46.49", "LIQUIDATION_STAGE")
    assert not pool.large_active(row) and cache.page_text(bo_nalog.search_url(BO_INN)) == newer
    # a record of the dump under the address of «no record»: never overwritten, and «не МСП» is not claimed
    assert cache.page_text(msp_dump.page_url(BO_INN)) == f"row.{BO_INN}.inn: {BO_INN}"
    assert row["msp_checked_at"] is None and discover_mod.from_large(row).is_msp is None
    assert cache.page_text(pb_nalog.card_url(PB_INN)) == pb_body(PB_INN)  # the older page is replaced
    assert cache.page_text(msp_dump.page_url(PB_INN)) == pool.msp_absent_page(PB_INN)

    q = Query(query_id="q", text="бумага", okpd2=["17.12.14.110"], region="Санкт-Петербург")
    found, _, _ = discover_mod.from_pool(q, q.okpd2, predicted=False, weights={"product_fit": 1.0},
                                         processed=_suppliers(tmp_path, [MSP_INN]), cache=cache, pool_dir=pool_dir)
    assert [m.counterparty.inn for m in found] == [PB_INN]  # the newer answer: in liquidation — not offered

    cache.put(bo_nalog.search_url(BO_INN), bo_nalog.flatten(BO_INN, {"content": []}), fetched_at=1_790_600_000.0)
    assert list(pool.large_pages([BO_INN], cache, pool_dir=pool_dir)) == []  # the source no longer names it
    assert pool.large_candidates(["17.12"], regions=["47"], pool_dir=pool_dir) == [
        {"inn": PB_INN, "code": "17.12", "main": True}]
    assert pool.large_candidates(["46"], pool_dir=pool_dir) == []
    assert WON_INN not in {r["inn"] for r in pool.large_candidates(["17"], pool_dir=pool_dir)}  # the `gap` list
    gone = pool.large_row(BO_INN, "bo", page("u", bo_body(BO_INN).replace("ACTIVE", "LIQUIDATION_STAGE")))
    pq.write_table(pa.Table.from_pylist([gone], schema=pool.LARGE_SCHEMA), tmp_path / "large.parquet")
    assert pool.large_candidates(["17"], pool_dir=tmp_path) == []  # in liquidation: in the table, not offered
    assert pool.large_candidates(["17"], pool_dir=tmp_path / "none") == []  # no table: the pool is the dump alone
    with pytest.raises(ValueError):
        pool.large_candidates(["17'; --"], pool_dir=pool_dir)


def test_collect_is_resumable_and_stops_at_a_refusal(tmp_path, monkeypatch):
    cache = SourceCache(tmp_path / "c.sqlite")
    asked: list[str] = []

    def bo_fetch(inn: str, cache: SourceCache) -> CachedPage:
        asked.append(f"bo:{inn}")
        if inn == FAR_INN:
            raise httpx.ConnectTimeout("timeout")
        body = bo_body(inn) if inn == BO_INN else bo_nalog.flatten(inn, {"content": []})
        return cache.put(bo_nalog.search_url(inn), body)

    def card(inn: str, cache: SourceCache) -> CachedPage:
        asked.append(f"pb:{inn}")
        if inn == GONE_INN:
            raise pb_nalog.NotFound(inn)
        return cache.put(pb_nalog.card_url(inn), pb_body(inn))

    monkeypatch.setattr(bo_nalog, "fetch", bo_fetch)
    monkeypatch.setattr(pb_nalog, "fetch_card", card)
    inns = [BO_INN, PB_INN, GONE_INN, FAR_INN]
    st = L.collect(inns, cache, egrul=True, rounds=2)
    assert (st["bo_found"], st["bo_no_row"], st["pb_found"], st["pb_no_row"]) == (1, 2, 1, 1)
    assert st["bo_error:ConnectTimeout"] == 1 and asked.count(f"bo:{FAR_INN}") == 2  # a timeout is retried
    assert asked.count(f"bo:{BO_INN}") == 1 and asked.count(f"pb:{GONE_INN}") == 1  # answers are asked once
    assert L.source_page(GONE_INN, cache) is None and L.source_page(PB_INN, cache)[0] == "pb"

    asked.clear()
    monkeypatch.setattr(bo_nalog, "fetch", lambda inn, cache: (_ for _ in ()).throw(bo_nalog.Blocked("403")))
    st = L.collect(inns, cache, egrul=True, rounds=3)
    assert asked == [] and st["stopped_bo"] == 1 and st["bo_not_asked"] == 1  # one refusal: no second round

    def captcha(inn: str, cache: SourceCache) -> CachedPage:
        asked.append(f"pb:{inn}")
        raise pb_nalog.CaptchaRequired("captcha")

    monkeypatch.setattr(bo_nalog, "fetch", bo_fetch)
    monkeypatch.setattr(pb_nalog, "fetch_card", captcha)
    other = SourceCache(tmp_path / "d.sqlite")
    st = L.collect([PB_INN, GONE_INN], other, egrul=True, rounds=3)
    assert asked.count(f"pb:{PB_INN}") == 1 and f"pb:{GONE_INN}" not in asked and st["stopped_pb"] == 1


@pytest.mark.parametrize("refusal", [
    httpx.HTTPStatusError("403", request=httpx.Request("POST", pb_nalog.BASE),
                          response=httpx.Response(403, request=httpx.Request("POST", pb_nalog.BASE))),
    httpx.HTTPStatusError("429", request=httpx.Request("POST", pb_nalog.BASE),
                          response=httpx.Response(429, request=httpx.Request("POST", pb_nalog.BASE))),
    ValueError("not JSON: a protection page"),
])
def test_collect_stops_pb_at_a_refusal_that_is_not_a_captcha(tmp_path, monkeypatch, refusal):
    cache = SourceCache(tmp_path / "c.sqlite")
    asked: list[str] = []
    monkeypatch.setattr(pb_nalog.PACER, "block", lambda seconds=None: asked.append("block"))
    monkeypatch.setattr(bo_nalog, "fetch", lambda inn, cache: cache.put(
        bo_nalog.search_url(inn), bo_nalog.flatten(inn, {"content": []})))

    def card(inn: str, cache: SourceCache) -> CachedPage:
        asked.append(inn)
        raise refusal

    monkeypatch.setattr(pb_nalog, "fetch_card", card)
    st = L.collect([PB_INN, GONE_INN, FAR_INN], cache, egrul=True, rounds=3)
    assert asked == [PB_INN, "block"] and st["stopped_pb"] == 1 and st["pb_not_asked"] == 3  # one request, one round


def test_collect_stops_a_dead_source_after_errors_in_a_row(tmp_path, monkeypatch):
    cache = SourceCache(tmp_path / "c.sqlite")
    asked: list[str] = []

    def dead(inn: str, cache: SourceCache) -> CachedPage:
        asked.append(inn)
        raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(bo_nalog, "fetch", dead)
    monkeypatch.setattr(L, "MAX_ERRORS", 3)
    st = L.collect([BO_INN, PB_INN, GONE_INN, FAR_INN, WON_INN], cache, egrul=False, rounds=3)
    assert len(asked) == 3 and st["stopped_bo"] == 1  # not 5 INNs x 3 rounds


class _Store:
    def participations(self, as_of):
        rows = [(1, WON_INN, True), (2, WON_INN, True), (2, MSP_INN, False), (3, MSP_INN, True), (4, GONE_INN, True),
                (5, "780100001234", True),  # a sole trader: never in the gap list of legal entities
                (6, "7801000013", True)]  # a legal entity whose INN fails the checksum: never asked
        return pa.table({"lot_id": [r[0] for r in rows], "inn": [r[1] for r in rows],
                         "customer_inn": ["7800000000"] * len(rows), "is_winner": [r[2] for r in rows]})


def test_lists_and_coverage(pool_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(L, "get_store", lambda: _Store())
    pq.write_table(pa.table({"inn": [MSP_INN]}), pool_dir / "companies.parquet")
    fns = tmp_path / "fns_inns.parquet"
    pq.write_table(pa.table({"inn": [BO_INN, PB_INN, FAR_INN, MSP_INN, "5001000041", "7801000013"],
                             "employees": [300.0, 300.0, 10.0, 900.0, 900.0, 900.0],
                             "income": [None, None, 3e9, None, None, None]}), fns)
    con = L.connect(pool_dir, fns)
    assert L.gap(con, 10) == [(WON_INN, 2), (GONE_INN, 1)] and L.gap(con, 1) == [(WON_INN, 2)]
    assert L.candidates(con) == [BO_INN, PB_INN, FAR_INN]  # large, home INN, not a supplier, not in the registry
    cov = L.coverage(con, pool_dir / "large.parquet")
    assert (cov["wins"], cov["wins_in_sme_registry"], cov["wins_outside_sme_registry"], cov["wins_in_large"]) == (
        6, 1, 5, 2)
    assert cov["share_of_wins_outside_registry_now_in_pool"] == 0.4 and cov["share_of_all_wins_in_pool"] == 0.5
    assert cov["winners_in_large"] == 1 and (cov["gap_rows"], cov["candidates_rows"]) == (1, 2)
    assert cov["candidates_offerable"] == 2 and cov["candidates_among_organizers_suppliers"] == 0
    assert (cov["large_rows"], cov["large_from_bo"], cov["large_from_pb"], cov["large_with_okved"]) == (3, 2, 1, 3)
