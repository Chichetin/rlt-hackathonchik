"""enrich(inn) while pb.nalog.ru asks for a captcha: rmsp by INN, then bo (#58, D-052).

Answers are real, recorded 01.10 (tests/fixtures/enrich_fallback/README.md); the network is mocked."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.contracts import Evidence
from src.enrich.enrich import enrich
from src.enrich.evidence import check_counterparty
from src.sources import bo_nalog, pb_nalog, rmsp
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "enrich_fallback"
MSP_INN = "7804428656"  # ООО «БРАСС» — a subject of МСП
BIG_INN = "7736050003"  # ПАО «Газпром» — not in the МСП registry


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.setenv("PB_DISABLED", "1")  # pb is on a captcha: not a single request to it
    monkeypatch.setattr(rmsp, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(bo_nalog, "MIN_INTERVAL_S", 0.0)
    return SourceCache()


def seed(cache: SourceCache, *, msp: bool = True, big_rmsp: bool = True, bo: bool = True) -> None:
    if msp:
        a = _load("rmsp_quick_7804428656.json")
        cache.put(rmsp.quick_url(MSP_INN), rmsp.quick_flatten(MSP_INN, a), fetched_at=a["recorded_at"])
    if big_rmsp:
        a = _load("rmsp_quick_7736050003.json")
        cache.put(rmsp.quick_url(BIG_INN), rmsp.quick_flatten(BIG_INN, a), fetched_at=a["recorded_at"])
    if bo:
        a = _load("bo_7736050003.json")
        cache.put(bo_nalog.search_url(BIG_INN), bo_nalog.flatten(BIG_INN, a), fetched_at=a["recorded_at"])


def test_msp_from_rmsp_with_evidence(cache):
    seed(cache)
    cp = enrich(MSP_INN)
    assert cp.inn == MSP_INN and cp.is_msp is True
    assert cp.name == 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "БРАСС"'
    assert cp.region == "Санкт-Петербург" and cp.okved == ["33.12"]
    assert {e.field for e in cp.evidence} >= {"inn", "ogrn", "name", "msp", "okved", "region"}
    assert {e.source for e in cp.evidence} == {rmsp.SOURCE}
    assert all(e.url == rmsp.quick_url(MSP_INN) for e in cp.evidence)
    assert check_counterparty(cp, cache.page_text) == []


def test_not_msp_name_from_bo(cache):
    seed(cache)
    cp = enrich(BIG_INN)
    assert cp.name == 'ПАО "ГАЗПРОМ"' and cp.status == "действующая" and cp.okved == ["46.71.4"]
    assert cp.is_msp is False  # rmsp answered with no row for this INN
    by_field = {e.field: e for e in cp.evidence}
    assert by_field["name"].source == bo_nalog.SOURCE and by_field["msp"].source == rmsp.SOURCE
    assert check_counterparty(cp, cache.page_text) == []


def test_not_found_anywhere_is_200_without_name_or_invented_facts(cache):
    seed(cache, bo=False)
    cache.put(bo_nalog.search_url(BIG_INN), bo_nalog.flatten(BIG_INN, {"content": []}))
    cp = enrich(BIG_INN)
    assert cp.name == f"ИНН {BIG_INN}" and cp.is_msp is False and cp.status is None
    assert [e.field for e in cp.evidence] == ["msp"]
    assert "не значит, что компании нет" in cp.evidence[0].fact
    assert check_counterparty(cp, cache.page_text) == [f"ИНН {BIG_INN} без Evidence с field='inn'"]


def test_pb_card_in_cache_wins(cache):
    seed(cache)
    cache.put(pb_nalog.card_url(MSP_INN),
              (Path(__file__).resolve().parents[1] / "fixtures" / "pb_nalog" / f"{MSP_INN}.txt").read_text("utf-8"))
    cp = enrich(MSP_INN)
    assert cp.name == 'ООО "БРАСС"' and {e.source for e in cp.evidence} == {pb_nalog.SOURCE}


def test_captcha_is_remembered_and_fallback_answers(cache, monkeypatch):
    monkeypatch.delenv("PB_DISABLED")
    calls = []

    def captcha(inn, client=None):
        calls.append(inn)
        raise pb_nalog._captcha("капча")

    monkeypatch.setattr(pb_nalog, "fetch_raw", captcha)
    seed(cache)
    assert enrich(MSP_INN).is_msp is True and calls == [MSP_INN]
    # the second INN: the captcha is remembered inside fetch_raw (here replaced) — emulate via the flag
    assert pb_nalog.blocked()


def test_pb_blocked_flag_sends_no_request(monkeypatch):
    pb_nalog.PACER.block(10**12)  # reset after the test by the autouse fixture in conftest.py
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: pytest.fail("request to a blocked host"))
    with pytest.raises(pb_nalog.CaptchaRequired):
        pb_nalog.fetch_raw(MSP_INN)


def test_live_rmsp_quick_request_and_cache(cache, monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_load("rmsp_quick_7804428656.json"))

    monkeypatch.setattr(rmsp, "_client", lambda *_: httpx.Client(transport=httpx.MockTransport(handler)))
    cp = enrich(MSP_INN)
    assert cp.is_msp is True and len(seen) == 1
    form = dict(x.split("=") for x in seen[0].content.decode().split("&"))
    assert form == {"mode": "quick", "page": "1", "query": MSP_INN, "pageSize": "10"}
    enrich(MSP_INN)
    assert len(seen) == 1  # the second call is from the cache


def test_rmsp_refusal_goes_to_bo_and_blocks_rmsp(cache, monkeypatch):
    seed(cache, big_rmsp=False)
    monkeypatch.setattr(rmsp, "_client", lambda *_: httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(403, text="forbidden"))))
    cp = enrich(BIG_INN)
    assert cp.name == 'ПАО "ГАЗПРОМ"' and cp.is_msp is None  # rmsp did not answer: МСП is not known
    assert rmsp.blocked()


def test_all_sources_unavailable_raises_pb_error(cache, monkeypatch):
    refuse = lambda r: httpx.Response(403, text="forbidden")  # noqa: E731
    monkeypatch.setattr(rmsp, "_client", lambda *_: httpx.Client(transport=httpx.MockTransport(refuse)))
    monkeypatch.setattr(bo_nalog, "_client", lambda *_: httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(pb_nalog.CaptchaRequired):
        enrich(MSP_INN)


def test_offline_uses_only_cache(cache, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    monkeypatch.setattr(rmsp, "_client", lambda *_: pytest.fail("network in offline mode"))
    seed(cache)
    assert enrich(MSP_INN).is_msp is True
    with pytest.raises(KeyError):
        enrich("7707049388")


def test_bo_flatten_keeps_only_this_inn_and_strips_markup():
    a = {"content": [
        {"inn": f"<strong>{BIG_INN}</strong>", "shortName": "ПАО \"ГАЗПРОМ\"", "statusCode": "ACTIVE"},
        {"inn": "7707049388", "shortName": "ЧУЖАЯ"},
    ]}
    text = bo_nalog.flatten(BIG_INN, a)
    assert "ЧУЖАЯ" not in text and "<strong>" not in text and "answer.inn_rows: 1" in text


def test_rmsp_quick_flatten_filters_rows_and_counts():
    a = _load("rmsp_quick_7804428656.json")
    assert "answer.inn_rows: 1" in rmsp.quick_flatten(MSP_INN, a)
    assert "answer.inn_rows: 0" in rmsp.quick_flatten("7707049388", a)


def test_fabricated_name_is_caught(cache):
    seed(cache)
    cp = enrich(MSP_INN)
    fake = Evidence(source=rmsp.SOURCE, url=rmsp.quick_url(MSP_INN), fetched_at=cp.evidence[0].fetched_at,
                    fact="Наименование: ООО Ромашка", field="name", value="ООО Ромашка")
    assert check_counterparty(cp.model_copy(update={"evidence": [*cp.evidence, fake]}), cache.page_text)


def test_inactive_registry_row_is_not_msp(cache):
    a = _load("rmsp_quick_7804428656.json")
    a["data"][0]["is_active"] = 0
    cache.put(rmsp.quick_url(MSP_INN), rmsp.quick_flatten(MSP_INN, a))
    cp = enrich(MSP_INN)
    assert cp.is_msp is False and check_counterparty(cp, cache.page_text) == []


def test_api_counterparty_200_on_fallback(cache):
    seed(cache)
    r = TestClient(app_mod.app).get(f"/api/counterparty/{MSP_INN}")
    assert r.status_code == 200
    body = r.json()
    assert body["is_msp"] is True and body["name"].endswith('"БРАСС"') and body["inn"] == MSP_INN
    assert {e["source"] for e in body["evidence"]} == {rmsp.SOURCE}


def test_api_503_only_when_nothing_answers(cache, monkeypatch):
    refuse = lambda r: httpx.Response(403, text="forbidden")  # noqa: E731
    monkeypatch.setattr(rmsp, "_client", lambda *_: httpx.Client(transport=httpx.MockTransport(refuse)))
    monkeypatch.setattr(bo_nalog, "_client", lambda *_: httpx.Client(transport=httpx.MockTransport(refuse)))
    assert TestClient(app_mod.app).get(f"/api/counterparty/{MSP_INN}").status_code == 503




def test_msp_flag_only_from_what_the_page_says():
    """Active row with an unknown category is not «не действующая»: no fact, is_msp unknown."""
    from src.enrich.enrich import _msp

    facts: list[tuple[str, str, str]] = []

    def add(field: str, value: str, fact: str) -> bool:
        facts.append((field, value, fact))
        return True

    assert _msp({"is_active": "1", "category": ""}, add) is None and facts == []
    assert _msp({"is_active": "1", "category": "?"}, add) is None and facts == []
    assert _msp({"is_active": "0", "category": "1"}, add) is False
    assert facts == [("msp", "is_active: 0", "Реестр МСП: запись есть, но не действующая")]
