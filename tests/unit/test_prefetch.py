"""Prefetch: top-N order, idempotent enqueue, resume after a crash, captcha stop. No network."""

from pathlib import Path

import duckdb
import pytest

from src.match.store import ParquetStore, clear_stores
from src.sources.pb_nalog import CaptchaRequired
from src.worker import prefetch
from src.worker.queue import engine_from_env, status_counts

GOOD = ["7811756312", "7811726371", "7804428656", "7736050003", "7707049388"]  # valid INNs


@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/q.sqlite")
    return engine_from_env()


@pytest.fixture
def suppliers(tmp_path):
    """An ingest directory with suppliers.parquet only: the top is read through the store (#293)."""
    p = tmp_path / "suppliers.parquet"
    duckdb.sql(
        "COPY (SELECT * FROM (VALUES "
        "('7811756312', true, 10, 5, DATE '2025-01-01'),"
        "('7811726371', true, 20, 5, DATE '2025-01-01'),"  # same wins, more participations
        "('7804428656', true, 1, 9, DATE '2025-01-01'),"  # most wins
        "('7736050003', true, 20, 5, DATE '2025-06-01'),"  # ties 7811726371, more recent
        "('1234567890', false, 99, 99, DATE '2025-06-01')"  # invalid INN is skipped
        ") t(inn, inn_valid, lots_participated, lots_won, last_seen)) "
        f"TO '{p.as_posix()}' (FORMAT parquet)"
    )
    return ParquetStore(tmp_path)


def test_top_order(suppliers):
    assert prefetch.top_inns(suppliers, 3) == ["7804428656", "7736050003", "7811726371"]
    assert prefetch.top_inns(suppliers, 10)[-1] == "7811756312"


def test_idempotent_and_resume(eng):
    calls: list[str] = []

    def crashy(inn):
        calls.append(inn)
        if inn == GOOD[2] and calls.count(inn) == 1:
            raise RuntimeError("boom")

    r1 = prefetch.run(GOOD[:4], eng, pause_s=0, enrich_fn=crashy)
    assert r1["errors_this_run"] == 1
    # the failed task was retried inside r1 already; the restart repeats no finished task
    r2 = prefetch.run(GOOD[:4], eng, pause_s=0, enrich_fn=crashy)
    assert calls.count(GOOD[2]) == 2 and all(calls.count(i) == 1 for i in GOOD[:4] if i != GOOD[2])
    assert r2["done_total"] == 4 and status_counts(eng) == {"done": 4}
    r3 = prefetch.run(GOOD[:4], eng, pause_s=0, enrich_fn=crashy)
    assert r3["done_this_run"] == 0 and len(calls) == 5


def test_captcha_stops_and_keeps_task(eng):
    seen: list[str] = []

    def fn(inn):
        seen.append(inn)
        if inn == GOOD[1]:
            raise CaptchaRequired("captcha")

    r = prefetch.run(GOOD[:4], eng, pause_s=0, enrich_fn=fn)
    assert r["stopped"] and seen == GOOD[:2] and r["done_total"] == 1
    assert status_counts(eng) == {"done": 1, "pending": 3}


def test_foreign_pending_task_untouched(eng):
    from sqlalchemy import select

    from src.worker.queue import enqueue, tasks
    enqueue(eng, "enrich", "7707083893")  # foreign task, smaller id than ours
    r = prefetch.run(GOOD[:3], eng, pause_s=0, enrich_fn=lambda inn: None)
    assert r["done_total"] == 3 and r["done_this_run"] == 3
    with eng.connect() as c:
        row = c.execute(select(tasks.c.status, tasks.c.attempts).where(tasks.c.key == "7707083893")).one()
    assert (row.status, row.attempts) == ("pending", 0)


def test_run_after_stop_processes_the_rest(eng):
    state = {"stop": True}

    def fn(inn):
        if inn == GOOD[1] and state["stop"]:
            raise CaptchaRequired("captcha")

    assert prefetch.run(GOOD[:4], eng, pause_s=0, enrich_fn=fn)["stopped"]
    state["stop"] = False
    r = prefetch.run(GOOD[:4], eng, pause_s=0, enrich_fn=fn)
    assert r["done_total"] == 4 and r["done_this_run"] == 3 and not r["stopped"]


def test_exit_code_nonzero_when_incomplete(eng, suppliers, monkeypatch):
    monkeypatch.setattr(prefetch, "enrich", lambda inn: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setenv("DATA_STORE", "parquet")  # the store of the environment, as the worker reads it
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(suppliers.processed))
    clear_stores()
    try:
        assert prefetch.main(["--top", "2", "--pause", "0"]) == 1
    finally:
        clear_stores()


def test_enqueue_only_fills_queue_idempotently(eng):
    assert prefetch.enqueue_only(GOOD[:3], eng) == {"pending": 3}
    assert prefetch.enqueue_only(GOOD[:4], eng) == {"pending": 4}  # no duplicates


