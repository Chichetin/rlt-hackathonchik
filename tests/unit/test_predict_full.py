"""`make predict` full mode (#229): synchronous check, only verified rows, text over a disagreeing code."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from src.contracts import Counterparty, CounterpartyMatch, Query, ScoreComponent, inn_is_valid
from src.enrich import discover as discover_mod
from src.enrich import verify as V
from src.enrich.enrich import from_card
from src.ingest import predict as P
from src.sources import eis_rnp, pb_nalog, rmsp
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures"
TESTSET = FIX / "testset"
UL = "7804428656"  # real pb card and a real «no results» RNP page (tests/fixtures/pb_nalog, eis_rnp)
PAPER_SUPPLIERS = {"7801000012", "7801000020", "780100000090", "4701000038", "470100000088"}


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _ok(cp: Counterparty) -> V.Verdict:
    """Stub transport of the check: every candidate with an INN gets a name and an active status."""
    if cp.inn is None:
        return V.Verdict(cp, False, "ИНН не подтверждён")
    name = cp.name if not cp.name.startswith("ИНН ") else f"ООО Тест {cp.inn}"
    return V.Verdict(cp.model_copy(update={"name": name, "status": "Действующая организация", "in_rnp": False}),
                     True, "проверен: Действующая организация; в РНП нет")


@pytest.fixture(autouse=True)
def _no_pool(monkeypatch):
    """The developer's built pool (data/external/rsmp/pool) never answers a unit test; a test sets its own."""
    monkeypatch.setattr(discover_mod, "pool_source", lambda: (None, None))


@pytest.fixture
def card_cache(tmp_path: Path) -> SourceCache:
    c = SourceCache(tmp_path / "s.sqlite")
    c.put(pb_nalog.card_url(UL), (FIX / "pb_nalog" / f"{UL}.txt").read_text(encoding="utf-8"))
    c.put(eis_rnp.search_url(UL), (FIX / "eis_rnp" / f"inn_{UL}_no_results.html").read_text(encoding="utf-8"))
    return c


# --- verify ------------------------------------------------------------------------------------------

def test_verify_real_card_and_rnp_from_cache(card_cache):
    v = V.verify(Counterparty(inn=UL, name=f"ИНН {UL}", stats={"lots_won": 3}), card_cache, offline=True)
    assert v.ok, v.why
    cp = v.counterparty
    assert cp.name == 'ООО "БРАСС"' and cp.status == "Действующая организация" and cp.in_rnp is False
    assert cp.stats == {"lots_won": 3}  # organizers' data kept
    assert {"status", "rnp"} <= {e.field for e in cp.evidence}
    assert "в РНП нет" in v.why


def test_verify_without_card_is_not_shown(tmp_path):
    v = V.verify(Counterparty(inn=UL, name=f"ИНН {UL}"), SourceCache(tmp_path / "e.sqlite"), offline=True)
    assert not v.ok and "не подтверждён" in v.why


def test_verify_rejects_liquidated_rnp_and_bad_inn(card_cache):
    page = card_cache.get(pb_nalog.card_url(UL))
    assert page is not None
    dead = page.body.replace("Действующая организация", "Ликвидирована")
    card_cache.put(pb_nalog.card_url(UL), dead)
    v = V.verify(Counterparty(inn=UL, name="x"), card_cache, offline=True)
    assert not v.ok and "Ликвидирована" in v.why

    card_cache.put(pb_nalog.card_url(UL), page.body)
    rnp = eis_rnp.RnpResult(True, [])
    v = V.verify(Counterparty(inn=UL, name="x"), card_cache, offline=True, rnp_fn=lambda inn, c: rnp)
    assert not v.ok and v.why == "в РНП ЕИС"
    assert not V.verify(Counterparty(inn=None, name="x"), card_cache).ok


def test_verify_rnp_down_is_reported_not_raised(card_cache):
    def down(inn, c):
        raise eis_rnp.Blocked("403")

    v = V.verify(Counterparty(inn=UL, name="x"), card_cache, offline=True, rnp_fn=down)
    assert not v.ok and V.RNP in v.down
    v = V.verify(Counterparty(inn=UL, name="x"), card_cache, offline=True, rnp=False)
    assert v.ok and "РНП не проверялся" in v.why


