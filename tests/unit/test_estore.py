"""Электронный магазин СПб, реестр контрактов: parsing, Evidence on the cached page, 152-ФЗ."""

import time
from pathlib import Path

import httpx
import pytest

from src.enrich.evidence import check_counterparty, check_evidence
from src.sources import estore
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "estore"
OKPD_PAGE = (FIX / "okpd2_17.12.14_bumaga.html").read_text(encoding="utf-8")
SUPPLIER_PAGE = (FIX / "supplier_7820324037.html").read_text(encoding="utf-8")
OFFICEMAG = "7820324037"  # ООО «Офисмаг СПБ», real registry page fetched 01.10


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.setattr(estore, "MIN_INTERVAL_S", 0.0)
    c = SourceCache()
    c.put(estore.search_url("17.12.14", "бумага"), OKPD_PAGE)
    c.put(estore.supplier_url(OFFICEMAG), SUPPLIER_PAGE)
    return c


def test_parse_registry_page():
    page = estore.parse(OKPD_PAGE)
    assert page.total == 681 and len(page.rows) == 50
    first = page.rows[0]
    assert first.supplier_inn == OFFICEMAG
    assert first.supplier_name == 'Общество с ограниченной ответственностью "Офисмаг СПБ"'
    assert first.subject and first.customer


def test_discover_legal_entities_only_with_evidence(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    found = estore.discover("17.12.14", "бумага", cache)
    inns = [cp.inn for cp in found]
    assert len(inns) == len(set(inns)) == 18  # 36 legal-entity rows, 18 distinct
    assert "500100732259" not in inns  # individuals (12 digits) are not shown — 152-ФЗ
    assert all(cp.inn and len(cp.inn) == 10 for cp in found)
    office = next(cp for cp in found if cp.inn == OFFICEMAG)
    assert {e.field for e in office.evidence} >= {"inn", "name", "contract"}
    for cp in found:
        assert check_counterparty(cp, cache.page_text) == [], cp.inn


def test_bad_checksum_inn_is_unconfirmed(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    bad = OKPD_PAGE.replace(f"({OFFICEMAG})", "(7820324030)")
    cache.put(estore.search_url("17.12.14", "бумага"), bad)
    found = estore.discover("17.12.14", "бумага", cache)
    unconfirmed = [cp for cp in found if cp.inn is None]
    assert [cp.name for cp in unconfirmed] == ['Общество с ограниченной ответственностью "Офисмаг СПБ"']
    assert not any(e.field == "inn" for e in unconfirmed[0].evidence)
    assert check_counterparty(unconfirmed[0], cache.page_text) == []


def test_lookup_by_inn_gives_name_and_contract_count(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    cp = estore.lookup(OFFICEMAG, cache)
    assert cp is not None and cp.inn == OFFICEMAG
    assert cp.name == 'Общество с ограниченной ответственностью "Офисмаг СПБ"'
    count = next(e for e in cp.evidence if e.field == "contracts")
    assert count.value == "Всего: 9199" and "9199" in count.fact
    assert check_counterparty(cp, cache.page_text) == []


def test_discover_repeated_inn_keeps_the_first_contract_in_registry_order(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    first = next(r for r in estore.parse(OKPD_PAGE).rows if r.supplier_inn == OFFICEMAG)
    office = next(cp for cp in estore.discover("17.12.14", "бумага", cache) if cp.inn == OFFICEMAG)
    contract = next(e for e in office.evidence if e.field == "contract")
    assert contract.value == first.subject  # 7 rows of this supplier: the first one, not the last


def test_lookup_on_a_mixed_page_takes_own_rows_and_gives_no_total(cache, monkeypatch):
    # a page that is NOT this supplier's list (here: the product query page, 681 rows of many suppliers):
    # name from the supplier's own row; «Всего: 681» is not his contract count
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    other = "7811662431"  # 5 rows on the page, not the first supplier
    cache.put(estore.supplier_url(other), OKPD_PAGE)
    cp = estore.lookup(other, cache)
    assert cp is not None and cp.inn == other
    assert cp.name == next(r for r in estore.parse(OKPD_PAGE).rows if r.supplier_inn == other).supplier_name
    assert cp.name != 'Общество с ограниченной ответственностью "Офисмаг СПБ"'
    assert not any(e.field == "contracts" for e in cp.evidence)
    assert check_counterparty(cp, cache.page_text) == []


def test_lookup_inn_not_in_registry_is_none_not_a_guess(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    absent = "7804428656"  # valid INN, not on the page
    cache.put(estore.supplier_url(absent), OKPD_PAGE)
    assert estore.lookup(absent, cache) is None


def test_lookup_individual_is_not_fetched(cache):
    assert estore.lookup("500100732259", cache) is None


def test_lookup_rejects_bad_checksum(cache):
    with pytest.raises(ValueError):
        estore.lookup("7820324030", cache)


def test_forged_name_is_caught(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    cp = estore.lookup(OFFICEMAG, cache)
    assert cp is not None
    name = next(e for e in cp.evidence if e.field == "name")
    forged = name.model_copy(update={"value": 'ООО "Офисмаг Москва"'})
    assert check_evidence(forged, cache.page_text)


def test_live_fetch_is_cached_and_blocked_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "s.sqlite"))
    monkeypatch.delenv("DISCOVERY_OFFLINE", raising=False)  # tests/unit/conftest.py sets it for every test
    monkeypatch.setattr(estore, "MIN_INTERVAL_S", 0.0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=SUPPLIER_PAGE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    c = SourceCache()
    for _ in range(2):
        cp = estore.lookup(OFFICEMAG, c, client=client)
        assert cp is not None and cp.inn == OFFICEMAG
    assert len(calls) == 1 and "supplier=7820324037" in calls[0] and "limit=50" in calls[0]

    for status in (403, 429):
        blocked = httpx.Client(transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s, text="WAF")))
        with pytest.raises(estore.Blocked):
            estore.discover("26.20", "ноутбук", c, client=blocked)
        assert c.get(estore.search_url("26.20", "ноутбук"), allow_stale=True) is None  # errors not cached

    # 200 but not a registry page (captcha, maintenance): stop, nothing cached, no «компаний нет»
    foreign = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>captcha</html>")))
    with pytest.raises(estore.Blocked):
        estore.discover("26.20", "ноутбук", c, client=foreign)
    assert c.get(estore.search_url("26.20", "ноутбук"), allow_stale=True) is None


def test_requests_are_paced(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "p.sqlite"))
    monkeypatch.delenv("DISCOVERY_OFFLINE", raising=False)
    slept: list[float] = []
    monkeypatch.setattr(estore, "MIN_INTERVAL_S", 2.0)
    monkeypatch.setattr(estore, "_last_request", time.monotonic())
    monkeypatch.setattr(estore.time, "sleep", slept.append)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=SUPPLIER_PAGE)))
    estore.lookup(OFFICEMAG, SourceCache(), client=client)
    assert len(slept) == 1 and 1.5 < slept[0] <= 2.0


def test_offline_without_cache_raises(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    with pytest.raises(KeyError):
        estore.discover("26.20", "ноутбук", cache)
