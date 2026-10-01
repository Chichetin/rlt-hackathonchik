"""Prefetch: top-N order, idempotent enqueue, resume after a crash, captcha stop. No network."""

import duckdb
import pytest

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
    p = tmp_path / "s.parquet"
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
    return p


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
    # restart: only the task that failed once is redone, finished ones are not
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
    assert prefetch.main(["--top", "2", "--pause", "0", "--suppliers", str(suppliers)]) == 1
