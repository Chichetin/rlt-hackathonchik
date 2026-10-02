"""GET /api/reviews/{inn} (#234): the company comes from the enrichment cache, its contacts only if the Evidence
check stands behind them, the answer is the module's result — 2GIS being down is a status of the answer, not a 5xx."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from src.api import guard
from src.api import reviews as reviews_api
from src.api.app import app
from src.contracts import Counterparty, Evidence
from src.enrich import enrich as enrich_mod
from src.sources import gis2
from src.sources.pacing import HostPacer
from tests.unit.test_reviews import GRACE, Site

INN = "7811777270"
PHONE = "+7(812)561-91-10"


def registry_company(*, backed: bool = True, name: str = GRACE) -> Counterparty:
    """A company with a phone contact; `backed` — Evidence stands behind it. `enrich()` gives no contacts today: this is
    the shape the endpoint is ready for, checked here."""
    now = datetime.now(UTC)
    ev = [Evidence(source="data:test", url="data:test#inn", fetched_at=now, fact=f"ИНН {INN}", field="inn", value=INN)]
    if backed:
        ev.append(Evidence(source="data:test", url="data:test#phone", fetched_at=now, fact="Телефон", field="phone",
                           value=PHONE))
    return Counterparty(inn=INN, name=name, region="Санкт-Петербург", contacts={"phone": PHONE}, evidence=ev)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(gis2, "PACER", HostPacer(0.0, gis2.BLOCK_COOLDOWN_S))
    site = Site()
    monkeypatch.setattr(gis2, "_client", lambda: site.client())
    monkeypatch.setattr(enrich_mod, "enrich", lambda inn, cache=None, offline=None: registry_company())
    with TestClient(app) as client:
        client.site = site  # type: ignore[attr-defined]
        yield client


def test_a_card_with_the_phone_of_the_registry_is_confirmed(api):
    r = api.get(f"/api/reviews/{INN}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["inn"] == INN and body["queried"] == "ГРЕЙС ТРЭЙД"
    card = body["cards"][0]
    assert (card["basis"], card["verified"]) == ("phone", True)
    quote = card["quotes"][0]
    assert quote["date"] == "2024-03-15" and quote["evidence"]["url"] == card["url"]
    assert "в подбор и рейтинг отзывы не входят" in body["disclaimer"].lower()


def test_a_contact_without_evidence_does_not_confirm_a_card(api, monkeypatch):
    monkeypatch.setattr(enrich_mod, "enrich", lambda inn, cache=None, offline=None: registry_company(backed=False))
    card = api.get(f"/api/reviews/{INN}").json()["cards"][0]
    assert (card["basis"], card["verified"]) == ("legal_name", False)


def test_wrong_checksum_is_422_and_unknown_company_is_404(api, monkeypatch):
    assert api.get("/api/reviews/7811777271").status_code == 422

    def missing(inn, cache=None, offline=None):
        raise KeyError(inn)

    monkeypatch.setattr(enrich_mod, "enrich", missing)
    assert api.get(f"/api/reviews/{INN}").status_code == 404


def test_the_site_being_down_is_a_status_not_a_server_error(api):
    api.site.status = 403  # type: ignore[attr-defined]
    r = api.get(f"/api/reviews/{INN}")
    assert r.status_code == 200 and r.json()["status"] == "unavailable"


def test_api_key_is_required_when_the_server_sets_one(api, monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    assert api.get(f"/api/reviews/{INN}").status_code == 401
    assert api.get(f"/api/reviews/{INN}", headers={"X-API-Key": "secret"}).status_code == 200


def test_busy_slots_answer_429_with_retry_after(api, monkeypatch):
    class Full:
        def take(self):
            raise guard.busy("отзывы запрашиваются другими пользователями, повторите позже")

    monkeypatch.setattr(reviews_api, "_slots", Full())
    r = api.get(f"/api/reviews/{INN}")
    assert r.status_code == 429 and r.headers["retry-after"] == guard.RETRY_AFTER_S


def test_the_endpoint_is_in_the_openapi_description(api):
    spec = api.get("/openapi.json").json()
    assert "/api/reviews/{inn}" in spec["paths"]
    assert "ReviewsResult" in spec["components"]["schemas"]


def test_a_natural_person_is_refused_before_any_registry_or_2gis_call(api, monkeypatch):
    def forbidden(inn, cache=None, offline=None):
        raise AssertionError("the registry must not be asked for an ИП")

    monkeypatch.setattr(enrich_mod, "enrich", forbidden)
    r = api.get("/api/reviews/781100000020")
    assert r.status_code == 200 and r.json()["status"] == "unsupported"
    assert api.site.paths == []  # type: ignore[attr-defined]
