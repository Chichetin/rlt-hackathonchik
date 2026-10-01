"""Company site → INN (src/sources/site.py) and the discovery funnel (src/enrich/web_discover.py), #194.

Sites are synthetic HTML served by httpx.MockTransport or put into the cache; the registry card is the
recorded pb.nalog.ru card of tests/fixtures/pb_nalog (7804428656). No network.
"""

from collections import Counter
from pathlib import Path

import httpx
import pytest

from src.contracts import Counterparty, inn_is_valid
from src.enrich import web_discover as W
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
    assert INN in cache.page_text(HOME + "rekvizity/")
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
