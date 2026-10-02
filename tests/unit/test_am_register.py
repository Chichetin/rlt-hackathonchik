"""Регистр юридических лиц Армении (#533): a company by its tax number through the cache, every fact checked against
the cached page; no network. The pages of 02226764 are the register's real ones, fetched on 02.10.2026."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from src.enrich.evidence import check_counterparty, strip_unverified
from src.sources import am_register as am
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "am_register"
TIN, CID = "02226764", "37478370"
SEARCH = (FIX / f"search_{TIN}.html").read_text(encoding="utf-8")
CARD = (FIX / f"card_{CID}.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(am, "MIN_INTERVAL_S", 0.0)
    am.PACER.reset()
    yield
    am.PACER.reset()


@pytest.fixture
def cache(tmp_path) -> SourceCache:
    return SourceCache(tmp_path / "c.sqlite")


def _client(search: str = SEARCH, card: str = CARD, seen: list[str] | None = None, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(status, text=search if "/search/" in request.url.path else card)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_company_from_the_real_pages_passes_the_evidence_check(cache):
    seen: list[str] = []
    pages = am.fetch(TIN, cache, _client(seen=seen), offline=False)
    assert seen == [am.search_url(TIN), am.card_url(CID)]
    cp = am.counterparty(TIN, pages)
    assert cp is not None
    assert (cp.inn, cp.country, cp.foreign_id, cp.kind) == (None, "AM", f"AM:{TIN}", None)
    assert cp.name == "«ԳՐԱՆԴ ՔԵՆԴԻ» ՍՊԸ" and cp.status == "Действующая" and am.is_active(TIN, pages)
    assert check_counterparty(cp, cache.page_text) == []
    assert strip_unverified(cp, cache.page_text) == cp
    assert {e.field for e in cp.evidence} == {"foreign_id", "name", "address", "status"}
    by_field = {e.field: e.url for e in cp.evidence}
    assert by_field["name"] == am.search_url(TIN) and by_field["foreign_id"] == am.card_url(CID)

    am.fetch(TIN, cache, _client(status=500, seen=seen), offline=False)  # fresh: not asked again
    assert len(seen) == 2
    assert am.fetch(TIN, cache, offline=True) == pages


def test_record_and_seed_round_trip(cache, tmp_path):
    rec = am.record(TIN, am.fetch(TIN, cache, _client(), offline=False))
    assert rec is not None and rec["card"]["tax_id"] == TIN
    import json

    (tmp_path / "rec").mkdir()
    (tmp_path / "rec" / f"{TIN}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    other = SourceCache(tmp_path / "other.sqlite")
    assert am.seed(tmp_path / "rec", other) == 1 and am.seed(tmp_path / "rec", other) == 0
    cp = am.counterparty(TIN, am.fetch(TIN, other, offline=True))
    assert cp is not None and check_counterparty(cp, other.page_text) == []


def test_no_single_company_is_not_a_company(cache):
    empty = SEARCH.replace(f'href="/hy/companies/{CID}"', 'href="/hy/nothing"')
    pages = am.fetch(TIN, cache, _client(search=empty), offline=False)
    assert pages[1] is None and am.counterparty(TIN, pages) is None and not am.is_active(TIN, pages)


def test_card_of_another_tax_number_is_refused(cache):
    pages = am.fetch("02226765", cache, _client(), offline=False)  # the search answered with Grand Candy's card
    assert pages[1] is None and am.counterparty("02226765", pages) is None


def test_bad_number_offline_and_refusal(cache):
    seen: list[str] = []
    with pytest.raises(ValueError):
        am.fetch("2226764", cache, _client(seen=seen), offline=False)
    assert seen == []
    with pytest.raises(KeyError):
        am.fetch(TIN, cache, offline=True)
    with pytest.raises(am.Blocked):
        am.fetch(TIN, cache, _client(status=403), offline=False)
    with pytest.raises(am.Blocked):  # the host is paused: nothing is sent
        am.fetch(TIN, cache, _client(seen=seen), offline=False)
    assert seen == []
