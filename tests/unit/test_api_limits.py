"""F-10 (#256, D-099): the endpoints that spend the registries' patience have a key, a queue ceiling and a live cap."""

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.api import guard
from src.contracts import inn_is_valid
from src.worker import queue as Q

KEY = "s3cret-key"


def _inn(n: int) -> str:
    """The n-th valid 10-digit INN (the checksum of src/contracts.py)."""
    body = [int(c) for c in f"{n:09d}"]
    check = sum(w * d for w, d in zip((2, 4, 10, 3, 5, 9, 4, 6, 8), body, strict=True)) % 11 % 10
    inn = "".join(map(str, [*body, check]))
    assert inn_is_valid(inn)
    return inn


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("ENRICH_QUEUE_MAX", raising=False)
    monkeypatch.setattr(app_mod, "_engine", None)
    return TestClient(app_mod.app)


# --- API key -------------------------------------------------------------------------------------------------------

def test_without_a_configured_key_the_endpoints_stay_open(client):
    assert client.post(f"/api/enrich/{_inn(1)}").status_code == 202


WRONG_HEADERS = [{}, {"X-API-Key": "wrong"}, {"X-API-Key": KEY[:-1]}, {"X-API-Key": "ключ".encode()}]


@pytest.mark.parametrize("headers", WRONG_HEADERS)
def test_enqueue_needs_the_key_when_it_is_set(client, monkeypatch, headers):
    monkeypatch.setenv("API_KEY", KEY)
    r = client.post(f"/api/enrich/{_inn(2)}", headers=headers)
    assert r.status_code == 401 and "X-API-Key" in r.json()["detail"]
    assert Q.status_counts(app_mod._queue()) == {}  # nothing got into the queue


def test_enqueue_with_the_right_key(client, monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    assert client.post(f"/api/enrich/{_inn(3)}", headers={"X-API-Key": KEY}).status_code == 202


def test_counterparty_needs_the_key_before_it_touches_a_registry(client, monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    calls: list[str] = []

    def enrich(inn, *a, **k):
        calls.append(inn)
        raise KeyError(inn)

    monkeypatch.setattr(app_mod.enrich_mod, "enrich", enrich)
    assert client.get(f"/api/counterparty/{_inn(4)}").status_code == 401
    assert client.get(f"/api/counterparty/{_inn(4)}", headers={"X-API-Key": "wrong"}).status_code == 401
    assert calls == []
    assert client.get(f"/api/counterparty/{_inn(4)}", headers={"X-API-Key": KEY}).status_code == 404  # reached enrich
    assert calls == [_inn(4)]


def test_search_and_health_do_not_ask_for_the_key(client, monkeypatch):
    """The UI calls them without a key: only the two endpoints that reach the registries are guarded."""
    monkeypatch.setenv("API_KEY", KEY)
    assert client.get("/api/health").status_code == 200


# --- queue ceiling ---------------------------------------------------------------------------------------------------

def test_new_inn_over_the_ceiling_is_429_with_retry_after(client, monkeypatch):
    monkeypatch.setenv("ENRICH_QUEUE_MAX", "2")
    assert [client.post(f"/api/enrich/{_inn(i)}").status_code for i in (10, 11)] == [202, 202]
    r = client.post(f"/api/enrich/{_inn(12)}")
    assert r.status_code == 429 and r.headers["Retry-After"] == guard.RETRY_AFTER_S
    assert Q.status_counts(app_mod._queue()) == {"pending": 2}


def test_an_inn_already_in_the_queue_is_returned_not_refused(client, monkeypatch):
    monkeypatch.setenv("ENRICH_QUEUE_MAX", "1")
    first = client.post(f"/api/enrich/{_inn(20)}")
    again = client.post(f"/api/enrich/{_inn(20)}")
    assert again.status_code == 202 and again.json()["task_id"] == first.json()["task_id"]


def test_ceiling_counts_waiting_tasks_only(client, monkeypatch):
    monkeypatch.setenv("ENRICH_QUEUE_MAX", "1")
    eng = app_mod._queue()
    done = Q.enqueue(eng, "enrich", _inn(30))
    Q.finish(eng, done)
    assert Q.claim(eng, "enrich") is None and Q.status_counts(eng) == {"done": 1}
    assert client.post(f"/api/enrich/{_inn(31)}").status_code == 202  # a finished task takes no room


def test_garbage_ceiling_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ENRICH_QUEUE_MAX", "много")
    assert guard.queue_max() == guard.QUEUE_MAX_DEFAULT
    monkeypatch.setenv("ENRICH_QUEUE_MAX", "0")
    assert guard.queue_max() == 1  # zero would refuse everything; the smallest ceiling is one task


def test_queue_ceiling_in_enqueue_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'e.sqlite').as_posix()}")
    eng = Q.engine_from_env()
    a, b = _inn(40), _inn(41)
    assert Q.enqueue(eng, "enrich", a, max_pending=1) >= 1
    with pytest.raises(Q.QueueFull):
        Q.enqueue(eng, "enrich", b, max_pending=1)
    Q.enqueue(eng, "enrich", b)  # a batch job passes no ceiling
    assert Q.status_counts(eng) == {"pending": 2}