def test_main_enqueue_only_fills_the_queue_from_the_store_and_enriches_nothing(eng, suppliers, monkeypatch):
    monkeypatch.setattr(prefetch, "enrich", lambda inn: (_ for _ in ()).throw(AssertionError("no enrich")))
    monkeypatch.setenv("DATA_STORE", "parquet")  # the store of the environment, as the worker reads it
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(suppliers.processed))
    clear_stores()
    try:
        assert prefetch.main(["--top", "2", "--enqueue-only"]) == 0
    finally:
        clear_stores()
    assert status_counts(eng) == {"pending": 2}


def test_worker_night_mode_sleeps_on_captcha_and_continues(eng, monkeypatch):
    from src.sources.pb_nalog import CaptchaRequired
    from src.worker import worker as W
    from src.worker.queue import MAX_ATTEMPTS, enqueue

    enqueue(eng, "enrich", GOOD[0])
    calls: list[str] = []

    def flaky(inn):
        calls.append(inn)
        if len(calls) <= MAX_ATTEMPTS:  # more captchas than attempts: a captcha must not spend one (release)
            raise CaptchaRequired("captcha")

    sleeps: list[float] = []
    monkeypatch.setattr(W, "enrich", flaky)
    monkeypatch.setattr(W.time, "sleep", sleeps.append)
    assert W.run(once=True, pause_s=0, captcha_sleep_s=900) == 1
    assert calls == [GOOD[0]] * (MAX_ATTEMPTS + 1) and sleeps.count(900) == MAX_ATTEMPTS
    assert status_counts(eng) == {"done": 1}


def test_worker_survives_db_errors_and_pauses_only_after_live_fetch(eng, monkeypatch, tmp_path):
    from sqlalchemy.exc import OperationalError

    from src.sources.cache import SourceCache
    from src.worker import worker as W
    from src.worker.queue import enqueue

    for inn in GOOD[:2]:
        enqueue(eng, "enrich", inn)
    real_claim = W.claim
    state = {"n": 0}

    class Drained(Exception):
        pass

    def flaky_claim(e, kind):
        state["n"] += 1
        if state["n"] == 1:
            raise OperationalError("select", {}, Exception("could not translate host name db"))
        task = real_claim(e, kind)
        if task is None:
            raise Drained  # the long run never ends by itself: stop it once the queue is drained
        return task

    cache = SourceCache(tmp_path / "c.sqlite")

    def enrich_fn(inn):
        if inn == GOOD[0]:
            cache.put("https://x/" + inn, "live")  # a live answer was written to the cache
        # GOOD[1]: served from cache, nothing written

    sleeps: list[float] = []
    monkeypatch.setattr(W, "claim", flaky_claim)
    monkeypatch.setattr(W, "enrich", enrich_fn)
    monkeypatch.setattr(W.time, "sleep", sleeps.append)
    with pytest.raises(Drained):
        W.run(pause_s=1.5, db_retry_s=7)
    assert 7 in sleeps  # waited for the database instead of crashing
    assert sleeps.count(1.5) == 1  # pause only after the live fetch, not after the cache hit
    assert status_counts(eng) == {"done": 2}


def test_worker_once_fails_instead_of_waiting_for_the_database(eng, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from src.worker import worker as W
    from src.worker.queue import enqueue

    down = OperationalError("connect", {}, Exception("could not translate host name db"))
    monkeypatch.setattr(W.time, "sleep", lambda s: pytest.fail("--once must not wait for the database"))
    monkeypatch.setattr(W, "engine_from_env", lambda: (_ for _ in ()).throw(down))
    with pytest.raises(OperationalError):
        W.run(once=True)
    monkeypatch.setattr(W, "engine_from_env", lambda: eng)  # connected, then the database goes away mid-run
    enqueue(eng, "enrich", GOOD[0])
    monkeypatch.setattr(W, "claim", lambda e, kind: (_ for _ in ()).throw(down))
    with pytest.raises(OperationalError):
        W.run(once=True)
    with pytest.raises(SystemExit) as ex:
        W.main(["--once"])
    assert ex.value.code == 1


def test_enqueue_only_revives_a_failed_task(eng):
    from src.worker.queue import MAX_ATTEMPTS, claim, enqueue, finish

    enqueue(eng, "enrich", GOOD[0])
    for _ in range(MAX_ATTEMPTS):
        task = claim(eng, "enrich")
        assert task is not None
        finish(eng, task[0], error="boom")
    assert status_counts(eng) == {"failed": 1}
    assert prefetch.enqueue_only(GOOD[:1], eng) == {"pending": 1}  # the night queue retries it with fresh attempts


def test_night_mode_is_an_overlay_not_the_default_of_make_up():
    import yaml

    root = Path(__file__).resolve().parents[2]
    base = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    night = yaml.safe_load((root / "docker-compose.night.yml").read_text(encoding="utf-8"))["services"]
    assert "--captcha-sleep" not in base["worker"]["command"] and "restart" not in base["worker"]
    assert "restart" not in base["db"]
    assert night["worker"]["command"][-2:] == ["--captcha-sleep", "900"]
    assert night["worker"]["restart"] == night["db"]["restart"] == "unless-stopped"
    assert not (root / "docker-compose.override.yml").exists()  # compose would load an override on every `make up`
