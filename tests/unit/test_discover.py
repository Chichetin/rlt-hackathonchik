"""Discovery v0 (#16): rmsp.nalog.ru answer → block «Новые», only with Evidence, no network in tests.

`tests/fixtures/rmsp/UL_17.23_78.json` is a real answer of the extended search recorded 01.10
(session tokens and row ids removed, phones and emails replaced with test values of the same shape).
ИП and ФНС-card texts used here are synthetic.
"""

import json
import time
from pathlib import Path

import duckdb
import httpx
import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.contracts import Query, inn_is_valid
from src.enrich import discover as D
from src.enrich.evidence import check_counterparty
from src.sources import pb_nalog, rmsp
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "rmsp"
ANSWER = json.loads((FIX / "UL_17.23_78.json").read_text(encoding="utf-8"))
ACTIVE = ["4703173460", "7805772436", "7807032363", "7810400592", "7811777270"]  # is_active = 1 in the fixture
EXCLUDED = "7805336905"  # is_active = 0: left the registry
WEIGHTS = {"product_fit": 0.35, "experience": 0.2, "reliability": 0.2, "activity": 0.1, "geography": 0.1,
           "price": 0.05}
IP_ROW = {"inn": "500100732259", "ogrn": "318169000190351", "nptype": "IP", "category": 1, "regioncode": "78",
          "is_active": 1, "okved1": "17.23", "okved1name": "Производство бумажных канцелярских принадлежностей",
          "name_ex": "ИВАНОВ ИВАН ИВАНОВИЧ", "dtregistry": "10.11.2018 00:00:00", "phone": "+7(921)1234567",
          "email": "ivanov@example.org", "localityname": "Парголово", "token": "SECRET"}


@pytest.fixture(autouse=True)
def _fast_and_unblocked(monkeypatch):
    monkeypatch.setattr(rmsp, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(rmsp, "_blocked_until", 0.0)


@pytest.fixture
def cache(tmp_path) -> SourceCache:
    c = SourceCache(tmp_path / "sources.sqlite")
    assert rmsp.seed(FIX, c) == 1
    return c


def _processed(tmp_path: Path, inns: list[str]) -> Path:
    """Minimal organizers' data: suppliers.parquet with these INNs."""
    out = tmp_path / "processed"
    out.mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE s AS SELECT unnest(?) AS inn", [inns or ["0000000000"]])
    con.execute(f"COPY s TO '{out}/suppliers.parquet' (FORMAT parquet)")
    con.close()
    return out


def _query(**kw) -> Query:
    return Query(**{"query_id": "q", "text": "папки канцелярские", "okpd2": ["17.23.13.130"], **kw})


def _run(cache, processed, **kw) -> D.Discovery:
    return D.discover(kw.pop("query", _query()), cache=cache, weights=WEIGHTS, processed=processed, **kw)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"network call in an offline path: {request.url}")


# --- rmsp client -------------------------------------------------------------------------------

def test_flatten_keeps_registry_facts_and_no_tokens():
    text = rmsp.flatten("17.23", "78", "UL", {**ANSWER, "data": [{**ANSWER["data"][0], "token": "SECRET", "id": 1}]})
    assert "SECRET" not in text and ".token" not in text and ".id:" not in text
    answer = rmsp.parse(rmsp.flatten("17.23", "78", "UL", ANSWER))
    assert answer.meta["answer.rowCount"] == "16" and answer.meta["query.okved1"] == "17.23"
    assert [r["inn"] for r in answer.rows][:2] == ["4703173460", EXCLUDED]  # registry order kept
    assert answer.rows[0]["name_ex"] == 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "МАРКЛИП"'
    assert [r["inn"] for r in answer.rows if r["is_active"] == "1"] == ACTIVE


def test_ip_row_keeps_no_contacts_and_bad_rows_are_dropped():
    bad_inn = {**ANSWER["data"][0], "inn": "7801000013"}  # fails the checksum
    wrong_len = {**ANSWER["data"][0], "inn": "500100732259"}  # a legal entity cannot have 12 digits
    fl = {**IP_ROW, "nptype": "FL"}
    rows = rmsp.clean_rows({"data": [IP_ROW, bad_inn, wrong_len, fl, "junk"]})
    assert [r["inn"] for r in rows] == ["500100732259"]
    assert set(rows[0]) == {"inn", "ogrn", "nptype", "name_ex", "category", "is_active", "regioncode", "okved1",
                            "okved1name", "dtregistry"}
    text = rmsp.flatten("17.23", "78", "IP", {"data": [IP_ROW]})
    assert "ivanov@example.org" not in text and "1234567" not in text and "Парголово" not in text


