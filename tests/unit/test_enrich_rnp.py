"""enrich(inn): РНП ЕИС merged in (D-154, #111).

Электронный магазин СПб is explicitly NOT a source of enrich() (D-061): nothing here touches
its removed client (D-067).

A live РНП request costs the 60 s Crawl-delay of zakupki.gov.ru, so enrich() reads the РНП from the cache only
unless asked with `rnp_offline=False`: the worker, prefetch, the API and the search all get the cached answer, and
the РНП pass of the shortlist (`python -m src.worker.prefetch --rnp`, D-154) fills that cache."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.contracts import Counterparty
from src.enrich.enrich import _merge_rnp, enrich
from src.enrich.evidence import check_counterparty, strip_unverified
from src.sources import eis_rnp, pb_nalog
from src.sources.cache import SourceCache

RNP_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "eis_rnp"
POS_PAGE = (RNP_FIX / "inn_9717122721_3_entries.html").read_text(encoding="utf-8")
NEG_PAGE = (RNP_FIX / "inn_7804428656_no_results.html").read_text(encoding="utf-8")
IN_RNP = "9717122721"
NOT_IN_RNP = "7804428656"  # same INN as the pb.nalog.ru UL fixture (tests/fixtures/pb_nalog)

PB_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "pb_nalog"
UL = "7804428656"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.setattr(eis_rnp, "MIN_INTERVAL_S", 0.0)
    return SourceCache()


def _bare(inn: str) -> Counterparty:
    return Counterparty(inn=inn, name=f"ИНН {inn}")


# --- _merge_rnp ------------------------------------------------------------------------------


def test_merge_rnp_true_sets_in_rnp_and_evidence(cache):
    cache.put(eis_rnp.search_url(IN_RNP), POS_PAGE)
    cp = _merge_rnp(IN_RNP, _bare(IN_RNP), cache, True)
    assert cp.in_rnp is True
    (ev,) = [e for e in cp.evidence if e.field == "rnp"]
    assert ev.value == "№ Р2621504"
    # _bare() has no "inn" Evidence (not this test's concern): only the rnp-specific rule is checked
    assert "in_rnp" not in " ".join(check_counterparty(cp, cache.page_text))


def test_merge_rnp_false_is_dated(cache):
    cache.put(eis_rnp.search_url(NOT_IN_RNP), NEG_PAGE)
    cp = _merge_rnp(NOT_IN_RNP, _bare(NOT_IN_RNP), cache, True)
    assert cp.in_rnp is False
    (ev,) = [e for e in cp.evidence if e.field == "rnp"]
    assert ev.fact.startswith("Не найдена в РНП ЕИС на ")


def test_merge_rnp_keyerror_offline_without_cache_is_quiet(cache):
    base = _bare(IN_RNP)
    cp = _merge_rnp(IN_RNP, base, cache, True)
    assert cp == base  # not checked yet (worker queue) — never guessed, no exception


def test_merge_rnp_blocked_keeps_none_not_false(cache, monkeypatch):
    monkeypatch.delenv("ENRICH_OFFLINE", raising=False)

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda self, request: refuse(request))
    base = _bare(NOT_IN_RNP)
    cp = _merge_rnp(NOT_IN_RNP, base, cache, False)
    assert cp.in_rnp is None and cp == base  # Blocked is swallowed, never turned into False


def test_merge_rnp_invalid_inn_is_quiet_not_raised(cache):
    # an inn that fails the checksum must not crash enrich(); eis_rnp.check raises ValueError
    base = Counterparty.model_construct(inn=None, name="x", evidence=[])
    cp = _merge_rnp("7804428650", base, cache, True)
    assert cp == base


# --- enrich(): full wiring, offline guard ------------------------------------------------------


@pytest.fixture
def pb_cache(cache):
    cache.put(pb_nalog.card_url(UL), (PB_FIX / f"{UL}.txt").read_text(encoding="utf-8"))
    return cache


def test_enrich_merges_rnp_from_cache(pb_cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    pb_cache.put(eis_rnp.search_url(UL), NEG_PAGE)  # UL == NOT_IN_RNP
    cp = enrich(UL)
    assert cp.inn == UL and cp.name == 'ООО "БРАСС"'  # the pb card's own name wins
    assert cp.in_rnp is False
    assert any(e.field == "rnp" for e in cp.evidence)


def test_enrich_offline_true_never_touches_the_network(pb_cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    pb_cache.put(eis_rnp.search_url(UL), NEG_PAGE)

    def no_network(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"enrich(offline=True) made a network request: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    cp = enrich(UL, pb_cache, offline=True)
    assert cp.in_rnp is False  # the cached РНП page is still read and merged


def test_enrich_offline_true_without_rnp_cache_leaves_in_rnp_none(pb_cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda self, r: pytest.fail("no network expected"))
    cp = enrich(UL, pb_cache, offline=True)  # no РНП page cached for this INN
    assert cp.in_rnp is None


def test_rnp_offline_overrides_a_live_main_enrich(pb_cache, monkeypatch):
    """rnp_offline=True forces РНП cache-only even when the main card fetch may go live
    (src/api/app.py api_counterparty): zakupki.gov.ru must never be reached from this call."""
    monkeypatch.delenv("ENRICH_OFFLINE", raising=False)

    def guard(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        if "dishonestsupplier" in str(request.url):
            pytest.fail("rnp_offline=True must not reach zakupki.gov.ru")
        return httpx.Response(200, text="<html></html>")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guard)
    # the pb card is already cached (fresh): no live pb/rmsp/bo call happens either
    cp = enrich(UL, pb_cache, rnp_offline=True)
    assert cp.in_rnp is None  # nothing cached for РНП, cache-only -> quiet KeyError


def _zakupki_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def guard(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        if "dishonestsupplier" in str(request.url):
            asked.append(str(request.url))
            return httpx.Response(200, text=NEG_PAGE)
        return httpx.Response(200, text="<html></html>")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guard)
    return asked


def test_enrich_of_the_worker_never_asks_rnp_live(pb_cache, monkeypatch):
    """The worker and prefetch call `enrich(inn)`: the card may go live, РНП may not (60 s per INN, D-154)."""
    monkeypatch.delenv("ENRICH_OFFLINE", raising=False)
    asked = _zakupki_guard(monkeypatch)
    cp = enrich(UL, pb_cache)
    assert asked == [] and cp.in_rnp is None
    pb_cache.put(eis_rnp.search_url(UL), NEG_PAGE)  # after the РНП pass the same call reads its answer
    assert enrich(UL, pb_cache).in_rnp is False and asked == []


def test_enrich_asks_rnp_live_only_when_told(pb_cache, monkeypatch):
    monkeypatch.delenv("ENRICH_OFFLINE", raising=False)
    asked = _zakupki_guard(monkeypatch)
    assert enrich(UL, pb_cache, rnp_offline=False).in_rnp is False and len(asked) == 1


def test_a_page_that_breaks_the_parser_leaves_the_card_whole(cache, monkeypatch):
    def broken(*a: object, **kw: object) -> eis_rnp.RnpResult:
        raise IndexError("layout changed")

    monkeypatch.setattr(eis_rnp, "check", broken)
    cp = _merge_rnp(NOT_IN_RNP, _bare(NOT_IN_RNP), cache, offline=True)
    assert cp == _bare(NOT_IN_RNP)  # no 500 from /api/counterparty, in_rnp stays «не проверено»


# --- strip_unverified / check_counterparty on in_rnp (honest and forged) -----------------------


def test_strip_unverified_drops_forged_in_rnp_without_evidence(cache):
    cache.put(eis_rnp.search_url(IN_RNP), POS_PAGE)
    honest = _merge_rnp(IN_RNP, _bare(IN_RNP), cache, True)
    assert honest.in_rnp is True and strip_unverified(honest, cache.page_text).in_rnp is True

    forged = Counterparty(inn=None, name="ООО Выдумка", in_rnp=True)  # no rnp Evidence at all
    assert check_counterparty(forged, cache.page_text) == [
        "in_rnp=True без подтверждённого Evidence field='rnp'"]
    assert strip_unverified(forged, cache.page_text).in_rnp is None


def test_strip_unverified_drops_in_rnp_whose_evidence_value_was_tampered(cache):
    cache.put(eis_rnp.search_url(IN_RNP), POS_PAGE)
    cp = _merge_rnp(IN_RNP, _bare(IN_RNP), cache, True)
    tampered = cp.model_copy(update={"evidence": [
        e.model_copy(update={"value": "№ Р0000000"}) if e.field == "rnp" else e for e in cp.evidence]})
    assert check_counterparty(tampered, cache.page_text)
    assert strip_unverified(tampered, cache.page_text).in_rnp is None


# --- GET /api/counterparty/{inn}: a direct card lookup never reaches zakupki.gov.ru -------------


def test_api_counterparty_never_reaches_zakupki(pb_cache, tmp_path, monkeypatch):
    monkeypatch.delenv("ENRICH_OFFLINE", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    monkeypatch.setattr(app_mod, "_engine", None)

    def guard(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        if "dishonestsupplier" in str(request.url):
            pytest.fail("GET /api/counterparty must not reach zakupki.gov.ru (D-154, #111)")
        raise httpx.ConnectError("no other network expected in this test")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guard)
    r = TestClient(app_mod.app).get(f"/api/counterparty/{UL}")
    assert r.status_code == 200 and r.json()["in_rnp"] is None  # not cached, cache-only -> «не проверено»
