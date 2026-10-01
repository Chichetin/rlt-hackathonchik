"""Enrichment queue: a task table + the `worker` process, no Redis/Celery (CLAUDE.md, «Стек»).

DATABASE_URL: postgresql+psycopg://… in docker-compose (service `db`), otherwise a local SQLite
file (cache/queue.sqlite). Claiming a task is one UPDATE … RETURNING, safe for several workers.
Tasks are idempotent by (kind, key): enqueueing the same INN twice keeps one pending task.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
tasks = Table(
    "tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(32), nullable=False),  # enrich
    Column("key", String(64), nullable=False),  # INN
    Column("status", String(16), nullable=False, default="pending"),  # pending/running/done/failed
    Column("attempts", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("kind", "key", name="uq_kind_key"),
)

MAX_ATTEMPTS = 3
STALE_RUNNING_S = int(os.environ.get("QUEUE_STALE_RUNNING_S", "600"))  # running longer = crashed worker


def engine_from_env() -> Engine:
    url = os.environ.get("DATABASE_URL") or "sqlite:///cache/queue.sqlite"
    if url.startswith("sqlite:///"):
        os.makedirs(os.path.dirname(url.removeprefix("sqlite:///")) or ".", exist_ok=True)
    eng = create_engine(url, future=True)
    metadata.create_all(eng)
    return eng


def _now() -> datetime:
    return datetime.now(UTC)


class QueueFull(Exception):
    """The pending-task ceiling is reached and the INN is not in the queue yet (F-10, #256)."""


def enqueue(eng: Engine, kind: str, key: str, *, retry_failed: bool = True, max_pending: int | None = None) -> int:
    """Idempotent by (kind, key). `retry_failed=True` (explicit request: POST /api/enrich) turns a failed
    task back to pending with fresh attempts; False (background calls from the search, #38) leaves it
    failed, so an unresolvable INN is not sent to the registries again on every search.
    `max_pending` (explicit requests only; batch tasks pass None): a task that would become a NEW pending one
    while that many are already pending raises QueueFull; an INN that is already queued or done changes nothing."""
    try:
        return _enqueue(eng, kind, key, retry_failed, max_pending)
    except IntegrityError:  # a concurrent enqueue of the same (kind, key) won the insert
        return _enqueue(eng, kind, key, retry_failed, max_pending)


def _check_room(c, max_pending: int | None) -> None:  # type: ignore[no-untyped-def]
    if max_pending is None:
        return
    pending = c.execute(select(func.count()).select_from(tasks).where(tasks.c.status == "pending")).scalar_one()
    if pending >= max_pending:
        raise QueueFull(f"в очереди обогащения {pending} задач (потолок {max_pending})")


def _enqueue(eng: Engine, kind: str, key: str, retry_failed: bool, max_pending: int | None = None) -> int:
    with eng.begin() as c:
        row = c.execute(select(tasks.c.id, tasks.c.status).where(tasks.c.kind == kind, tasks.c.key == key)).first()
        if row is not None:
            if row.status == "failed" and retry_failed:
                _check_room(c, max_pending)
                c.execute(tasks.update().where(tasks.c.id == row.id)
                          .values(status="pending", attempts=0, error=None, updated_at=_now()))
            return row.id
        _check_room(c, max_pending)
        res = c.execute(tasks.insert().values(kind=kind, key=key, status="pending", attempts=0,
                                              created_at=_now(), updated_at=_now()))
        return int(res.inserted_primary_key[0])  # type: ignore[index]


def claim(eng: Engine, kind: str) -> tuple[int, str] | None:
    # Postgres: SKIP LOCKED — concurrent workers take different rows instead of one losing the race
    # and seeing an "empty" queue. SQLite serializes writers, a plain subquery is enough.
    lock = " FOR UPDATE SKIP LOCKED" if eng.dialect.name == "postgresql" else ""
    with eng.begin() as c:
        row = c.execute(
            text(
                "UPDATE tasks SET status='running', attempts=attempts+1, updated_at=:now "
                "WHERE id = (SELECT id FROM tasks WHERE kind=:kind AND status='pending' ORDER BY id LIMIT 1"
                f"{lock}) AND status='pending' RETURNING id, key"
            ),
            {"kind": kind, "now": _now()},
        ).first()
    return (row.id, row.key) if row else None


def requeue_stale(eng: Engine, older_than_s: int = STALE_RUNNING_S) -> int:
    """Tasks `running` longer than the timeout belong to a crashed worker: back to the queue."""
    cutoff = datetime.fromtimestamp(_now().timestamp() - older_than_s, UTC)
    with eng.begin() as c:
        res = c.execute(tasks.update().where(tasks.c.status == "running", tasks.c.updated_at < cutoff)
                        .values(status="pending", updated_at=_now()))
    return res.rowcount or 0


def finish(eng: Engine, task_id: int, error: str | None = None) -> None:
    with eng.begin() as c:
        attempts = c.execute(select(tasks.c.attempts).where(tasks.c.id == task_id)).scalar_one()
        status = "done" if error is None else ("failed" if attempts >= MAX_ATTEMPTS else "pending")
        c.execute(tasks.update().where(tasks.c.id == task_id).values(status=status, error=error, updated_at=_now()))


def release(eng: Engine, task_id: int) -> None:
    """Back to pending without spending an attempt: the source stopped us, the task did not fail."""
    with eng.begin() as c:
        c.execute(tasks.update().where(tasks.c.id == task_id, tasks.c.status == "running")
                  .values(status="pending", attempts=tasks.c.attempts - 1, updated_at=_now()))


def status_counts(eng: Engine) -> dict[str, int]:
    with eng.connect() as c:
        rows = c.execute(text("SELECT status, COUNT(*) FROM tasks GROUP BY status")).all()
    return {r[0]: int(r[1]) for r in rows}


def claim_keys(eng: Engine, kind: str, keys: list[str]) -> tuple[int, str] | None:
    """Like `claim`, but only for the given keys: a batch job must not take someone else's task."""
    if not keys:
        return None
    lock = " FOR UPDATE SKIP LOCKED" if eng.dialect.name == "postgresql" else ""
    names = {f"k{i}": k for i, k in enumerate(keys)}
    marks = ", ".join(f":{n}" for n in names)
    with eng.begin() as c:
        row = c.execute(
            text(
                "UPDATE tasks SET status='running', attempts=attempts+1, updated_at=:now "
                "WHERE id = (SELECT id FROM tasks WHERE kind=:kind AND status='pending' "
                f"AND key IN ({marks}) ORDER BY id LIMIT 1{lock}) AND status='pending' RETURNING id, key"
            ),
            {"kind": kind, "now": _now(), **names},
        ).first()
    return (row.id, row.key) if row else None
