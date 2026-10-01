"""Company site → INN (src/sources/site.py), the discovery funnel and the fallback path «one lot → candidates»
with its plan (src/enrich/web_discover.py), #194 / #186.

Sites are synthetic HTML served by httpx.MockTransport or put into the cache; the registry card is the
recorded pb.nalog.ru card of tests/fixtures/pb_nalog (7804428656). No network.
"""

import base64
import json
import logging
import unittest.mock
from collections import Counter
from pathlib import Path

import httpx
import pytest

from src.contracts import Counterparty, CounterpartyMatch, ScoreComponent, inn_is_valid
from src.enrich import web_discover as W
from src.enrich.enrich import enrich as real_enrich
from src.enrich.evidence import check_counterparty
from src.sources import pb_nalog, site, yandex_search
from src.sources.cache import SourceCache

PB = Path(__file__).resolve().parents[1] / "fixtures" / "pb_nalog"
INN = "7804428656"  # the recorded card: «Действующая организация»
OTHER = "7707049388"
IN_DATA = "7801000012"  # tests/fixtures/ingest: a supplier of organizers' data
HOME = "https://brass.example.ru/"

PAGE_HOME = """<html><head><script>var inn = "7707049388";</script><style>.x{}</style></head><body>
<nav><a href="/catalog/">Каталог</a> <a href="/rekvizity/">Реквизиты</a> <a href="https://other.ru/contacts">x</a></nav>
<p>Поставка оборудования</p></body></html>"""
PAGE_REQ = f"<html><body><table><tr><td>ИНН/КПП</td><td>{INN}/780401001</td></tr></table></body></html>"


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.setattr(site, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setenv("YANDEX_BUDGET_DB", str(tmp_path / "budget.sqlite"))
    # Yandex is OFF unless a test switches it on (`yandex_on`); a developer's .env never decides a test
    for name in ("YANDEX_SEARCH_API_KEY", "YANDEX_MAX_PER_LOT", "YANDEX_MAX_PER_RUN", "YANDEX_SEARCH_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(yandex_search, "_dotenv", lambda name: None)
    monkeypatch.setattr(yandex_search, "_run_caps", None)
    monkeypatch.setattr(yandex_search, "_blocked_until", 0.0)
    monkeypatch.setattr(yandex_search, "MIN_INTERVAL_S", 0.0)


def _client(pages: dict[str, tuple[int, str]], calls: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls.append(url)
        status, body = pages.get(url, (404, ""))
        return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- site.py ---------------------------------------------------------------------------------------------

def test_html_to_text_drops_scripts_and_keeps_blocks():
    text = site.html_to_text(PAGE_HOME)
    assert "7707049388" not in text and ".x{}" not in text
    assert "Поставка оборудования" in text.splitlines()


def test_labelled_inns_checksum_and_label():
    assert site.labelled_inns(f"ИНН/КПП: {INN}/780401001") == [INN]
    assert site.labelled_inns("ИНН 7804428657") == []  # bad checksum
    assert site.labelled_inns(f"Телефон {INN}") == []  # no label — not an INN
    assert all(inn_is_valid(i) for i in site.labelled_inns(f"ИНН {INN}, ИНН {OTHER}"))


def test_requisite_links_same_site_only():
    links = site.requisite_links(PAGE_HOME, HOME)
    assert links == [HOME + "rekvizity/"]


def test_decide():
    assert site.decide(Counter()) == (None, "на сайте нет ИНН")
    assert site.decide(Counter({INN: 2}))[0] == INN
    assert site.decide(Counter({INN: 3, OTHER: 1}))[0] == INN
    assert site.decide(Counter({INN: 2, OTHER: 1}))[0] is None


def test_site_inn_follows_requisites_link(tmp_path):
    calls: list[str] = []
    client = _client({HOME + "robots.txt": (200, "User-agent: *\nDisallow: /admin/\n"),
                      HOME: (200, PAGE_HOME), HOME + "rekvizity/": (200, PAGE_REQ)}, calls)
    cache = SourceCache(tmp_path / "c.sqlite")
    res = site.site_inn(HOME, cache, client)
    assert res.inn == INN and res.url == HOME + "rekvizity/"
    assert res.pages == [HOME, HOME + "rekvizity/"]
    # the INN is literally in the cached text: an Evidence on this page passes
    assert INN in (cache.page_text(HOME + "rekvizity/") or "")
    again = site.site_inn(HOME, cache, client)  # all cached: no new requests
    assert again.inn == INN and calls.count(HOME) == 1


def test_robots_disallow_means_no_page(tmp_path):
    calls: list[str] = []
    client = _client({HOME + "robots.txt": (200, "User-agent: *\nDisallow: /\n"), HOME: (200, PAGE_REQ)}, calls)
    res = site.site_inn(HOME, SourceCache(tmp_path / "c.sqlite"), client)
    assert res.inn is None and "robots" in res.reason
    assert HOME not in calls


def test_refusal_is_not_bypassed(tmp_path):
    client = _client({HOME + "robots.txt": (404, ""), HOME: (403, "Forbidden")})
    res = site.site_inn(HOME, SourceCache(tmp_path / "c.sqlite"), client)
    assert res.inn is None and "не обходим" in res.reason


def test_several_inns_give_none(tmp_path):
    body = f"<p>ИНН {INN}</p><p>ИНН {OTHER}</p>"
    client = _client({HOME + "robots.txt": (404, ""), HOME: (200, body)})
    res = site.site_inn(HOME, SourceCache(tmp_path / "c.sqlite"), client)
    assert res.inn is None and "несколько" in res.reason


# --- web_discover.py --------------------------------------------------------------------------------------

def test_queries_and_skip_domains():
    cfg = {"templates": ["{p} оптом", "{p} производитель"], "groups": {"21.20": ["вакцины", "бинты"]},
           "max_requests_per_group": 3}
    assert W.queries(cfg, "21.20") == ["вакцины оптом", "вакцины производитель", "бинты оптом"]
    assert W.skipped("spb.2gis.ru", ["2gis.ru"]) and not W.skipped("my2gis.ru", ["2gis.ru"])


def test_real_config_is_consistent():
    cfg = W.load_config()
    for g, products in cfg["groups"].items():
        assert len(g) == 5 and products
        assert len(W.queries(cfg, g)) <= cfg["max_requests_per_group"]


def _seed(cache: SourceCache, query: str, domain_inn: str = INN) -> None:
    xml = f"""<?xml version="1.0" encoding="utf-8"?><yandexsearch><response><found priority="all">2</found>
    <results><grouping><group><doc><url>{HOME}catalog/</url><domain>brass.example.ru</domain>
    <title>Брасс</title></doc></group><group><doc><url>https://spb.2gis.ru/firm/1</url><domain>spb.2gis.ru</domain>
    </doc></group></grouping></results></response></yandexsearch>"""
    cache.put(yandex_search.search_url(query, 0, "2"), yandex_search.flatten(query, 0, "2", xml))
    cache.put(site.robots_url(HOME), "", status=404)
    cache.put(HOME, site.page_body(PAGE_HOME, HOME))
    cache.put(HOME + "rekvizity/", site.page_body(PAGE_REQ.replace(INN, domain_inn), HOME + "rekvizity/"))


CFG = {"templates": ["{p} оптом"], "groups": {"21.20": ["оборудование"]}, "skip_domains": ["2gis.ru"],
       "region": "2", "pages": 1}


def test_funnel_new_company_with_evidence(tmp_path, fixture_processed):
    cache = SourceCache(tmp_path / "c.sqlite")
    _seed(cache, "оборудование оптом")
    cache.put(pb_nalog.card_url(INN), (PB / f"{INN}.txt").read_text(encoding="utf-8"))
    budget = yandex_search.Budget(limit=0)  # offline run must not need any budget
    rep, pool = W.run_group("21.20", CFG, cache, budget, fixture_processed, True, 0, set())
    f = rep.funnel()
    assert f["domains"] == 2 and f["domains_skipped"] == 1 and f["new"] == 1
    cp = pool[0]
    assert cp.inn == INN
    assert check_counterparty(cp, cache.page_text) == []
    sources = {e.source for e in cp.evidence}
    assert {site.SOURCE, yandex_search.SOURCE, pb_nalog.SOURCE} <= sources
    assert [c.inn for c in W.read_pool("21.20", cache)] == [INN]


def test_funnel_known_supplier_is_not_new(tmp_path, fixture_processed, monkeypatch):
    cache = SourceCache(tmp_path / "c.sqlite")
    _seed(cache, "оборудование оптом", IN_DATA)
    monkeypatch.setattr(W, "enrich", lambda inn, cache, offline: Counterparty(inn=inn, name="ООО Тест",
                                                                               status="Действующая организация"))
    rep, pool = W.run_group("21.20", CFG, cache, yandex_search.Budget(limit=0), fixture_processed, True, 0, set())
    assert rep.funnel()["in_data"] == 1 and pool == []


def test_funnel_liquidated_is_dropped(tmp_path, fixture_processed, monkeypatch):
    cache = SourceCache(tmp_path / "c.sqlite")
    _seed(cache, "оборудование оптом")
    monkeypatch.setattr(W, "enrich", lambda inn, cache, offline: Counterparty(
        inn=inn, name="ООО Тест", status="Организация ликвидирована"))
    rep, pool = W.run_group("21.20", CFG, cache, yandex_search.Budget(limit=0), fixture_processed, True, 0, set())
    assert rep.funnel()["dead"] == 1 and pool == []


def test_unresolved_inn_is_never_guessed(tmp_path, fixture_processed, monkeypatch):
    cache = SourceCache(tmp_path / "c.sqlite")
    _seed(cache, "оборудование оптом")

    def not_found(inn, cache, offline):
        raise KeyError(inn)

    monkeypatch.setattr(W, "enrich", not_found)
    rep, pool = W.run_group("21.20", CFG, cache, yandex_search.Budget(limit=0), fixture_processed, True, 0, set())
    assert rep.funnel()["not_in_registry"] == 1 and pool == []


# --- the pool in the block «Новые» (src/enrich/discover.py) ------------------------------------------------

RMSP = Path(__file__).resolve().parents[1] / "fixtures" / "rmsp"
WEIGHTS = {"product_fit": 0.35, "experience": 0.2, "reliability": 0.2, "activity": 0.1, "geography": 0.1,
           "price": 0.05}


def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"network call in an offline path: {request.url}")


def _discover(cache: SourceCache, processed: Path):
    from src.contracts import Query
    from src.enrich import discover as D

    q = Query(query_id="q", text="папки канцелярские", okpd2=["17.23.13.130"])
    return D.discover(q, cache=cache, weights=WEIGHTS, processed=processed, offline=True,
                      client=httpx.Client(transport=httpx.MockTransport(_no_network)))


def test_pool_joins_the_new_block_from_cache_with_evidence(tmp_path, fixture_processed):
    from src.enrich import discover as D
    from src.sources import rmsp

    cache = SourceCache(tmp_path / "c.sqlite")
    assert rmsp.seed(RMSP, cache) == 1  # the registry channel of class 17.23
    _seed(cache, "оборудование оптом")
    cache.put(pb_nalog.card_url(INN), (PB / f"{INN}.txt").read_text(encoding="utf-8"))
    cfg = {**CFG, "groups": {"17.23": ["оборудование"]}}
    W.run_group("17.23", cfg, cache, yandex_search.Budget(limit=0), fixture_processed, True, 0, set())

    res = _discover(cache, fixture_processed)
    web = [m for m in res.pool if m.counterparty.inn == INN]
    assert len(web) == 1 and web[0].origin == "discovered"
    m = web[0]
    assert check_counterparty(m.counterparty, cache.page_text) == []
    assert [c.name for c in m.components] == list(D.COMPONENT_ORDER)
    fit = m.components[0]
    assert fit.value == D.FIT["web"] and "Яндекс Поиске" in fit.explanation and "brass.example.ru" in fit.explanation
    assert m.score == round(sum(c.value * c.weight for c in m.components), 4)
    assert any("Яндекс Поиск, ОКПД2 17.23" in r for r in res.requests)
    assert len({x.counterparty.inn for x in res.pool}) == len(res.pool)  # no company twice


def test_pool_skips_registry_duplicates_and_organizers_suppliers(tmp_path, fixture_processed):
    from src.sources import rmsp

    cache = SourceCache(tmp_path / "c.sqlite")
    assert rmsp.seed(RMSP, cache) == 1
    dup = "4703173460"  # active in the recorded registry answer of 17.23: the registry channel keeps it
    pool = [Counterparty(inn=dup, name="ООО Дубль"), Counterparty(inn=IN_DATA, name="ООО Уже в данных"),
            Counterparty(inn=None, name="ООО Без ИНН")]
    cache.put(W.pool_url("17.23"), "[" + ",".join(cp.model_dump_json() for cp in pool) + "]")

    res = _discover(cache, fixture_processed)
    inns = [m.counterparty.inn for m in res.pool]
    assert inns.count(dup) == 1 and IN_DATA not in inns and None not in inns
    line = next(r for r in res.requests if r.startswith("Яндекс Поиск, ОКПД2 17.23"))
    assert "новых 0" in line and "уже в данных организаторов 1" in line


def test_without_organizers_data_nobody_is_new_and_no_paid_run(tmp_path, monkeypatch):
    cache = SourceCache(tmp_path / "c.sqlite")
    _seed(cache, "оборудование оптом")
    cache.put(pb_nalog.card_url(INN), (PB / f"{INN}.txt").read_text(encoding="utf-8"))
    empty = tmp_path / "no-data"
    rep, pool = W.run_group("21.20", CFG, cache, yandex_search.Budget(limit=0), empty, True, 0, set())
    assert rep.funnel()["unchecked"] == 1 and rep.funnel()["new"] == 0 and pool == []

    monkeypatch.setattr(W, "processed_dir", lambda: empty)
    monkeypatch.setattr(W, "SourceCache", lambda: cache)
    with pytest.raises(SystemExit):  # argparse error before any request is counted
        W.main(["Y-test", "--groups", "21.20", "--max-requests", "1"])
    assert yandex_search.Budget().used() == 0


# --- the fallback path: one lot → candidates (#194 / #186) ----------------------------------------------------

YFIX = Path(__file__).resolve().parents[1] / "fixtures" / "yandex_search"
LOT_TEXT = "Насосы дренажные на 2025 год (в 1 точке)"
LOT_QUERIES = ["Насосы дренажные производитель", "Насосы дренажные оптом", "Насосы дренажные официальный дилер"]
SITE_HOME = """<html><body><nav><a href="/rekvizity/">Реквизиты</a></nav>
<h1>Насосы дренажные и фекальные</h1><p>Мы — официальный дистрибьютор завода «Пример».</p>
<div>Отдел продаж: +7 (812) 000-00-07, sales@brass.example.ru</div>
<div>Менеджер Сидоров Иван Иванович: +7 (921) 000-00-08</div></body></html>"""


@pytest.fixture
def yandex_on(monkeypatch):
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "test-key")
    monkeypatch.setenv("YANDEX_MAX_PER_LOT", "3")
    monkeypatch.setenv("YANDEX_MAX_PER_RUN", "10")
    monkeypatch.setenv("YANDEX_SEARCH_MODE", "sync")
    # live mode of the test: the registry card comes from the cache, never from the network
    monkeypatch.setattr(W, "enrich", lambda inn, cache, offline: real_enrich(inn, cache, offline=True))


def _forbidden() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a request was sent while nothing may be sent: {request.method} {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _yandex(calls: list[httpx.Request]) -> httpx.Client:
    """The stub of Yandex Search: sync answers and deferred operations from tests/fixtures/yandex_search."""
    xml = (YFIX / "operation_answer.xml").read_text(encoding="utf-8")
    pending = json.loads((YFIX / "operation_pending.json").read_text(encoding="utf-8"))
    done = json.loads((YFIX / "operation_done.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = str(request.url)
        if url == yandex_search.ENDPOINT:
            return httpx.Response(200, json={"rawData": base64.b64encode(xml.encode("utf-8")).decode()})
        if url == yandex_search.ASYNC_ENDPOINT:
            n = sum(1 for c in calls if c.method == "POST")
            return httpx.Response(200, json={**pending, "id": f"op{n}"})
        assert request.method == "GET" and url.startswith(yandex_search.OPERATION_ENDPOINT)
        return httpx.Response(200, json=done)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _site(cache: SourceCache) -> None:
    """The company site of the search answer, cached as src/sources/site.py caches it, and its ФНС card."""
    cache.put(site.robots_url(HOME), "", status=404)
    cache.put(HOME, site.page_body(SITE_HOME, HOME))
    cache.put(HOME + "rekvizity/", site.page_body(PAGE_REQ, HOME + "rekvizity/"))
    cache.put(pb_nalog.card_url(INN), (PB / f"{INN}.txt").read_text(encoding="utf-8"))


def _match(fit: float) -> CounterpartyMatch:
    comps = [ScoreComponent(name="product_fit", value=fit, weight=0.35, explanation="x"),
             ScoreComponent(name="geography", value=1.0, weight=0.1, explanation="x")]
    return CounterpartyMatch(counterparty=Counterparty(inn=None, name="ООО Тест"), origin="discovered",
                             components=comps, score=0.5)


@pytest.mark.parametrize(("text", "clean"), [
    ("Насосы дренажные на 2025 год (в 1 точке)", "Насосы дренажные"),
    ("Поставка бумаги офисной в 2025 году", "Поставка бумаги офисной"),
    ("Услуги связи 2024-2025 гг.", "Услуги связи"),
    ("Молоко питьевое, 2025 г., в 12 точках", "Молоко питьевое"),
    ("Картридж HP 2055 для принтера", "Картридж HP 2055 для принтера"),  # a model number is not a year
    ("Вывоз отходов за 2026 год; в 3-х точках", "Вывоз отходов"),
    ("  2025  ", ""),
])
def test_clean_text(text, clean):
    assert W.clean_text(text) == clean


def test_clean_names_drops_okpd_names_and_repeats():
    names = ["Бумага офисная А4 2025 год", "Бумага и картон", "бумага  И КАРТОН.", "Бумага офисная А4", "2025"]
    assert W.clean_names(names, okpd_names=["Бумага и картон"]) == ["Бумага офисная А4"]


@pytest.mark.parametrize(("okpd2", "kind"), [
    (["17.12.14"], "goods"), (["01.11"], "goods"), (["33.12"], "goods"), (["34.00"], "services"),
    (["71.20.19", "17.12"], "services"), (["81.21"], "services"), (["43.21"], "services"), ([], "goods"),
    (["", "x", "86.10"], "services"),
])
def test_purchase_kind(okpd2, kind):
    assert W.purchase_kind(okpd2) == kind


def test_lot_queries_by_purchase_type():
    assert W.lot_queries(LOT_TEXT, ["28.13.14"]) == LOT_QUERIES
    assert W.lot_queries("Испытания воды в 2025 году", ["71.20.19"]) == [
        "Испытания воды исполнитель", "Испытания воды подрядчик", "Испытания воды аккредитованная лаборатория"]
    assert W.lot_queries("Испытания воды", ["71.20.19"], kind="goods")[0] == "Испытания воды производитель"
    assert W.lot_queries(" 2025 год ", ["28.13"]) == []
    long = W.lot_queries("слово " * 300, [])
    assert all(len(q) <= W.MAX_QUERY_CHARS and len(q.split()) <= W.MAX_QUERY_WORDS + 2 for q in long)


def test_strong_count_takes_steps_1_0_and_0_8():
    assert W.strong_count([_match(1.0), _match(0.8), _match(0.79), _match(0.6), _match(0.4)]) == 2
    assert W.strong_count([]) == 0


@pytest.mark.parametrize(("env", "reason"), [
    ({}, "выключен: нет ключа"),
    ({"YANDEX_SEARCH_API_KEY": "k", "YANDEX_MAX_PER_RUN": "10"}, "выключен: нет потолка (YANDEX_MAX_PER_LOT)"),
    ({"YANDEX_SEARCH_API_KEY": "k", "YANDEX_MAX_PER_LOT": "3"}, "выключен: нет потолка (YANDEX_MAX_PER_RUN)"),
    ({"YANDEX_MAX_PER_LOT": "3", "YANDEX_MAX_PER_RUN": "10"}, "выключен: нет ключа"),
])
def test_fallback_is_off_by_default_and_sends_nothing(env, reason, monkeypatch, tmp_path, fixture_processed):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cache = SourceCache(tmp_path / "c.sqlite")
    _site(cache)
    budget = yandex_search.Budget(limit=10)
    res = W.lot_candidates("L1", LOT_TEXT, 0, okpd2=["28.13.14"], cache=cache, budget=budget, cfg=CFG,
                           processed=fixture_processed, client=_forbidden(), site_client=_forbidden(),
                           offline=False)
    assert not res.used and reason in res.reason
    assert res.candidates == [] and res.queries == [] and res.requests_live == 0 and budget.used() == 0
    assert W.fallback_needed(0) == (False, res.reason)


@pytest.mark.parametrize(("strong", "need"), [(0, True), (2, True), (3, False), (4, False), (50, False)])
def test_fallback_only_with_fewer_than_three_strong(strong, need, yandex_on, tmp_path, fixture_processed):
    got, why = W.fallback_needed(strong)
    assert got is need and (f"{strong} < 3" in why if need else "не нужен: сильных кандидатов" in why)
    if not need:
        budget = yandex_search.Budget(limit=10)
        res = W.lot_candidates("L1", LOT_TEXT, strong, cache=SourceCache(tmp_path / "c.sqlite"), budget=budget,
                               cfg=CFG, processed=fixture_processed, client=_forbidden(), offline=False)
        assert not res.used and res.reason == why and f"{strong} ≥ 3" in why and budget.used() == 0


def test_fallback_finds_a_new_company_with_facts_of_its_site(yandex_on, tmp_path, fixture_processed):
    cache = SourceCache(tmp_path / "c.sqlite")
    _site(cache)
    calls: list[httpx.Request] = []
    budget, caps = yandex_search.Budget(limit=10), yandex_search.Caps(3, 10)
    kw = {"okpd2": ["28.13.14"], "cache": cache, "budget": budget, "caps": caps, "cfg": CFG,
          "processed": fixture_processed, "site_client": _forbidden(), "offline": False}
    res = W.lot_candidates("L1", LOT_TEXT, 2, client=_yandex(calls), **kw)

    assert res.used and res.queries == LOT_QUERIES
    assert [json.loads(c.content)["query"]["queryText"] for c in calls] == LOT_QUERIES
    assert res.requests_live == 3 and budget.used() == 3 and caps.used_lot == {"L1": 3} and res.pending == 0
    assert [s.domain for s in res.sites] == ["brass.example.ru"]  # 2gis is a directory, each domain once
    assert "платных 3" in res.reason and "новых компаний 1" in res.reason

    (cp,) = res.candidates
    assert cp.inn == INN and check_counterparty(cp, cache.page_text) == []
    assert cp.contacts["phone"] == "+7 (812) 000-00-07" and cp.contacts["email"] == "sales@brass.example.ru"
    assert "000-00-08" not in cp.model_dump_json() and "Сидоров" not in cp.model_dump_json()  # the manager
    by_source = {e.source for e in cp.evidence}
    assert {site.SOURCE, yandex_search.SOURCE, pb_nalog.SOURCE} <= by_source
    assert all(e.url and e.fetched_at for e in cp.evidence)

    fit = res.fits[INN]
    assert (fit.value, fit.step) == (1.0, "site_name") and "Насосы дренажные" in fit.basis
    decision = res.roles[INN]
    assert decision.kind == "distributor" and decision.basis is not None and decision.basis.source == "site"
    assert "официальный дистрибьютор" in decision.basis.why and not decision.conflict  # OKVED 33.12 → unknown
    assert [b.source for b in decision.bases] == ["site", "okved"]

    again = W.lot_candidates("L1", LOT_TEXT, 2, client=_forbidden(), **kw)  # everything is cached now
    assert [c.inn for c in again.candidates] == [INN] and again.requests_live == 0 and budget.used() == 3


def test_fallback_stops_at_the_ceiling_of_the_lot_and_of_the_run(yandex_on, tmp_path, fixture_processed):
    cache = SourceCache(tmp_path / "c.sqlite")
    _site(cache)
    calls: list[httpx.Request] = []
    budget, caps = yandex_search.Budget(limit=10), yandex_search.Caps(per_lot=2, per_run=3)
    kw = {"cache": cache, "budget": budget, "caps": caps, "cfg": CFG, "processed": fixture_processed,
          "client": _yandex(calls), "site_client": _forbidden(), "offline": False}
    first = W.lot_candidates("L1", LOT_TEXT, 0, **kw)
    assert first.requests_live == 2 and "на лот" in first.reason and len(calls) == 2
    second = W.lot_candidates("L2", "Задвижки чугунные", 0, **kw)
    assert second.requests_live == 1 and "на прогон" in second.reason and len(calls) == 3
    third = W.lot_candidates("L3", "Клапаны обратные", 0, **kw)
    assert third.requests_live == 0 and third.candidates == [] and len(calls) == 3 and budget.used() == 3


def test_fallback_stops_at_the_hard_limit(yandex_on, tmp_path, fixture_processed):
    cache = SourceCache(tmp_path / "c.sqlite")
    _site(cache)
    calls: list[httpx.Request] = []
    budget = yandex_search.Budget(limit=1)
    res = W.lot_candidates("L1", LOT_TEXT, 0, cache=cache, budget=budget, caps=yandex_search.Caps(3, 10), cfg=CFG,
                           processed=fixture_processed, client=_yandex(calls), site_client=_forbidden(),
                           offline=False)
    assert res.requests_live == 1 and len(calls) == 1 and "лимит 1 запросов исчерпан" in res.reason
    assert [c.inn for c in res.candidates] == [INN]  # the answer that was paid for is still used


def test_fallback_deferred_submits_first_and_collects_on_the_next_run(yandex_on, tmp_path, fixture_processed,
                                                                      monkeypatch):
    monkeypatch.setenv("YANDEX_SEARCH_MODE", "deferred")
    cache = SourceCache(tmp_path / "c.sqlite")
    _site(cache)
    calls: list[httpx.Request] = []
    budget = yandex_search.Budget(limit=10)
    kw = {"okpd2": ["28.13.14"], "cache": cache, "budget": budget, "cfg": CFG, "processed": fixture_processed,
          "client": _yandex(calls), "site_client": _forbidden(), "offline": False}
    first = W.lot_candidates("L1", LOT_TEXT, 0, caps=yandex_search.Caps(3, 10), **kw)
    assert (first.requests_live, first.pending, first.candidates) == (3, 3, [])
    assert [c.method for c in calls] == ["POST"] * 3 and budget.used() == 3
    assert "ответ ещё не готов у 3" in first.reason
    second = W.lot_candidates("L1", LOT_TEXT, 0, caps=yandex_search.Caps(3, 10), **kw)  # the next run
    assert [c.method for c in calls[3:]] == ["GET"] * 3 and budget.used() == 3  # collecting is not paid again
    assert (second.requests_live, second.pending) == (0, 0) and [c.inn for c in second.candidates] == [INN]


def test_fallback_offline_reads_the_cache_only(yandex_on, tmp_path, fixture_processed, monkeypatch):
    cache = SourceCache(tmp_path / "c.sqlite")
    _site(cache)
    budget = yandex_search.Budget(limit=10)
    kw = {"cache": cache, "budget": budget, "cfg": CFG, "processed": fixture_processed, "client": _forbidden(),
          "site_client": _forbidden()}  # offline comes from DISCOVERY_OFFLINE=1 of conftest
    empty = W.lot_candidates("L1", LOT_TEXT, 0, **kw)
    assert empty.used and empty.candidates == [] and empty.pending == 0 and budget.used() == 0
    xml = (YFIX / "operation_answer.xml").read_text(encoding="utf-8")
    cache.put(yandex_search.search_url(LOT_QUERIES[1], 0, "2"), yandex_search.flatten(LOT_QUERIES[1], 0, "2", xml))
    warm = W.lot_candidates("L1", LOT_TEXT, 0, **kw)
    assert [c.inn for c in warm.candidates] == [INN] and budget.used() == 0


def test_fallback_site_fact_that_fails_evidence_is_not_added(yandex_on, tmp_path, fixture_processed, monkeypatch):
    cache = SourceCache(tmp_path / "c.sqlite")
    _site(cache)
    made_up = W.site_facts.SiteFacts(url=HOME)
    made_up.phones.append(W.Evidence(source=site.SOURCE, url=HOME, fetched_at=W.datetime.now(W.UTC),
                                     fact="Телефон на сайте", field="phone", value="+7 (812) 999-99-99"))
    monkeypatch.setattr(W.site_facts, "merge", lambda pages: made_up)
    res = W.lot_candidates("L1", LOT_TEXT, 0, cache=cache, budget=yandex_search.Budget(limit=10),
                           caps=yandex_search.Caps(3, 10), cfg=CFG, processed=fixture_processed,
                           client=_yandex([]), site_client=_forbidden(), offline=False)
    (cp,) = res.candidates
    assert "999-99-99" not in cp.model_dump_json() and "phone" not in cp.contacts
    assert check_counterparty(cp, cache.page_text) == []


def test_fallback_takes_no_contacts_of_an_individual_from_a_site(tmp_path):
    cache = SourceCache(tmp_path / "c.sqlite")
    ip = "500100732259"  # 12 digits: an individual entrepreneur
    page = cache.put(HOME, site.page_body(SITE_HOME + f"<p>ИНН {ip}</p>", HOME))
    ev = W.Evidence(source=site.SOURCE, url=HOME, fetched_at=W.datetime.fromtimestamp(page.fetched_at, tz=W.UTC),
                    fact=f"ИНН {ip} указан на сайте", field="inn", value=ip)
    s = W.Site(domain="brass.example.ru", home=HOME, search_url="", query="", pages=[HOME])
    out = W.Fallback(lot="L1", used=True, reason="")
    cp = W._with_site_facts(Counterparty(inn=ip, name="ИП Тест", evidence=[ev]), s, "Насосы дренажные", [], cache, out)
    assert cp.contacts == {} and all(e.field not in ("phone", "email", "address") for e in cp.evidence)
    assert out.roles[ip].kind == "distributor"  # what the site says about the business is kept
    org = W._with_site_facts(Counterparty(inn=INN, name="ООО Тест", evidence=[
        ev.model_copy(update={"value": INN, "fact": "ИНН указан на сайте"})]), s, "Насосы", [], cache, out)
    assert org.contacts == {}  # INN 7804428656 is not on this page: the card fails Evidence, nothing is added
    cache.put(HOME, site.page_body(SITE_HOME + f"<p>ИНН {INN}</p>", HOME))
    org = W._with_site_facts(Counterparty(inn=INN, name="ООО Тест", evidence=[
        ev.model_copy(update={"value": INN, "fact": "ИНН указан на сайте"})]), s, "Насосы", [], cache, out)
    assert org.contacts == {"phone": "+7 (812) 000-00-07", "email": "sales@brass.example.ru"}


# --- the plan of a run and its log line -----------------------------------------------------------------------

STRONG = {"L1": 0, "L2": 1, "L3": 2, "L4": 3, "L5": 7}


def test_plan_off_means_zero(caplog):
    plan = W.plan_run(STRONG)
    assert not plan.enabled and (plan.lots, plan.lots_yandex, plan.requests_max, plan.rub_max) == (5, 0, 0, 0.0)
    with caplog.at_level(logging.WARNING, logger=W.log.name):
        line = W.log_plan(plan)
    assert "выключен: нет ключа" in line and "запросов 0, 0 ₽" in line and line in caplog.text


@pytest.mark.parametrize(("per_lot", "per_run", "left", "mode", "requests", "rub"), [
    ("3", "100", 2500, "deferred", 9, 0.27),  # 3 lots × 3 queries
    ("2", "100", 2500, "deferred", 6, 0.18),  # the ceiling of the lot
    ("10", "100", 2500, "sync", 9, 4.39),  # a lot has only 3 queries; the sync price
    ("3", "4", 2500, "deferred", 4, 0.12),  # the ceiling of the run
    ("3", "100", 5, "deferred", 5, 0.15),  # the rest of the hard limit
    ("3", "100", 0, "deferred", 0, 0.0),
])
def test_plan_counts_lots_requests_and_roubles(per_lot, per_run, left, mode, requests, rub, yandex_on, monkeypatch,
                                               caplog):
    monkeypatch.setenv("YANDEX_MAX_PER_LOT", per_lot)
    monkeypatch.setenv("YANDEX_MAX_PER_RUN", per_run)
    plan = W.plan_run(STRONG, mode=mode, budget_left=left)
    assert plan.enabled and (plan.lots, plan.lots_yandex) == (5, 3)
    assert (plan.requests_max, plan.rub_max) == (requests, rub)
    with caplog.at_level(logging.WARNING, logger=W.log.name):
        line = W.log_plan(plan)
    assert "под Yandex попадают 3 из 5 лотов" in line and f"максимум {requests} запросов ≈ {rub:.2f} ₽" in line
    assert f"режим {mode}" in line and line in caplog.text


def test_plan_reads_the_rest_of_the_hard_limit(yandex_on):
    yandex_search.Budget().record(2497, "прежние")
    plan = W.plan_run(STRONG)
    assert plan.budget_left == 3 and plan.requests_max == 3 and plan.mode == "sync"
    assert W.plan_run({}).requests_max == 0 and W.plan_run({"L": 5}).lots_yandex == 0


# --- the order under the ceiling of the run: thin categories first (#263) ------------------------------------

THIN = frozenset({"21.20", "10.51"})
LOT_OKPD2 = {"L1": ["21.20.10.113"], "L2": ["26.20.15.000"], "L3": ["26.20.15.000", "10.51.11.110"], "L4": [],
             "L5": ["10.51.11.110"]}


def test_thin_classes_reads_the_metric_file(tmp_path, monkeypatch):
    path = tmp_path / "thin.json"
    path.write_text(json.dumps({"thin": [{"okpd2": "21.20"}, {"okpd2": "10.51"}]}), encoding="utf-8")
    assert W.thin_classes(path) == THIN
    assert W.thin_classes(tmp_path / "missing.json") == frozenset()  # no metric — no thin classes, no error
    monkeypatch.setattr(W, "THIN_RESULT", path)
    assert W.thin_classes() == THIN


def test_priority_order_puts_thin_lots_first_and_keeps_the_rest_in_place():
    assert W.priority_order(LOT_OKPD2, THIN) == ["L1", "L3", "L5", "L2", "L4"]  # any code of the lot may be thin
    assert W.priority_order(LOT_OKPD2, frozenset()) == list(LOT_OKPD2)  # no metric — the order as it was
    assert W.priority_order({}, THIN) == []


def test_under_the_ceiling_of_the_run_thin_lots_are_served_first(yandex_on, tmp_path, fixture_processed):
    cache = SourceCache(tmp_path / "c.sqlite")
    _site(cache)
    calls: list[httpx.Request] = []
    kw = {"cache": cache, "budget": yandex_search.Budget(limit=10), "caps": yandex_search.Caps(per_lot=3, per_run=3),
          "cfg": CFG, "processed": fixture_processed, "client": _yandex(calls), "site_client": _forbidden(),
          "offline": False}
    lots = {"L1": ("Задвижки чугунные", ["26.20.15.000"]), "L2": (LOT_TEXT, ["21.20.10.113"])}  # only L2 is thin
    served = {lot: W.lot_candidates(lot, lots[lot][0], 0, okpd2=lots[lot][1], **kw).requests_live
              for lot in W.priority_order({k: v[1] for k, v in lots.items()}, THIN)}
    assert served == {"L2": 3, "L1": 0} and list(served) == ["L2", "L1"] and len(calls) == 3


def test_plan_counts_thin_lots_among_those_under_yandex(yandex_on):
    plan = W.plan_run(STRONG, okpd2_by_lot=LOT_OKPD2, thin=THIN)  # under Yandex: L1, L2, L3; thin among them: L1, L3
    assert plan.lots_yandex == 3 and "из них в тонких категориях: 2" in plan.line()
    assert "тонк" not in W.plan_run(STRONG).line()  # no OKPD2 of the lots — nothing is said about thin


def test_plan_says_when_the_thin_metric_is_missing(yandex_on, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(W, "THIN_RESULT", tmp_path / "missing.json")
    plan = W.plan_run(STRONG, okpd2_by_lot=LOT_OKPD2)
    with caplog.at_level(logging.WARNING, logger=W.log.name):
        line = W.log_plan(plan)
    assert "метрика тонких категорий недоступна, порядок прежний" in line and line in caplog.text


def test_group_run_is_behind_the_same_gate(tmp_path, monkeypatch, fixture_processed):
    monkeypatch.setattr(W, "processed_dir", lambda: fixture_processed)
    monkeypatch.setattr(W, "SourceCache", lambda: SourceCache(tmp_path / "c.sqlite"))
    monkeypatch.setattr(W, "RESULTS", tmp_path / "results")  # a broken gate must not write into the repository
    with pytest.raises(SystemExit):  # Yandex is off: the paid group run does not start
        W.main(["Y-test", "--groups", "21.20", "--max-requests", "1"])
    assert yandex_search.Budget().used() == 0


def test_group_counts_as_one_lot(yandex_on, tmp_path, fixture_processed):
    cache = SourceCache(tmp_path / "c.sqlite")
    _site(cache)
    cfg = {**CFG, "templates": ["{p} оптом", "{p} производитель", "{p} дилер"]}
    calls: list[httpx.Request] = []
    caps = yandex_search.Caps(per_lot=2, per_run=10)
    orig = yandex_search.search

    def search(*a, **kw):
        return orig(*a, **{**kw, "client": _yandex(calls)})

    with unittest.mock.patch.object(yandex_search, "search", search):
        rep, _ = W.run_group("21.20", cfg, cache, yandex_search.Budget(limit=10), fixture_processed, False, 50, set(),
                             caps=caps)
    assert rep.requests_live == 2 and len(calls) == 2 and caps.used_lot == {"group:21.20": 2}