def test_verify_drops_status_whose_evidence_fails(card_cache):
    page = card_cache.get(pb_nalog.card_url(UL))
    assert page is not None
    card = from_card(UL, page)
    forged = card.model_copy(update={"status": "Действующая организация", "evidence": [
        e.model_copy(update={"value": "Действует без сомнений"}) if e.field == "status" else e
        for e in card.evidence]})
    v = V.verify(Counterparty(inn=UL, name="x"), card_cache, offline=True, enrich_fn=lambda *a, **k: forged)
    assert not v.ok and "статус не подтверждён" in v.why


@pytest.mark.parametrize(("status", "active"), [
    ("Действующая организация", True), ("действующая", True), ("Ликвидирована", False),
    ("банкротство (ГИР БО: BANKRUPT)", False), ("В процессе реорганизации", None), (None, None)])
def test_is_active(status, active):
    assert V.is_active(status) is active


# --- code vs text --------------------------------------------------------------------------------------

def test_code_disagrees():
    known = frozenset({"17.12", "11.07", "20.30"})
    paper = {"17": 0.9, "20": 0.1}
    assert P.code_disagrees(["17.12.14.110"], paper, "17.12", known) is None
    assert P.code_disagrees([], paper, "17.12", known) is None
    assert P.code_disagrees(["26.70.22.150"], {}, None, known) is None  # the text matched nothing
    why = P.code_disagrees(["26.70.22.150"], paper, "17.12", known) or ""
    assert "код и текст расходятся" in why and "0%" in why and "класс 17.12 (90%)" in why
    assert P.code_disagrees(["20.30.11.000"], paper, "17.12", known) is None  # 10 % is enough to keep the code
    assert "новый" in (P.code_disagrees(["17.29.11.000"], paper, "17.12", known) or "")
    assert P.code_disagrees(["17.29.11.000"], paper, "17.12", None) is None  # no data: nothing to call new


def test_section_shares_from_text_matches(search_data):
    from src.match import search as search_mod

    hist = search_mod.search(Query(query_id="q", text="бумага офисная"), k=10)
    shares = P.section_shares(hist, search_data)
    assert shares and abs(sum(shares.values()) - 1) < 1e-9 and max(shares, key=lambda k: shares[k]) == "17"
    assert P.section_shares([], search_data) == {}


# --- full run ------------------------------------------------------------------------------------------

def _seed_cards(cache: SourceCache, inns: list[str], nameless: frozenset[str] = frozenset()) -> None:
    """A pb card and a «no results» RNP page per INN: the real recorded pages with the INN replaced (a
    stub of the transport); `nameless` — cards without any name line."""
    card = (FIX / "pb_nalog" / f"{UL}.txt").read_text(encoding="utf-8")
    rnp = (FIX / "eis_rnp" / f"inn_{UL}_no_results.html").read_text(encoding="utf-8")
    for inn in inns:
        body = card.replace(UL, inn)
        if inn in nameless:
            body = "\n".join(line for line in body.splitlines()
                              if not line.startswith(("search.namec", "search.namep", "vyp.НаимЮЛ")))
        cache.put(pb_nalog.card_url(inn), body)
        cache.put(eis_rnp.search_url(inn), rnp.replace(UL, inn))


def _supplier_inns(processed: Path) -> list[str]:
    import duckdb

    return [r[0] for r in duckdb.connect().execute(
        "SELECT DISTINCT inn FROM read_parquet(?) WHERE inn IS NOT NULL", [str(processed / "suppliers.parquet")]
    ).fetchall()]


def test_full_mode_every_row_has_name_and_status(search_data, tmp_path, monkeypatch):
    """Acceptance (#229): the real check on a stubbed transport — every row of both blocks has a checked
    name and status, cache only (offline): no network."""
    cache_path = tmp_path / "sources.sqlite"
    monkeypatch.setenv("SOURCE_CACHE", str(cache_path))
    inns = _supplier_inns(search_data)
    _seed_cards(SourceCache(cache_path), inns)
    out = tmp_path / "out.csv"
    stats = P.run(TESTSET, out, top=5, offline=True, mode="full")
    rows = _rows(out)
    assert {r["lot_id"] for r in rows} == {"9001", "9002", "9003"}
    shown = [r for r in rows if r["inn"]]
    assert shown and all(r["name"] and not r["name"].startswith("ИНН ") for r in shown)
    assert all(r["status"] == "Действующая организация" and r["rnp"] == "нет" for r in shown)
    assert all(r["check"].startswith("проверен") for r in shown)
    assert {r["inn"] for r in shown if r["lot_id"] == "9001"} & PAPER_SUPPLIERS
    report = _rows(P.report_path(out))
    assert [r["lot_id"] for r in report] == ["9001", "9002", "9003"]
    assert all(r["mode"] == "full" and int(r["history_checked"]) >= int(r["history_shown"]) for r in report)
    assert stats["rows"] == stats["rows_checked"] == len(shown)
    again = P.run(TESTSET, out, top=5, offline=True, mode="full")
    assert again["skipped"] == 3 and len(_rows(P.report_path(out))) == 3


