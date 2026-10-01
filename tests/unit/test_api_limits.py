"""F-10 (#256): API key, queue ceiling and live-call limit on /api/enrich and /api/counterparty."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.contracts import Counterparty
from src.enrich import enrich as enrich_mod

INNS = ["7707083893", "7736050003", "7710140679", "7728168971"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/q.sqlite")
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    for name in ("API_KEY", "ENRICH_QUEUE_MAX", "COUNTERPARTY_LIVE_MAX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app_mod, "_engine", None)
    return TestClient(app_mod.app)


def _card(inn: str) -> Counterparty:
    return Counterparty(inn=inn, name=f"ООО {inn}", evidence=[])


def test_no_api_key_configured_keeps_the_demo_open(client):
    assert client.post(f"/api/enrich/{INNS[0]}").status_code == 202


def test_api_key_required_when_set(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    monkeypatch.setattr(enrich_mod, "enrich", lambda inn, *a, **k: _card(inn))
    for call in (lambda h: client.post(f"/api/enrich/{INNS[0]}", headers=h),
                 lambda h: client.get(f"/api/counterparty/{INNS[0]}", headers=h)):
        assert call({}).status_code == 401
        assert call({"X-API-Key": "wrong"}).status_code == 401
        assert call({"X-API-Key": "s3cret"}).status_code in (200, 202)
    assert client.get("/api/health").status_code == 200  # the rest stays open


def test_queue_ceiling_429_but_repeat_of_queued_inn_is_fine(client, monkeypatch):
    monkeypatch.setenv("ENRICH_QUEUE_MAX", "2")
    first = client.post(f"/api/enrich/{INNS[0]}")
    assert client.post(f"/api/enrich/{INNS[1]}").status_code == 202
    over = client.post(f"/api/enrich/{INNS[2]}")
    assert over.status_code == 429
    assert int(over.headers["Retry-After"]) > 0
    again = client.post(f"/api/enrich/{INNS[0]}")  # already queued: no new task, no 429
    assert again.status_code == 202
    assert again.json()["task_id"] == first.json()["task_id"]


def test_cached_counterparty_needs_no_live_slot(client, monkeypatch):
    monkeypatch.setenv("COUNTERPARTY_LIVE_MAX", "0")
    monkeypatch.setattr(enrich_mod, "enrich", lambda inn, *a, **k: _card(inn))  # answers offline
    assert client.get(f"/api/counterparty/{INNS[0]}").status_code == 200


def test_live_calls_are_limited(client, monkeypatch):
    monkeypatch.setenv("COUNTERPARTY_LIVE_MAX", "1")
    inside, release = threading.Event(), threading.Event()

    def fake(inn, *a, offline=None, **k):
        if offline:
            raise KeyError(inn)  # not in the cache
        inside.set()
        release.wait(5)
        return _card(inn)

    monkeypatch.setattr(enrich_mod, "enrich", fake)
    first: list[int] = []
    t = threading.Thread(target=lambda: first.append(client.get(f"/api/counterparty/{INNS[0]}").status_code))
    t.start()
    try:
        assert inside.wait(5)
        second = client.get(f"/api/counterparty/{INNS[1]}")
        assert second.status_code == 429
        assert "Retry-After" in second.headers
    finally:
        release.set()
        t.join(5)
    assert first == [200]
    assert client.get(f"/api/counterparty/{INNS[1]}").status_code == 200  # the slot was released