def test_search_arguments_are_validated():
    bad = (("17.23&x=1", "78", "UL"), ("17.23", "7 8", "UL"), ("17.23", "78", "FL"), ("", "78", "UL"))
    for okved, region, np in bad:
        with pytest.raises(ValueError):
            rmsp.search_url(okved, region, np)
    assert rmsp.search_url("46.49.33", "47", "IP").endswith("region=47&npType=IP&okved1=46.49.33")


def test_fetch_is_one_post_then_cache(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(200, json=ANSWER)

    c = SourceCache(tmp_path / "c.sqlite")
    page = rmsp.fetch_search("17.23", "78", cache=c, client=_client(handler))
    assert page is not None and page.url == rmsp.search_url("17.23", "78")
    assert calls == ["mode=extended&page=1&region=78&npType=UL&okved1=17.23"]
    assert rmsp.fetch_search("17.23", "78", cache=c, client=_client(_no_network)) == page  # fresh cache
    raw = c.page_text(rmsp.raw_url("17.23", "78"))
    assert raw is not None and "token" not in raw and len(json.loads(raw)["data"]) == 10


def test_offline_reads_only_cache(tmp_path, cache):
    assert rmsp.fetch_search("17.23", "78", cache=cache, client=_client(_no_network), offline=True) is not None
    assert rmsp.fetch_search("46.49.33", "78", cache=cache, client=_client(_no_network), offline=True) is None


@pytest.mark.parametrize("response", [
    httpx.Response(403, text="If you are not a bot"),
    httpx.Response(429, text="slow down"),
    httpx.Response(200, text="<html>captcha</html>"),
    httpx.Response(200, json={"captchaRequired": True}),
])
def test_refusal_stops_further_requests(tmp_path, response):
    c = SourceCache(tmp_path / "c.sqlite")
    with pytest.raises(rmsp.Blocked):
        rmsp.fetch_search("17.23", "78", cache=c, client=_client(lambda r: response))
    assert rmsp.blocked()
    with pytest.raises(rmsp.Blocked):  # the cool-down holds without touching the network
        rmsp.fetch_search("17.23", "47", cache=c, client=_client(_no_network))


def test_server_error_is_not_a_block_and_stale_cache_survives(tmp_path):
    c = SourceCache(tmp_path / "c.sqlite", ttl_s=10)
    old = c.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", ANSWER), fetched_at=1.0)
    page = rmsp.fetch_search("17.23", "78", cache=c, client=_client(lambda r: httpx.Response(500)))
    assert page == old and not rmsp.blocked()
    with pytest.raises(httpx.HTTPStatusError):
        rmsp.fetch_search("17.23", "47", cache=c, client=_client(lambda r: httpx.Response(500)))
    with pytest.raises(ValueError):  # JSON, but not the search answer
        rmsp.fetch_search("17.23", "47", cache=c, client=_client(lambda r: httpx.Response(200, json={"x": 1})))
    assert not rmsp.blocked()


# --- OKPD2 → OKVED ------------------------------------------------------------------------------

def test_okved_plan_class_then_wholesale():
    plan = D.okved_plan(["17.12.14.110", "17.12.14.119"])
    assert [(c.okved, c.okpd_class, c.relation) for c in plan] == [("17.12", "17.12", "class"),
                                                                    ("46.49.33", "17.12", "wholesale")]
    assert [c.okved for c in D.okved_plan(["22.19.60.119"])] == ["22.19", "46.46.2"]  # longest prefix wins
    assert [c.okved for c in D.okved_plan(["22.19.20.110"])] == ["22.19"]  # no wholesale row → class only
    assert [c.okved for c in D.okved_plan(["28.23.25.000"])] == ["28.23", "46.66"]
    assert [c.okved for c in D.okved_plan(["28.99.39.190"])] == ["28.99", "46.69"]
    assert [c.okved for c in D.okved_plan(["81.21.10.000"])] == ["81.21"]  # a service: the class itself


def test_okved_plan_skips_broad_codes_and_limits_classes():
    assert D.okved_plan(["17", "", " "]) == [] and D.okpd_classes(["17", "17.1"]) == []
    plan = D.okved_plan(["17.12.14", "26.20.40", "31.01.11"])
    assert [c.okpd_class for c in plan] == ["17.12", "17.12", "26.20", "26.20"]  # MAX_CLASSES = 2
    assert [c.okved for c in D.okved_plan(["17.12.14", "17.23.13"])] == ["17.12", "46.49.33", "17.23"]  # no repeat


# --- discover() ----------------------------------------------------------------------------------

def test_discover_shows_only_active_new_companies_with_evidence(cache, tmp_path):
    res = _run(cache, _processed(tmp_path, ["7805772436"]), client=_client(_no_network))
    inns = [m.counterparty.inn for m in res.matches]
    assert inns == ["4703173460", "7807032363", "7810400592", "7811777270"]  # active, minus the known one
    assert res.history_checked and res.known == 1 and res.inactive == 5 and res.okpd_classes == ["17.23"]
    for m in res.matches:
        cp = m.counterparty
        assert m.origin == "discovered" and cp.inn and inn_is_valid(cp.inn)
        assert check_counterparty(cp, cache.page_text) == []
        assert {e.field for e in cp.evidence} >= {"inn", "ogrn", "name", "okved", "msp", "region"}
        assert all(e.url == rmsp.search_url("17.23", "78") and e.fetched_at.year == 2026 for e in cp.evidence)
        assert cp.region == "Санкт-Петербург" and cp.is_msp is True and cp.okved == ["17.23"]
        assert cp.status is None and cp.kind is None  # not known from the registry row → not guessed
        assert [c.name for c in m.components] == list(D.COMPONENT_ORDER)
    first = res.matches[0]
    assert first.counterparty.name == 'ООО "МАРКЛИП"' and first.score == 0.45  # 0.35·1.0 + 0.1·1.0
    fit = first.components[0]
    assert fit.value == 1.0 and "ОКВЭД 17.23" in fit.explanation and "классом ОКПД2 запроса 17.23" in fit.explanation
    assert "вероятно производитель" in fit.explanation and "автоматически" not in fit.explanation
    assert fit.explanation.endswith("; совпадение по классу ОКВЭД (4 знака), не по товару")
    assert first.components[1].explanation == "В данных организаторов за 2024–2025 не встречается ни в одной закупке"
    assert "ещё не проверен" in first.components[2].explanation and first.components[2].weight == 0.0


def test_registry_contacts_of_a_legal_entity_pass_the_evidence_check(cache, tmp_path):
    res = _run(cache, _processed(tmp_path, []))
    cp = next(m.counterparty for m in res.matches if m.counterparty.inn == "7805772436")
    assert cp.contacts == {"phone": "+7(812)5550101", "email": "company1@example.org"}
    assert check_counterparty(cp, cache.page_text) == []
    assert len(res.matches) == 5 and res.known == 0


def test_odd_phone_is_not_a_contact_and_legal_form_suffix_is_shortened(tmp_path):
    # shapes seen live on 01.10 (digits replaced): a 5-digit area code, the legal form at the end of the name
    row = {**ANSWER["data"][2], "phone": "+7(96507)00000",
           "name_ex": '" ЭЛАСТИС " ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ'}
    c = SourceCache(tmp_path / "c.sqlite")
    c.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", {"data": [row]}))
    cp = _run(c, _processed(tmp_path, [])).matches[0].counterparty
    assert cp.contacts == {"email": "company1@example.org"} and "phone" not in {e.field for e in cp.evidence}
    assert cp.name == 'ООО " ЭЛАСТИС "' and check_counterparty(cp, c.page_text) == []
    assert D.display_name({"nptype": "UL", "name_ex": 'АКЦИОНЕРНОЕ ОБЩЕСТВО "НПО "ИЗОЛЯТОР"'}) == 'АО "НПО "ИЗОЛЯТОР"'
    assert D.display_name({"nptype": "UL", "name_ex": "ФОНД РАЗВИТИЯ"}) == "ФОНД РАЗВИТИЯ"


def test_without_organizers_data_nothing_is_called_new(cache, tmp_path):
    res = _run(cache, tmp_path / "no-processed")
    assert res.history_checked is False and len(res.matches) == 5
    assert any("сверка с историей закупок не выполнена" in n for n in res.notes)
    as_of = _run(cache, _processed(tmp_path, []), query=_query(as_of="2025-07-01"))  # no suppliers_<as_of>
    assert as_of.history_checked is False


def test_offline_without_cache_is_empty_and_says_why(tmp_path):
    res = _run(SourceCache(tmp_path / "empty.sqlite"), _processed(tmp_path, []), offline=True,
               client=_client(_no_network))
    assert res.matches == [] and res.requests and all("нет в кеше" in r for r in res.requests)


def test_query_without_okpd2_and_history_does_not_search(cache, tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_PREDICTED_CLASS", "1")
    res = _run(cache, _processed(tmp_path, []), query=_query(okpd2=[]), client=_client(_no_network))
    assert res.matches == [] and res.requests == [] and "не определён" in res.notes[0]


@pytest.mark.parametrize("flag", [None, "0", "true"])  # only "1" switches the predicted class on
def test_predicted_class_is_off_by_default_no_block_no_network(search_data, tmp_path, monkeypatch, flag):
    from src.match.search import search
    if flag is None:
        monkeypatch.delenv("DISCOVERY_PREDICTED_CLASS", raising=False)
    else:
        monkeypatch.setenv("DISCOVERY_PREDICTED_CLASS", flag)
    q = Query(query_id="q", text="бумага А4")  # no OKPD2: search() would predict 17.12
    history = search(q)
    assert D.stated_class(history) == "17.12"
    c = SourceCache(tmp_path / "c.sqlite")
    rmsp.seed(FIX / "e2e", c)  # even a warm cache is not used for a predicted class
    res = D.discover(q, history, cache=c, weights=WEIGHTS, processed=search_data, offline=False,
                     client=_client(_no_network))
    assert res.matches == [] and res.requests == [] and res.okpd_classes == [] and not res.predicted
    assert "DISCOVERY_PREDICTED_CLASS" in res.notes[0]
    monkeypatch.setenv("DISCOVERY_PREDICTED_CLASS", "1")
    on = D.discover(q, history, cache=c, weights=WEIGHTS, processed=search_data, offline=True)
    assert on.predicted and on.okpd_classes == ["17.12"] and len(on.matches) == 10
    # an explicit OKPD2 never needs the flag
    monkeypatch.delenv("DISCOVERY_PREDICTED_CLASS")
    explicit = D.discover(q.model_copy(update={"okpd2": ["17.12.14.110"]}), history, cache=c, weights=WEIGHTS,
                          processed=search_data, offline=True)
    assert len(explicit.matches) == 10 and not explicit.predicted


def test_known_inns_are_cached_per_file_version(tmp_path):
    processed = _processed(tmp_path, ["7805772436"])
    assert D.known_inns(["7805772436", "4703173460"], None, processed) == {"7805772436"}
    assert D.known_inns([], None, processed) == set()
    path = processed / "suppliers.parquet"
    before = dict(D._SUPPLIERS)
    assert D.known_inns(["4703173460"], None, processed) == set() and D._SUPPLIERS == before  # no re-read
    con = duckdb.connect()  # the data is rebuilt: the new file is read, the old entry is dropped
    con.execute(f"COPY (SELECT '4703173460' AS inn) TO '{path}' (FORMAT parquet)")
    con.close()
    import os
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    assert D.known_inns(["7805772436", "4703173460"], None, processed) == {"4703173460"}
    assert sum(k[0] == str(path) for k in D._SUPPLIERS) == 1
    path.unlink()
    assert D.known_inns(["4703173460"], None, processed) is None


def test_live_requests_respect_plan_budget_and_block(tmp_path):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.content.decode())
        return httpx.Response(200, json={"rowCount": 0, "data": []})

    processed = _processed(tmp_path, [])
    q = _query(okpd2=["17.12.14.110"])
    _run(SourceCache(tmp_path / "a.sqlite"), processed, query=q, offline=False, client=_client(handler))
    assert [p.split("region=")[1] for p in posted] == [  # own region first, then the neighbour; ≤ MAX_REQUESTS
        "78&npType=UL&okved1=17.12", "78&npType=UL&okved1=46.49.33",
        "47&npType=UL&okved1=17.12", "47&npType=UL&okved1=46.49.33"]
    posted.clear()
    two = _query(okpd2=["17.12.14.110", "26.20.40.110"])  # 2 classes × (class + wholesale) × 2 regions = 8
    _run(SourceCache(tmp_path / "two.sqlite"), processed, query=two, offline=False, client=_client(handler))
    assert len(posted) == D.MAX_REQUESTS == 4 and all("region=78" in p for p in posted)
    posted.clear()
    lo = _query(okpd2=["17.12.14.110"], filters={"region": "Ленинградская область"})
    _run(SourceCache(tmp_path / "b.sqlite"), processed, query=lo, offline=False, client=_client(handler))
    assert posted[0].endswith("region=47&npType=UL&okved1=17.12")
    posted.clear()
    res = _run(SourceCache(tmp_path / "c.sqlite"), processed, query=q, offline=False, budget_s=0.0,
               client=_client(handler))
    assert posted == [] and all("нет в кеше" in r for r in res.requests)  # budget spent → cache only
    blocked = _run(SourceCache(tmp_path / "d.sqlite"), processed, query=q, offline=False,
                   client=_client(lambda r: (posted.append("x"), httpx.Response(403))[1]))
    assert posted == ["x"] and blocked.matches == [] and "отказал" in blocked.requests[0]  # one try, then stop
    assert any("защиту не обходим" in n for n in blocked.notes)


def test_source_timeout_gives_empty_block_and_the_rest_of_the_query_is_cache_only(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("timeout", request=request)

    processed = _processed(tmp_path, [])
    res = _run(SourceCache(tmp_path / "c.sqlite"), processed, offline=False, client=_client(handler))
    assert res.matches == [] and len(calls) == 1  # one attempt, not one per (OKVED, region)
    assert "не ответил" in res.requests[0] and all("в сеть не ходили" in r for r in res.requests[1:])
    assert rmsp.blocked()  # the hanging source is left alone for a while: the next search does not wait either
    again = _run(SourceCache(tmp_path / "c2.sqlite"), processed, offline=False, client=_client(_no_network))
    assert again.matches == [] and all("в сеть не ходили" in r for r in again.requests)


def test_server_error_makes_the_rest_of_the_query_cache_only(tmp_path):
    calls = []
    processed = _processed(tmp_path, [])
    res = _run(SourceCache(tmp_path / "c.sqlite"), processed, offline=False,
               client=_client(lambda r: (calls.append(1), httpx.Response(500))[1]))
    assert len(calls) == 1 and "не ответил" in res.requests[0] and not rmsp.blocked()
    # a failed refresh that fell back to an outdated answer also ends the live part of the query
    c = SourceCache(tmp_path / "stale.sqlite", ttl_s=10)
    c.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", ANSWER), fetched_at=1.0)
    calls.clear()
    res = _run(c, processed, offline=False,
               client=_client(lambda r: (calls.append(1), httpx.Response(500))[1]))
    assert len(calls) == 1 and len(res.matches) == 5 and "кеш" in res.requests[0]


def _hang(calls: list, ok_first: float | None = None):
    """A source that keeps silent exactly as long as the client agreed to wait (connect + read)."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({**request.extensions["timeout"], "at": time.monotonic()})
        if ok_first is not None and len(calls) == 1:
            time.sleep(ok_first)
            return httpx.Response(200, json={"rowCount": 0, "data": []})
        t = request.extensions["timeout"]
        time.sleep(t["connect"] + t["read"])
        raise httpx.ReadTimeout("silence", request=request)
    return handler


@pytest.mark.parametrize(("ok_first", "pacing", "expected_calls"), [
    (None, 0.0, 1),  # the very first request hangs
    (0.2, 0.0, 2),  # the first answers slowly, the second hangs: it only gets what is left
    (0.0, 0.3, 2),  # the pause between requests is part of the budget too
    (0.5, 0.0, 1),  # almost nothing left after the first answer → the second request is not started
])
def test_hanging_source_cannot_hold_the_search_longer_than_the_budget(tmp_path, monkeypatch, ok_first, pacing,
                                                                     expected_calls):
    budget = 0.6
    monkeypatch.setattr(rmsp, "MIN_INTERVAL_S", pacing)
    monkeypatch.setattr(rmsp, "_last_request", 0.0)
    calls: list = []
    res = _run(SourceCache(tmp_path / "c.sqlite"), _processed(tmp_path, []), query=_query(okpd2=["17.12.14.110"]),
               offline=False, budget_s=budget, client=_client(_hang(calls, ok_first)))
    # coarse guard against a request waiting its full default timeout; the clock starts at the first request
    # (the deadline starts after the query setup) with slack for result handling on a loaded machine
    elapsed = time.monotonic() - calls[0]["at"]
    assert elapsed < budget + 1.0, f"{elapsed:.2f} s of network for a {budget} s budget"
    assert len(calls) == expected_calls and len(res.requests) == 4 and res.matches == []
    # every request is allowed to wait only for what is left before the deadline
    assert all(t["at"] - calls[0]["at"] + t["connect"] + t["read"] <= budget + 0.1 for t in calls)


def test_request_timeout_is_capped_by_the_default_when_time_is_plenty(tmp_path, monkeypatch):
    monkeypatch.setattr(rmsp, "TIMEOUT_S", 3.0)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=ANSWER)

    c = SourceCache(tmp_path / "c.sqlite")
    rmsp.fetch_search("17.23", "78", cache=c, client=_client(handler))  # no deadline: connect + read = 3 s
    rmsp.fetch_search("17.23", "47", cache=c, client=_client(handler), deadline=time.monotonic() + 60)
    assert [(t["connect"], t["read"]) for t in seen] == [(1.5, 1.5), (1.5, 1.5)]
    with pytest.raises(rmsp.Paused):  # too little time: no request at all, and it is not a refusal
        rmsp.fetch_search("17.12", "78", cache=c, client=_client(_no_network), deadline=time.monotonic() + 0.05)
    assert not rmsp.blocked()


def test_okved_code_without_a_name_still_has_evidence(tmp_path):
    no_name = {k: v for k, v in ANSWER["data"][0].items() if k != "okved1name"}
    no_code = {k: v for k, v in ANSWER["data"][2].items() if k not in ("okved1", "okved1name")}
    c = SourceCache(tmp_path / "c.sqlite")
    c.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", {"data": [no_name, no_code]}))
    res = _run(c, _processed(tmp_path, []))
    assert [m.counterparty.inn for m in res.matches] == ["4703173460"] and res.inactive == 0  # no OKVED → not shown
    cp = res.matches[0].counterparty
    ev = next(e for e in cp.evidence if e.field == "okved")
    assert ev.value == "4703173460.okved1: 17.23" and ev.fact == "Основной ОКВЭД 17.23" and cp.okved == ["17.23"]
    assert check_counterparty(cp, c.page_text) == []
    assert res.matches[0].components[0].explanation.startswith("Основной ОКВЭД 17.23 совпадает с классом ОКПД2 запроса")
    fake = ev.model_copy(update={"value": "4703173460.okved1: 46.49"})  # a code the row does not hold
    assert check_counterparty(cp.model_copy(update={"evidence": [fake, *cp.evidence]}), c.page_text)


def _card(inn: str, status: str) -> str:  # synthetic ФНС card text in the pb_nalog.flatten format
    return "\n".join([f"search.inn: {inn}", f"vyp.ИНН: {inn}", "vyp.ОГРН: 1157847383638", "vyp.КПП: 780501001",
                      'vyp.НаимЮЛСокр: ООО "ПК ЭВРИКА"', f"vyp.sulst_name_ex: {status}",
                      "vyp.АдресРФ: 198095, Г.САНКТ-ПЕТЕРБУРГ, УЛ. ШВЕЦОВА, Д. 23"])


def test_cached_card_adds_status_and_uncached_inns_go_to_the_queue(cache, tmp_path):
    cache.put(pb_nalog.card_url("7805772436"), _card("7805772436", "Действующая организация"))
    cache.put(pb_nalog.card_url("7807032363"), _card("7807032363", "Ликвидировано"))
    queued: list[str] = []
    res = _run(cache, _processed(tmp_path, []), enqueue=queued.append)
    by_inn = {m.counterparty.inn: m for m in res.matches}
    assert "7807032363" not in by_inn  # liquidated by the ФНС card → not shown
    cp = by_inn["7805772436"].counterparty
    assert cp.status == "Действующая организация" and cp.kpp == "780501001"
    assert {"status", "kpp", "address"} <= {e.field for e in cp.evidence if e.source == pb_nalog.SOURCE}
    assert sum(e.field == "inn" for e in cp.evidence) == 1  # the same fact is not listed twice
    assert check_counterparty(cp, cache.page_text) == []
    assert "Действующая организация" in by_inn["7805772436"].components[2].explanation
    assert sorted(queued) == ["4703173460", "7810400592", "7811777270"]  # shown and without a card


def test_wholesale_neighbour_region_and_ip(tmp_path):
    c = SourceCache(tmp_path / "c.sqlite")
    trade = {**ANSWER["data"][0], "okved1": "46.49.33", "regioncode": "47",
             "okved1name": "Торговля оптовая писчебумажными и канцелярскими товарами"}
    c.put(rmsp.search_url("46.49.33", "47"), rmsp.flatten("46.49.33", "47", "UL", {"rowCount": 1, "data": [trade]}))
    c.put(rmsp.search_url("17.12", "78"), rmsp.flatten("17.12", "78", "UL", {"rowCount": 1, "data": [
        {**IP_ROW, "nptype": "UL", "inn": "7811777270", "okved1": "17.12",
         "okved1name": "Производство бумаги и картона"}]}))
    res = _run(c, _processed(tmp_path, []), query=_query(okpd2=["17.12.14.110"]))
    maker, trader = res.matches
    assert maker.counterparty.inn == "7811777270" and maker.score == 0.45
    assert trader.score == 0.29  # 0.35·0.6 + 0.1·0.8
    assert trader.components[0].explanation.endswith("вероятно дистрибьютор")  # no «по классу» note for trade
    assert "соседний регион — Ленинградская область" in trader.components[4].explanation
    assert trader.counterparty.region == "Ленинградская область"


def _rows(inns: list[str], okved: str, name: str) -> dict:
    return {"rowCount": -1, "data": [{**ANSWER["data"][0], "inn": i, "okved1": okved, "okved1name": name}
                                     for i in inns]}


def test_top_ten_keeps_both_producers_and_distributors(tmp_path):
    makers = ["7801000012", "7801000020", "7801000037", "7801000044", "7801000051", "4701000038", "7804428656",
              "7805772436", "7807032363", "7810400592", "7811777270", "4703173460"]
    traders = ["7707049388", "7805336905", "7806602902"]
    assert all(inn_is_valid(i) for i in makers + traders)
    c = SourceCache(tmp_path / "c.sqlite")
    c.put(rmsp.search_url("17.12", "78"),
          rmsp.flatten("17.12", "78", "UL", _rows(makers, "17.12", "Производство бумаги и картона")))
    c.put(rmsp.search_url("46.49.33", "78"),
          rmsp.flatten("46.49.33", "78", "UL", _rows(traders, "46.49.33", "Торговля оптовая канцелярскими товарами")))
    res = _run(c, _processed(tmp_path, []), query=_query(okpd2=["17.12.14.110"]))
    shown = [m.counterparty.inn for m in res.matches]
    assert len(shown) == D.MAX_SHOWN == 10 and set(traders) <= set(shown)  # 7 producers + all 3 distributors
    assert shown[:7] == sorted(makers)[:7] and shown[7:] == sorted(traders)  # score order, INN inside a score
    assert "всего в реестре не сообщается" in res.requests[0]  # rowCount -1 is not shown as a number


def test_ip_is_shown_from_the_registry_without_contacts(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_NP_TYPES", "IP")
    c = SourceCache(tmp_path / "c.sqlite")
    c.put(rmsp.search_url("17.23", "78", "IP"), rmsp.flatten("17.23", "78", "IP", {"rowCount": 1, "data": [IP_ROW]}))
    res = _run(c, _processed(tmp_path, []))
    cp = res.matches[0].counterparty
    assert cp.name == "ИП ИВАНОВ ИВАН ИВАНОВИЧ" and cp.inn == "500100732259" and cp.contacts == {}
    assert not {"phone", "email", "address"} & {e.field for e in cp.evidence}
    assert check_counterparty(cp, c.page_text) == []
    # second line of defence: even a page that somehow holds an ИП phone gives no contact
    page = c.put("https://rmsp.nalog.ru/x", "row.500100732259.phone: +7(921)1234567")
    leaked = D.from_row({**res_row(c), "phone": "+7(921)1234567", "email": "ivanov@example.org"}, page)
    assert leaked.contacts == {} and not {"phone", "email"} & {e.field for e in leaked.evidence}


def res_row(c: SourceCache) -> dict[str, str]:
    text = c.page_text(rmsp.search_url("17.23", "78", "IP"))
    assert text is not None
    return rmsp.parse(text).rows[0]


def test_fabricated_row_value_fails_the_evidence_check(cache, tmp_path):
    res = _run(cache, _processed(tmp_path, []))
    cp = res.matches[0].counterparty
    fake = cp.evidence[0].model_copy(update={"field": "phone", "value": "+7 (812) 555-12-34",
                                             "fact": "Телефон +7 (812) 555-12-34"})
    assert check_counterparty(cp.model_copy(update={"evidence": [*cp.evidence, fake]}), cache.page_text)
    other = next(e for e in res.matches[1].counterparty.evidence if e.field == "msp")
    assert other.value == f"{res.matches[1].counterparty.inn}.category: 2"  # tied to its own registry row


# --- OKPD2 class from history, API ---------------------------------------------------------------

def test_class_is_predicted_from_history_lots(search_data):
    from src.match.search import search
    history = search(Query(query_id="q", text="бумага А4"))
    assert D.stated_class(history) == "17.12" == D._lots_class(history, search_data)
    assert D.predict_class([], search_data) is None and D._lots_class(history, search_data / "nope") is None
    # lot 2 holds a cartridge (26.20) and a toner (20.59): lots alone cannot choose, search() can
    cartridge = search(Query(query_id="q", text="Картридж лазерный"))
    assert D.stated_class(cartridge) == "26.20" == D.predict_class(cartridge, search_data)
    assert D._lots_class(cartridge, search_data) == "20.59"
    # a query WITH codes states nothing; a reworded explanation falls back to the lots
    assert D.stated_class(search(Query(query_id="q", text="бумага А4", okpd2=["17.12.14.110"]))) is None
    reworded = [m.model_copy(update={"components": [c.model_copy(update={"explanation": "иначе"})
                                                    for c in m.components]}) for m in history]
    assert D.stated_class(reworded) is None and D.predict_class(reworded, search_data) == "17.12"


def _stated(explanations: list[str]) -> list:
    from src.contracts import Counterparty, CounterpartyMatch, ScoreComponent
    return [CounterpartyMatch(counterparty=Counterparty(inn=None, name="x"), score=1.0, origin="history",
                              components=[ScoreComponent(name="product_fit", value=1, weight=1, explanation=e)])
            for e in explanations]


def test_stated_class_takes_the_majority_and_ignores_short_codes():
    mark = "x; ОКПД2 определён по похожим позициям"
    assert D.stated_class(_stated([f"{mark}: совпадает только класс 17", f"{mark}: совпадает код 26.20.40.110",
                                   f"{mark}: совпадает до группы 26.20", f"{mark} 17.12.14.110 в его истории нет"])
                          ) == "26.20"
    assert D.stated_class(_stated([f"{mark}: совпадает только класс 17"])) is None
    assert D.stated_class(_stated(["Похожие позиции в лоте 26.20.40; ОКПД2 запроса: совпадает код 17.12.14"])) is None


@pytest.fixture
def api(tmp_path, monkeypatch, search_data):
    monkeypatch.setenv("DISCOVERY_PREDICTED_CLASS", "1")  # these tests search by text, without OKPD2
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/q.sqlite")
    monkeypatch.setattr(app_mod, "_engine", None)
    c = SourceCache()  # SOURCE_CACHE of this test (conftest)
    page = rmsp.flatten("46.49.33", "78", "UL", {"rowCount": 2, "dtQueryEnd": "01.10.2026 14:23:47", "data": [
        {**ANSWER["data"][0], "okved1": "46.49.33",
         "okved1name": "Торговля оптовая писчебумажными и канцелярскими товарами"},
        # an INN of the ingest fixture: already in organizers' data → must not be «новый»
        {**ANSWER["data"][2], "inn": "7801000012", "okved1": "46.49.33"}]})
    c.put(rmsp.search_url("46.49.33", "78"), page)
    return TestClient(app_mod.app)


def test_api_search_fills_the_discovered_block(api):
    body = api.post("/api/search", json={"query_id": "q", "text": "бумага А4"}).json()  # no OKPD2 in the query
    assert body["history"] and [m["counterparty"]["inn"] for m in body["discovered"]] == ["4703173460"]
    m = body["discovered"][0]
    assert m["origin"] == "discovered" and m["counterparty"]["name"] == 'ООО "МАРКЛИП"'
    assert all(e["url"].startswith("https://rmsp.nalog.ru/") and e["fetched_at"] for e in m["counterparty"]["evidence"])
    why = m["components"][0]["explanation"]  # a predicted class is never called «ОКПД2 запроса» (#63)
    assert "класса ОКПД2 17.12 (в запросе ОКПД2 не указан — класс определён автоматически" in why
    assert "ОКПД2 запроса" not in why
    assert "7801000012" in [x["counterparty"]["inn"] for x in body["history"]]
    assert api.get("/api/health").json()["queue"] == {"pending": 1}  # the ФНС card is ordered, not fetched inline
    only_lo = api.post("/api/search", json={"query_id": "q", "text": "бумага А4",
                                            "filters": {"region": "Ленинградская область"}}).json()
    assert only_lo["discovered"] == []


def test_api_text_query_without_flag_gives_an_empty_block_and_no_network(api, monkeypatch):
    monkeypatch.delenv("DISCOVERY_PREDICTED_CLASS")
    monkeypatch.delenv("DISCOVERY_OFFLINE")  # the network is allowed — and still must not be touched
    attempts = []
    monkeypatch.setattr(rmsp, "_post", lambda *a, **k: attempts.append(a) or 1 / 0)
    body = api.post("/api/search", json={"query_id": "q", "text": "бумага А4"}).json()
    assert body["history"] and body["discovered"] == [] and attempts == []
    assert api.get("/api/health").json()["queue"] == {}
    # the same request with the code from the notice fills the block without the flag
    monkeypatch.setenv("DISCOVERY_OFFLINE", "1")
    with_code = api.post("/api/search", json={"query_id": "q", "text": "бумага А4", "okpd2": ["17.12.14.110"]}).json()
    assert [m["counterparty"]["inn"] for m in with_code["discovered"]] == ["4703173460"]
    assert "ОКПД2 запроса 17.12" in with_code["discovered"][0]["components"][0]["explanation"]


def test_evidence_pages_are_prepared_once_per_request(cache, tmp_path):
    from src.enrich.evidence import cached_pages, strip_unverified
    res = _run(cache, _processed(tmp_path, []))
    reads: list[str] = []
    pages = cached_pages(lambda url: reads.append(url) or cache.page_text(url))
    shown = [strip_unverified(m.counterparty, pages) for m in res.matches]
    assert reads == [rmsp.search_url("17.23", "78")]  # 5 companies, ~30 facts, one page read
    assert shown == [strip_unverified(m.counterparty, cache.page_text) for m in res.matches]
    assert cached_pages(pages) is pages and pages("https://nope") is None


def test_api_does_not_call_unchecked_companies_new(api, monkeypatch):
    real = D.discover

    def unchecked(*a, **k):
        res = real(*a, **k)
        assert res.matches
        res.history_checked = False
        return res

    monkeypatch.setattr(D, "discover", unchecked)
    body = api.post("/api/search", json={"query_id": "q", "text": "бумага А4"}).json()
    assert body["history"] and body["discovered"] == []


def test_api_search_survives_a_broken_discovery(api, monkeypatch):
    monkeypatch.setattr(D, "discover", lambda *a, **k: 1 / 0)
    body = api.post("/api/search", json={"query_id": "q", "text": "бумага А4"}).json()
    assert body["history"] and body["discovered"] == []


def test_e2e_fixtures_give_the_demo_lot_a_filled_block(tmp_path):
    """The recorded answers the Playwright demo runs on (tests/fixtures/rmsp/e2e): real rows, 01.10."""
    c = SourceCache(tmp_path / "c.sqlite")
    assert rmsp.seed(FIX / "e2e", c) == 2
    res = _run(c, _processed(tmp_path, []), query=_query(text="Бумага офисная А4", okpd2=["17.12.14.110"]))
    assert len(res.matches) == 10 and res.history_checked
    assert sorted({m.score for m in res.matches}) == [0.31, 0.45]  # distributors and producers of СПб
    assert all(check_counterparty(m.counterparty, c.page_text) == [] for m in res.matches)
    assert all(m.counterparty.evidence[0].fetched_at.date().isoformat() == "2026-10-01" for m in res.matches)
