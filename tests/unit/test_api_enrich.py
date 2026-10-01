"""#38: cached enrichment is mixed into /api/search history before the hard filters; no network in the request."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.contracts import Counterparty, CounterpartyMatch, Evidence, ScoreComponent
from src.match import pipeline
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


# --- review of PR #96: queue order, page-only queueing, failed stays failed, invalid INNs, kind, ГИР БО status


def _inn10(n: int) -> str:
    """A valid 10-digit legal-entity INN (78xxxxxxx + check digit)."""
    body = f"78{n:07d}"
    w = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    return body + str(sum(int(d) * k for d, k in zip(body, w, strict=True)) % 11 % 10)


def _keys() -> list[str]:
    with engine_from_env().connect() as c:
        return [row.key for row in c.execute(tasks.select().order_by(tasks.c.id))]


def _statuses() -> dict[str, str]:
    with engine_from_env().connect() as c:
        return {row.key: row.status for row in c.execute(tasks.select())}


def test_new_companies_are_queued_before_history(env, monkeypatch):
    from src.enrich import discover as discover_mod

    client, put, calls = env
    put()
    new_inn = _inn10(999)
    assert new_inn != UNCACHED

    def fake_discover(query, history, *, enqueue=None, **k):
        assert enqueue is not None
        enqueue(new_inn)
        cp = Counterparty(inn=new_inn, name="ООО НОВАЯ")
        m = CounterpartyMatch(counterparty=cp, score=0.4, origin="discovered", components=[])
        return discover_mod.Discovery(matches=[m], history_checked=True)
    monkeypatch.setattr(discover_mod, "discover", fake_discover)
    body = client.post("/api/search", json=Q).json()
    assert [m["counterparty"]["name"] for m in body["discovered"]] == ["ООО НОВАЯ"]
    assert _keys()[0] == new_inn  # «Новые» first: the worker takes them before the history INNs
    assert _keys() == [new_inn, UNCACHED]


def test_only_the_visible_page_is_queued(env, monkeypatch):
    client, put, calls = env
    inns = [_inn10(i) for i in range(1, 31)]
    monkeypatch.setattr(search_mod, "search", lambda q, k=20: [_match(i) for i in inns])
    r = client.post("/api/search?limit=5&offset=0", json=Q)
    assert r.headers["X-Total-History"] == "30"
    assert _keys() == inns[:5]  # not 30: the rest is queued when the user opens it
    client.post("/api/search?limit=5&offset=5", json=Q)  # «Показать ещё»
    assert _keys() == inns[:10]
    client.post("/api/export", json=Q)  # the xlsx takes every row but queues nothing
    assert _keys() == inns[:10]


def test_failed_task_stays_failed_after_repeated_search(env):
    from src.worker import queue as Q_

    client, put, calls = env
    put()  # only UNCACHED goes to the queue
    client.post("/api/search", json=Q)
    eng = engine_from_env()
    for _ in range(Q_.MAX_ATTEMPTS):
        task = Q_.claim(eng, "enrich")
        assert task is not None and task[1] == UNCACHED
        Q_.finish(eng, task[0], error="not found")
    assert _statuses() == {UNCACHED: "failed"}
    client.post("/api/search", json=Q)
    client.post("/api/search", json=Q)
    assert _statuses() == {UNCACHED: "failed"}  # the search does not revive it: no repeated registry hits
    assert Q_.claim(eng, "enrich") is None
    client.post(f"/api/enrich/{UNCACHED}")  # an explicit request still retries
    assert _statuses() == {UNCACHED: "pending"}


def test_invalid_inn_is_not_queued(env):
    # Counterparty rejects a bad INN, so this guards rows built without validation (model_construct)
    bad = "7801000013"
    good = _match(UNCACHED)
    cp = Counterparty.model_construct(**{**good.counterparty.model_dump(), "inn": bad})
    rows = [CounterpartyMatch.model_construct(**{**good.__dict__, "counterparty": cp}), good]
    uncached: set[str] = set()
    out = pipeline.enriched(rows, uncached)
    app_mod._queue_uncached(out, uncached)
    assert uncached == {UNCACHED} and _keys() == [UNCACHED]


def test_kind_from_the_cached_card_reaches_history_and_the_filter(env):
    client, put, calls = env
    put("33.12", "17.12")  # main OKVED 17.12 (paper) -> manufacturer, basis = the cached line (D-057)
    by = {m["counterparty"]["inn"]: m["counterparty"] for m in client.post("/api/search", json=Q).json()["history"]}
    assert by[CACHED]["kind"] == "manufacturer"
    assert any(e["field"] == "okved" and e["fact"].startswith("Роль") for e in by[CACHED]["evidence"])
    only = {**Q, "filters": {"only_manufacturers": True}}
    assert _inns(client.post("/api/search", json=only)) == [CACHED]


def test_role_without_a_verified_basis_does_not_pass_only_manufacturers(env, monkeypatch):
    from src.enrich import enrich as enrich_mod

    client, put, calls = env
    monkeypatch.setattr(enrich_mod, "enrich", lambda inn, cache=None, *, offline=None: Counterparty(
        inn=inn, name="ООО ВЫДУМКА", kind="manufacturer"))  # no Evidence for the role
    only = {**Q, "filters": {"only_manufacturers": True}}
    # strip_unverified runs before the filters: the row is not shown as a manufacturer it cannot back
    assert _inns(client.post("/api/search", json=only)) == []


@pytest.mark.parametrize(("code", "dropped"), [
    ("ACTIVE", False), ("LIQUIDATED", True), ("LIQUIDATING", True), ("BANKRUPT", True),
    ("TERMINATED", True), ("REORGANIZING", False), (None, False),
])
def test_bo_status_code_is_normalized_for_the_hard_filter(code, dropped):
    from src.contracts import Query
    from src.enrich.enrich import bo_status
    from src.match.filters import apply_filters

    st = bo_status(code)
    if code == "ACTIVE":
        assert st == "действующая"
    if code and not dropped and code != "ACTIVE":
        assert st == code  # unknown code is kept as is, not guessed
    m = _match(CACHED)
    m = m.model_copy(update={"counterparty": m.counterparty.model_copy(update={"status": st})})
    assert (apply_filters([m], Query(query_id="q", text="x")) == []) is dropped
