"""Discovery v0 (#16): rmsp.nalog.ru answer → block «Новые», only with Evidence, no network in tests.

`tests/fixtures/rmsp/UL_17.23_78.json` is a real answer of the extended search recorded 01.10
(session tokens and row ids removed). ИП and ФНС-card texts used here are synthetic.
"""

import json
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
    assert "вероятно производитель" in fit.explanation and "не указан" not in fit.explanation
    assert "ещё не проверен" in first.components[2].explanation and first.components[2].weight == 0.0


def test_registry_contacts_of_a_legal_entity_pass_the_evidence_check(cache, tmp_path):
    res = _run(cache, _processed(tmp_path, []))
    cp = next(m.counterparty for m in res.matches if m.counterparty.inn == "7805772436")
    assert cp.contacts == {"phone": "+7(812)4487815", "email": "evrikapk@mail.ru"}
    assert check_counterparty(cp, cache.page_text) == []
    assert len(res.matches) == 5 and res.known == 0


def test_odd_phone_is_not_a_contact_and_legal_form_suffix_is_shortened(tmp_path):
    # real shapes seen live on 01.10: phone "+7(96507)53515", name with the legal form at the end
    row = {**ANSWER["data"][2], "phone": "+7(96507)53515",
           "name_ex": '" ЭЛАСТИС " ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ'}
    c = SourceCache(tmp_path / "c.sqlite")
    c.put(rmsp.search_url("17.23", "78"), rmsp.flatten("17.23", "78", "UL", {"data": [row]}))
    cp = _run(c, _processed(tmp_path, [])).matches[0].counterparty
    assert cp.contacts == {"email": "evrikapk@mail.ru"} and "phone" not in {e.field for e in cp.evidence}
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


def test_query_without_okpd2_and_history_does_not_search(cache, tmp_path):
    res = _run(cache, _processed(tmp_path, []), query=_query(okpd2=[]), client=_client(_no_network))
    assert res.matches == [] and res.requests == [] and "не определён" in res.notes[0]


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


def test_source_timeout_gives_empty_block_not_an_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    res = _run(SourceCache(tmp_path / "c.sqlite"), _processed(tmp_path, []), offline=False, client=_client(handler))
    assert res.matches == [] and all("не ответил" in r for r in res.requests)


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
    assert "вероятно дистрибьютор" in trader.components[0].explanation
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
    assert D.predict_class(history, search_data) == "17.12"
    assert D.predict_class([], search_data) is None
    assert D.predict_class(history, search_data / "nope") is None


@pytest.fixture
def api(tmp_path, monkeypatch, search_data):
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
    assert "класс определён по похожим закупкам" in m["components"][0]["explanation"]
    assert "7801000012" in [x["counterparty"]["inn"] for x in body["history"]]
    assert api.get("/api/health").json()["queue"] == {"pending": 1}  # the ФНС card is ordered, not fetched inline
    only_lo = api.post("/api/search", json={"query_id": "q", "text": "бумага А4",
                                            "filters": {"region": "Ленинградская область"}}).json()
    assert only_lo["discovered"] == []


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