def test_full_mode_drops_a_company_without_a_checked_name(search_data, tmp_path, monkeypatch):
    cache_path = tmp_path / "sources.sqlite"
    monkeypatch.setenv("SOURCE_CACHE", str(cache_path))
    inns = _supplier_inns(search_data)
    _seed_cards(SourceCache(cache_path), inns, nameless=frozenset({"7801000012"}))
    out = tmp_path / "out.csv"
    P.run(TESTSET, out, top=5, offline=True, mode="full")
    assert "7801000012" not in {r["inn"] for r in _rows(out)}  # the only water supplier: no name → no row
    assert any(r["inn"] for r in _rows(out) if r["lot_id"] == "9001")


def test_verify_offline_never_asks_rnp(card_cache, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("RNP request in an offline run")

    monkeypatch.setattr(eis_rnp, "_get", no_network)
    page = card_cache.get(pb_nalog.card_url(UL))
    assert page is not None
    bare = SourceCache(card_cache.path.with_name("bare.sqlite"))
    bare.put(pb_nalog.card_url(UL), page.body)
    v = V.verify(Counterparty(inn=UL, name="x"), bare, offline=True)
    assert not v.ok and "РНП не проверен" in v.why


def test_full_mode_shows_only_verified(search_data, tmp_path):
    def nobody(cp: Counterparty) -> V.Verdict:
        return V.Verdict(cp, False, "статус не подтверждён", [V.FNS])

    out = tmp_path / "o.csv"
    P.run(TESTSET, out, top=5, offline=True, mode="full", verify_fn=nobody)
    rows = _rows(out)
    assert all(not r["inn"] for r in rows) and {r["lot_id"] for r in rows} == {"9001", "9002", "9003"}
    assert "ни один не прошёл проверку" in next(r["note"] for r in rows if r["lot_id"] == "9001")
    assert all(V.FNS in r["unavailable"] for r in _rows(P.report_path(out)) if int(r["history_checked"]))


def test_code_text_mismatch_searches_by_text(search_data, tmp_path):
    d = tmp_path / "t"
    shutil.copytree(TESTSET, d)
    (d / "ТРУ_24-25.csv").write_text('"lot_id";"product_name";"okpd2_code"\n9001;Бумага А4;"26.70.22.150"\n',
                                     encoding="utf-8")
    (d / "Извещения_24-25.csv").write_text(
        (TESTSET / "Извещения_24-25.csv").read_text(encoding="utf-8").splitlines()[0] + "\n"
        + (TESTSET / "Извещения_24-25.csv").read_text(encoding="utf-8").splitlines()[1] + "\n", encoding="utf-8")
    out = tmp_path / "o.csv"
    P.run(d, out, top=5, offline=True, mode="full", verify_fn=_ok)
    rows = _rows(out)
    assert all("код и текст расходятся" in r["note"] and "поочерёдно" in r["note"] for r in rows)
    assert {r["inn"] for r in rows if r["block"] == "История"} & PAPER_SUPPLIERS
    rep = _rows(P.report_path(out))[0]
    assert rep["searched_by"] == "код и текст поочерёдно" and rep["text_class"] == "17.12"


def _new(inn: str, fit: float) -> CounterpartyMatch:
    comps = [ScoreComponent(name="product_fit", value=fit, weight=1.0, explanation="x")]
    return CounterpartyMatch(counterparty=Counterparty(inn=inn, name=f"ООО {inn}"), score=fit, components=comps,
                             origin="discovered")


NEW_INNS = ["7805772436", "7807032363", "7810400592", "7811777270", "4703173460", "7804428656"]


@pytest.mark.parametrize(("fits", "shown"), [
    ([1.0, 0.8, 0.8, 0.6, 0.4], 4),  # strong < 5 → 0.6 joins; 0.4 never
    ([1.0, 1.0, 1.0, 0.8, 0.8, 0.6], 5),  # 5 strong → no 0.6
])
def test_new_block_product_fit_steps(search_data, monkeypatch, fits, shown):
    pool = [_new(inn, f) for inn, f in zip(NEW_INNS, fits, strict=False)]
    monkeypatch.setattr(discover_mod, "discover", lambda *a, **k: discover_mod.Discovery(
        matches=pool[:2], pool=pool, history_checked=True))
    lot = P.load_testset(TESTSET).lots[0]
    rows, _, rep = P.full_recommend(lot, 10, offline=True, verify_fn=_ok)
    new = [r.match for r in rows if r.match.origin == "discovered"]
    assert len(new) == shown and all(P._fit(m) >= 0.6 for m in new)
    assert rep.counts["new_found"] == len(pool) and rep.counts["new_shown"] == shown


def test_full_discover_lifts_budgets(search_data, monkeypatch):
    seen = {}

    def spy(query: Query, history, **kw):
        seen.update(kw)
        return discover_mod.Discovery()

    monkeypatch.setattr(discover_mod, "discover", spy)
    P.full_recommend(P.load_testset(TESTSET).lots[0], 5, offline=True, verify_fn=_ok)
    assert seen["max_requests"] is None and seen["pages"] == P.FULL_PAGES and seen["predicted_class"] is True
    assert seen["budget_s"] >= 60


# --- rmsp pages in discovery ----------------------------------------------------------------------------

def test_rmsp_page_url_keeps_page_one_key():
    assert rmsp.search_url("17.23", "78") == rmsp.search_url("17.23", "78", page=1)
    assert rmsp.search_url("17.23", "78", page=2).endswith("&page=2")
    with pytest.raises(ValueError):
        rmsp.search_url("17.23", "78", page=0)


def test_discover_reads_next_pages_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(rmsp, "_blocked_until", 0.0)
    cache = SourceCache(tmp_path / "c.sqlite")
    rmsp.seed(FIX / "rmsp", cache)  # page 1 of 17.23 / 78: 10 rows, a full page
    extra = {"inn": "7802000008", "ogrn": "1027800000006", "nptype": "UL", "category": 1, "regioncode": "78",
             "is_active": 1, "okved1": "17.23", "okved1name": "Производство бумажных канцелярских принадлежностей",
             "name_ex": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ \"СТРАНИЦА ДВА\""}
    assert inn_is_valid(extra["inn"])
    cache.put(rmsp.search_url("17.23", "78", "UL", 2), rmsp.flatten("17.23", "78", "UL", {"data": [extra]}))
    processed = tmp_path / "p"
    processed.mkdir()
    import duckdb

    con = duckdb.connect()
    con.execute(f"COPY (SELECT '0000000000' AS inn) TO '{processed.as_posix()}/suppliers.parquet' (FORMAT parquet)")
    con.close()
    q = Query(query_id="q", text="папки", okpd2=["17.23.13.130"])
    w = {"product_fit": 1.0}
    one = discover_mod.discover(q, cache=cache, weights=w, processed=processed, offline=True)
    many = discover_mod.discover(q, cache=cache, weights=w, processed=processed, offline=True, pages=3,
                                 max_requests=None)
    assert extra["inn"] not in {m.counterparty.inn for m in one.pool}
    assert extra["inn"] in {m.counterparty.inn for m in many.pool}
    assert any("стр. 2" in r for r in many.requests)
    assert not any("стр. 3" in r for r in many.requests)  # page 2 had one row: the last page


# --- offline МСП pool (#185) as a source of «Новые» ---------------------------------------------------------

@pytest.fixture(scope="module")
def pool_dir(tmp_path_factory) -> Path:
    import zipfile

    from src.db import pool
    from src.sources import msp_dump

    tmp = tmp_path_factory.mktemp("pool")
    zip_path = tmp / "dump.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("a.xml", (FIX / "msp_dump" / "sample.xml").read_text(encoding="utf-8"))
    msp_dump.build(zip_path, tmp / "parts", workers=1)
    pool.build(tmp / "parts", tmp / "pool", fetched_at=1_789_000_000.0)
    return tmp / "pool"


def _suppliers(tmp_path: Path, inns: list[str]) -> Path:
    import duckdb

    out = tmp_path / "processed"
    out.mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE s AS SELECT unnest(?) AS inn", [inns])
    con.execute(f"COPY s TO '{out.as_posix()}/suppliers.parquet' (FORMAT parquet)")
    con.close()
    return out


def test_from_pool_class_company_with_evidence(pool_dir, tmp_path):
    from src.enrich.evidence import check_counterparty

    cache = SourceCache(tmp_path / "c.sqlite")
    q = Query(query_id="q", text="бумага", okpd2=["17.12.14.110"])
    found, relation, checked = discover_mod.from_pool(
        q, q.okpd2, predicted=False, weights={"product_fit": 1.0}, processed=_suppliers(tmp_path, ["7801000012"]),
        cache=cache, pool_dir=pool_dir)
    assert checked is True and [m.counterparty.inn for m in found] == ["0078000013"]
    m = found[0]
    assert P._fit(m) == 1.0 and relation["0078000013"] == "class" and m.origin == "discovered"
    assert check_counterparty(m.counterparty, cache.page_text) == []
    (tmp_path / "k").mkdir()
    known = _suppliers(tmp_path / "k", ["0078000013"])
    again, _, _ = discover_mod.from_pool(q, q.okpd2, predicted=False, weights={"product_fit": 1.0}, processed=known,
                                         cache=cache, pool_dir=pool_dir)
    assert again == []  # already a supplier in organizers' data: not «новая»


def test_from_pool_not_loaded(tmp_path):
    q = Query(query_id="q", text="бумага", okpd2=["17.12.14.110"])
    assert discover_mod.from_pool(q, q.okpd2, predicted=False, weights={}, processed=tmp_path,
                                  cache=SourceCache(tmp_path / "c.sqlite")) == ([], {}, None)


def test_full_mode_reports_pool(search_data, tmp_path, monkeypatch, pool_dir):
    out = tmp_path / "o.csv"
    P.run(TESTSET, out, top=5, offline=True, mode="full", verify_fn=_ok)
    assert all(P.POOL_SOURCE in r["unavailable"] for r in _rows(P.report_path(out)))
    monkeypatch.setattr(discover_mod, "pool_source", lambda: (None, pool_dir))
    cache_path = tmp_path / "sources.sqlite"  # «Новые» go through the real check too (both blocks, #229)
    monkeypatch.setenv("SOURCE_CACHE", str(cache_path))
    _seed_cards(SourceCache(cache_path), [*_supplier_inns(search_data), "0078000013"])
    out2 = tmp_path / "o2.csv"
    P.run(TESTSET, out2, top=5, offline=True, mode="full")
    rep = {r["lot_id"]: r for r in _rows(P.report_path(out2))}
    assert "пул МСП: кандидатов 1" in rep["9001"]["requests"] and P.POOL_SOURCE not in rep["9001"]["unavailable"]
    new = [r for r in _rows(out2) if r["lot_id"] == "9001" and r["block"] == "Новые"]
    assert [r["inn"] for r in new] == ["0078000013"] and new[0]["status"] == "Действующая организация"
    assert new[0]["name"] and not new[0]["name"].startswith("ИНН ") and new[0]["check"].startswith("проверен")


def test_interleave_takes_turns_without_duplicates():
    a = [_new(i, 1.0) for i in ("7805772436", "7807032363", "7810400592")]
    b = [_new(i, 1.0) for i in ("7807032363", "7811777270")]
    assert [m.counterparty.inn for m in P.interleave(a, b)] == ["7805772436", "7807032363", "7811777270",
                                                                "7810400592"]
    assert P.interleave([], b) == b


def test_new_block_steps_by_basis(search_data, monkeypatch):
    """#186 steps: the OKVED class is 0.6 (shown only while strong < 5), wholesale 0.4 — never, whatever
    product_fit the live registry block gives them (1.0 / 0.6 in /api/search)."""
    pool = [_new("7805772436", 1.0), _new("7807032363", 1.0), _new("7810400592", 0.6)]
    relation = {"7805772436": "class", "7807032363": "class", "7810400592": "wholesale"}
    monkeypatch.setattr(discover_mod, "discover", lambda *a, **k: discover_mod.Discovery(
        pool=pool, history_checked=True, relation=relation))
    rows, _, _ = P.full_recommend(P.load_testset(TESTSET).lots[0], 10, offline=True, verify_fn=_ok)
    assert sorted(r.match.counterparty.inn or "" for r in rows if r.match.origin == "discovered") == [
        "7805772436", "7807032363"]


def test_old_format_file_is_moved_aside(search_data, tmp_path):
    out = tmp_path / "o.csv"
    out.write_text("lot_id;reqnum;query_text;okpd2;block;rank;inn;name;score;components;note\n9001;;x;;;;;;;;\n",
                   encoding="utf-8-sig")
    stats = P.run(TESTSET, out, top=3, offline=True, mode="demo")
    assert stats["skipped"] == 0  # the old rows are not taken for finished lots
    assert list(_rows(out)[0]) == list(P.COLUMNS) and len(list(tmp_path.glob("o.old-*.csv"))) == 1
