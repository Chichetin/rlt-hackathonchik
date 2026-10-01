"""#38: cached enrichment is mixed into /api/search history before the hard filters; no network in the request."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.contracts import Counterparty, CounterpartyMatch, Evidence, ScoreComponent
from src.match import search as search_mod
from src.sources import bo_nalog, pb_nalog, rmsp
from src.sources.cache import SourceCache
from src.worker.queue import engine_from_env, tasks

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "pb_nalog"
CACHED = "7804428656"  # card in fixtures
UNCACHED = "7801000012"
Q = {"query_id": "q", "text": "x"}


def _match(inn: str) -> CounterpartyMatch:
    ev = Evidence(source="data:participants", url="data:participants#1", fetched_at=datetime.now(UTC),
                  fact="ИНН", field="inn", value=inn)
    cp = Counterparty(inn=inn, name=f"ИНН {inn}", kpp="780601001", stats={"lots_won": 3}, evidence=[ev])
    return CounterpartyMatch(counterparty=cp, score=0.5, origin="history", components=[
        ScoreComponent(name="product_fit", value=0.5, weight=0.35, explanation="x")])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/q.sqlite")
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.setattr(app_mod, "_engine", None)
    monkeypatch.setattr(search_mod, "search", lambda q, k=20: [_match(CACHED), _match(UNCACHED)])

    calls: list[str] = []  # the API swallows source errors, so the guard records instead of only raising

    def no_network(*a, **k):
        calls.append("network")
        raise AssertionError("network call inside /api/search")

    def only_offline(real):
        def wrapper(*a, **k):
            if not k.get("offline"):
                no_network()
            return real(*a, **k)
        return wrapper
    monkeypatch.setattr(pb_nalog, "fetch_card", no_network)
    monkeypatch.setattr(rmsp, "fetch_quick", only_offline(rmsp.fetch_quick))
    monkeypatch.setattr(bo_nalog, "fetch", only_offline(bo_nalog.fetch))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)

    def put(status_from: str | None = None, status_to: str | None = None, msp: bool = True) -> None:
        body = (FIX / f"{CACHED}.txt").read_text(encoding="utf-8")
        if status_to:
            body = body.replace(status_from or "", status_to)
        if not msp:
            body = body.replace("vyp.rsmpcategory: 1", "vyp.rsmpcategory: 0")
        SourceCache().put(pb_nalog.card_url(CACHED), body)
    return TestClient(app_mod.app), put, calls


def _inns(r) -> list[str]:
    return [m["counterparty"]["inn"] for m in r.json()["history"]]


def test_cached_card_is_mixed_in_and_uncached_is_queued(env):
    client, put, calls = env
    put()
    r = client.post("/api/search", json=Q)
    by = {m["counterparty"]["inn"]: m["counterparty"] for m in r.json()["history"]}
    a, b = by[CACHED], by[UNCACHED]
    assert a["name"] == 'ООО "БРАСС"' and a["status"] == "Действующая организация" and a["is_msp"] is True
    assert a["stats"]["lots_won"] == 3 and a["kpp"] == "780601001" and a["evidence"]  # organizers' data kept
    assert b["is_msp"] is None and b["status"] is None and b["name"] == f"ИНН {UNCACHED}"
    with engine_from_env().connect() as c:
        keys = [row.key for row in c.execute(tasks.select())]
    assert keys == [UNCACHED]  # only the uncached INN is queued
    assert calls == []  # no network in the request
    client.post("/api/search", json=Q)  # idempotent
    with engine_from_env().connect() as c:
        assert len(list(c.execute(tasks.select()))) == 1


def test_liquidated_cached_candidate_is_dropped(env):
    client, put, calls = env
    put("Действующая организация", "Ликвидирована")
    assert _inns(client.post("/api/search", json=Q)) == [UNCACHED]


def test_only_msp_uses_cached_flag(env):
    client, put, calls = env
    q = {**Q, "filters": {"only_msp": True}}
    put()
    assert _inns(client.post("/api/search", json=q)) == [CACHED]  # uncached: unknown -> hidden (D-030)
    put(msp=False)
    assert _inns(client.post("/api/search", json=q)) == []