def test_reviving_a_failed_task_is_a_new_waiting_task(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'r.sqlite').as_posix()}")
    eng = Q.engine_from_env()
    failed = Q.enqueue(eng, "enrich", _inn(50))
    for _ in range(Q.MAX_ATTEMPTS):
        assert Q.claim(eng, "enrich") is not None
        Q.finish(eng, failed, "boom")
    assert Q.status_counts(eng) == {"failed": 1}
    Q.enqueue(eng, "enrich", _inn(51))  # one task is waiting
    with pytest.raises(Q.QueueFull):
        Q.enqueue(eng, "enrich", _inn(50), max_pending=1)
    assert Q.status_counts(eng) == {"failed": 1, "pending": 1}  # a refused revival changed nothing
    # no revival asked: the failed task stays failed and needs no room
    assert Q.enqueue(eng, "enrich", _inn(50), retry_failed=False, max_pending=1) == failed


# --- live cap of /api/counterparty -----------------------------------------------------------------------------------

def test_a_second_live_request_is_429_while_the_slot_is_taken(client, monkeypatch):
    monkeypatch.setattr(guard, "live_slots", guard.LiveSlots(1))
    seen: list[int] = []

    def enrich(inn, *a, **k):
        if inn == _inn(60):  # a request arrives while this one is inside the registry call
            seen.append(client.get(f"/api/counterparty/{_inn(61)}").status_code)
        raise KeyError(inn)

    monkeypatch.setattr(app_mod.enrich_mod, "enrich", enrich)
    assert client.get(f"/api/counterparty/{_inn(60)}").status_code == 404
    assert seen == [429]
    assert client.get(f"/api/counterparty/{_inn(61)}").status_code == 404  # the slot is free again


def test_the_slot_is_released_after_an_error(client, monkeypatch):
    monkeypatch.setattr(guard, "live_slots", guard.LiveSlots(1))

    def down(inn, *a, **k):
        raise TimeoutError("pb.nalog.ru")

    monkeypatch.setattr(app_mod.enrich_mod, "enrich", down)
    assert [client.get(f"/api/counterparty/{_inn(70)}").status_code for _ in range(3)] == [503, 503, 503]


def test_busy_answer_carries_retry_after(client, monkeypatch):
    slots = guard.LiveSlots(1)
    monkeypatch.setattr(guard, "live_slots", slots)
    with slots.take():
        r = client.get(f"/api/counterparty/{_inn(80)}")
    assert r.status_code == 429 and r.headers["Retry-After"] == guard.RETRY_AFTER_S


def test_openapi_documents_the_limits(client):
    spec = client.get("/openapi.json").json()["paths"]
    for path, method in (("/api/enrich/{inn}", "post"), ("/api/counterparty/{inn}", "get")):
        assert {"401", "429"} <= set(spec[path][method]["responses"])
