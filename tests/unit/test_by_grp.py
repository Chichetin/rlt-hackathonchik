"""Реестр плательщиков Беларуси (#200): a company by УНП through the cache, every fact checked against the cached
page; no network. The answer of 100063724 is the registry's real one, recorded by one request on 02.10.2026."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.enrich.evidence import check_counterparty, strip_unverified
from src.sources import by_grp
from src.sources.cache import SourceCache

ROOT = Path(__file__).resolve().parents[2]
RECORDED = ROOT / "configs" / "eaeu" / "grp"
FIX = ROOT / "tests" / "fixtures" / "by_grp"
UNP = "100063724"


def _answer() -> dict:
    return json.loads((RECORDED / f"{UNP}.json").read_text(encoding="utf-8"))["answer"]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(by_grp, "MIN_INTERVAL_S", 0.0)
    by_grp.PACER.reset()
    yield
    by_grp.PACER.reset()


@pytest.fixture
def cache(tmp_path) -> SourceCache:
    return SourceCache(tmp_path / "c.sqlite")


def _client(status: int, body: object, seen: list[httpx.Request] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_unp_control_digit():
    assert by_grp.unp_is_valid(UNP)
    assert not by_grp.unp_is_valid("100063725")
    assert not by_grp.unp_is_valid("7707083893")  # a Russian INN is not an УНП
    assert not by_grp.unp_is_valid("10006372")


def test_company_from_the_recorded_answer_passes_the_evidence_check(cache):
    seen: list[httpx.Request] = []
    page = by_grp.fetch(UNP, cache, _client(200, _answer(), seen), offline=False)
    assert len(seen) == 1 and seen[0].url.params["unp"] == UNP
    cp = by_grp.counterparty(UNP, page)
    assert cp is not None
    assert (cp.inn, cp.country, cp.foreign_id) == (None, "BY", f"BY:{UNP}")
    assert cp.name == 'ОАО "Управляющая компания холдинга "Белорусские обои"'
    assert cp.status == "Действующий" and by_grp.is_active(cp, page)
    assert cp.kind is None  # the registry holds no activity codes: the role is not guessed
    assert "Добруш" in cp.contacts["address"]
    assert check_counterparty(cp, cache.page_text) == []
    shown = strip_unverified(cp, cache.page_text)
    assert shown == cp
    assert {e.field for e in shown.evidence} == {"foreign_id", "name", "address", "status"}
    assert all(e.url == by_grp.page_url(UNP) and e.source == by_grp.SOURCE for e in shown.evidence)

    by_grp.fetch(UNP, cache, _client(500, {}, seen), offline=False)  # a fresh answer is not asked again
    assert len(seen) == 1
    assert by_grp.fetch(UNP, cache, offline=True) == page


def test_forged_identifier_is_stripped(cache):
    page = by_grp.fetch(UNP, cache, _client(200, _answer()), offline=False)
    cp = by_grp.counterparty(UNP, page)
    assert cp is not None
    other = "190000000"
    forged = cp.model_copy(update={"foreign_id": f"BY:{other}", "evidence": [
        e.model_copy(update={"value": f"BY:{other}"}) if e.field == "foreign_id" else e for e in cp.evidence]})
    assert check_counterparty(forged, cache.page_text)
    hidden = strip_unverified(forged, cache.page_text)
    assert (hidden.foreign_id, hidden.country) == (None, None)


def test_unknown_unp_is_not_a_company(cache):
    body = json.loads((FIX / "not_found.json").read_text(encoding="utf-8"))
    other = "100582333"  # passes the control digit
    page = by_grp.fetch(other, cache, _client(400, body), offline=False)
    assert "found: false" in page.body
    assert by_grp.counterparty(other, page) is None


def test_bad_unp_is_never_sent(cache):
    seen: list[httpx.Request] = []
    with pytest.raises(ValueError):
        by_grp.fetch("100063725", cache, _client(200, _answer(), seen), offline=False)
    assert seen == []


def test_offline_without_cache_and_refusals(cache):
    with pytest.raises(KeyError):
        by_grp.fetch(UNP, cache, offline=True)
    with pytest.raises(by_grp.Blocked):
        by_grp.fetch(UNP, cache, _client(403, {}), offline=False)
    seen: list[httpx.Request] = []
    with pytest.raises(by_grp.Blocked):  # the host is paused: nothing is sent
        by_grp.fetch(UNP, cache, _client(200, _answer(), seen), offline=False)
    assert seen == []
    by_grp.PACER.reset()
    wrong = {"row": {**_answer()["row"], "vunp": "100582333"}}  # an answer about another company is refused
    with pytest.raises(by_grp.Blocked):
        by_grp.fetch(UNP, cache, _client(200, wrong), offline=False)
    assert cache.page_text(by_grp.page_url(UNP)) is None


def test_seed_puts_recorded_answers_with_their_time(cache):
    assert by_grp.seed(RECORDED, cache) >= 1
    page = by_grp.fetch(UNP, cache, offline=True)
    rec = json.loads((RECORDED / f"{UNP}.json").read_text(encoding="utf-8"))
    assert page.fetched_at == float(rec["recorded_at"])
    cp = by_grp.counterparty(UNP, page)
    assert cp is not None and check_counterparty(cp, cache.page_text) == []
    assert by_grp.seed(RECORDED, cache) == 0  # a cached page is kept
