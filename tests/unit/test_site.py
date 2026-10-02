"""Company site → INN (src/sources/site.py, #194): visible text, labelled INNs with the checksum, requisite links,
robots.txt (src/sources/robots.py) and refusals that are not bypassed.

Sites are synthetic HTML served by httpx.MockTransport. No network.
"""

from collections import Counter

import httpx
import pytest

from src.contracts import inn_is_valid
from src.sources import site
from src.sources.cache import SourceCache

INN = "7804428656"
OTHER = "7707049388"
HOME = "https://brass.example.ru/"

PAGE_HOME = """<html><head><script>var inn = "7707049388";</script><style>.x{}</style></head><body>
<nav><a href="/catalog/">Каталог</a> <a href="/rekvizity/">Реквизиты</a> <a href="https://other.ru/contacts">x</a></nav>
<p>Поставка оборудования</p></body></html>"""
PAGE_REQ = f"<html><body><table><tr><td>ИНН/КПП</td><td>{INN}/780401001</td></tr></table></body></html>"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(site, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(site, "_pacers", {})


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


def test_pick_inn():
    assert site.pick_inn(Counter()) == (None, "на сайте нет ИНН")
    assert site.pick_inn(Counter({INN: 2}))[0] == INN
    assert site.pick_inn(Counter({INN: 3, OTHER: 1}))[0] == INN
    assert site.pick_inn(Counter({INN: 2, OTHER: 1}))[0] is None


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


def test_robots_wildcard_rule_is_obeyed(tmp_path):
    """robots.txt with `*` inside a path (src/sources/robots.py semantics): the matching page is not requested."""
    calls: list[str] = []
    client = _client({HOME + "robots.txt": (200, "User-agent: *\nDisallow: /*rekvizit\n"),
                      HOME: (200, PAGE_HOME), HOME + "rekvizity/": (200, PAGE_REQ)}, calls)
    res = site.site_inn(HOME, SourceCache(tmp_path / "c.sqlite"), client)
    assert res.inn is None and HOME + "rekvizity/" not in calls and HOME in calls


def test_offline_reads_the_cache_only(tmp_path):
    cache = SourceCache(tmp_path / "c.sqlite")
    cache.put(HOME, site.page_body(PAGE_REQ, HOME))
    calls: list[str] = []
    client = _client({}, calls)
    res = site.site_inn(HOME, cache, client, offline=True)
    assert res.inn == INN and res.url == HOME and calls == []


def test_redirect_within_the_site_is_followed(tmp_path):
    calls: list[str] = []
    pages = {HOME + "robots.txt": (404, ""), HOME: (200, PAGE_HOME), HOME + "rekvizity/": (301, ""),
             HOME + "about/rekvizity/": (200, PAGE_REQ)}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        status, body = pages.get(url, (404, ""))
        headers = {"content-type": "text/html; charset=utf-8"}
        if status == 301:
            headers["location"] = "/about/rekvizity/"
        return httpx.Response(status, text=body, headers=headers)

    res = site.site_inn(HOME, SourceCache(tmp_path / "c.sqlite"), httpx.Client(transport=httpx.MockTransport(handler)))
    assert res.inn == INN and HOME + "about/rekvizity/" in calls


def test_redirect_to_another_site_is_not_followed(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == HOME:
            return httpx.Response(302, headers={"location": "https://other.example/"})
        return httpx.Response(404)

    res = site.site_inn(HOME, SourceCache(tmp_path / "c.sqlite"), httpx.Client(transport=httpx.MockTransport(handler)))
    assert res.inn is None and "другой сайт" in res.reason
    assert not any(u.startswith("https://other.example") for u in calls)
