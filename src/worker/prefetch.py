"""`python -m src.worker.prefetch --top 200`: warm the source cache for the most active suppliers.

Selection rule (deterministic): valid INN, ordered by lots_won DESC, lots_participated DESC,
last_seen DESC, inn ASC (wins first: a winner is the likelier invitee; recency and INN break ties).
The INNs go into the task queue (idempotent by (kind, key), D-008), the loop drains them with pauses
between live source calls. A restart does not repeat finished INNs: `done` tasks stay done and the
cache serves the rest. A captcha stops the run, the task returns to the queue (never bypassed).
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

import duckdb
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.enrich.enrich import enrich
from src.sources.pb_nalog import CaptchaRequired
from src.worker.queue import claim, engine_from_env, enqueue, finish, release, requeue_stale, tasks

log = logging.getLogger("prefetch")
CACHE_HIT_S = 0.05  # a task faster than this was served by the cache: no source was hit, no pause


def top_inns(suppliers: Path, n: int) -> list[str]:
    rows = duckdb.connect().execute(
        "SELECT inn FROM read_parquet(?) WHERE inn_valid "
        "ORDER BY lots_won DESC, lots_participated DESC, last_seen DESC, inn ASC LIMIT ?",
        [str(suppliers), n],
    ).fetchall()
    return [r[0] for r in rows]


def _statuses(eng: Engine, inns: list[str]) -> dict[str, str]:
    with eng.connect() as c:
        rows = c.execute(select(tasks.c.key, tasks.c.status).where(tasks.c.kind == "enrich")).all()
    want = set(inns)
    return {k: s for k, s in rows if k in want}


def run(inns: list[str], eng: Engine, pause_s: float = 1.0,
        enrich_fn: Callable[[str], object] = enrich, progress_every: int = 10) -> dict[str, object]:
    """Enqueue `inns`, drain them. Returns counters; `stopped` is set when a source stopped the run."""
    requeue_stale(eng)
    for inn in inns:
        enqueue(eng, "enrich", inn)
    want = set(inns)
    total = len(inns)
    already = sum(1 for s in _statuses(eng, inns).values() if s == "done")
    done = errors = 0
    stopped = None
    t0 = time.monotonic()
    log.info("prefetch: %d INN, %d already done, %d to do", total, already, total - already)
    while True:
        task = claim(eng, "enrich")
        if task is None:
            break
        task_id, inn = task
        if inn not in want:  # someone else's task in a shared queue: leave it alone
            release(eng, task_id)
            break
        started = time.monotonic()
        try:
            enrich_fn(inn)
            finish(eng, task_id)
            done += 1
        except CaptchaRequired:
            release(eng, task_id)
            stopped = f"captcha on {inn}"
            log.error("captcha on %s: stop, task back in the queue", inn)
            break
        except Exception as e:  # noqa: BLE001 - one INN must not kill the batch
            errors += 1
            log.warning("enrich %s failed: %s", inn, e)
            finish(eng, task_id, error=str(e)[:500])
        n = done + errors
        if n % progress_every == 0:
            mins = max((time.monotonic() - t0) / 60, 1e-9)
            log.info("progress: %d/%d (this run %d, errors %d, %.1f INN/min)",
                     already + done, total, n, errors, n / mins)
        if time.monotonic() - started >= CACHE_HIT_S:
            time.sleep(pause_s)
    elapsed = time.monotonic() - t0
    after = _statuses(eng, inns)
    res: dict[str, object] = {
        "total": total, "done_total": sum(1 for s in after.values() if s == "done"),
        "done_this_run": done, "errors_this_run": errors,
        "failed_total": sum(1 for s in after.values() if s == "failed"),
        "elapsed_s": round(elapsed, 1), "inn_per_min": round((done + errors) / max(elapsed / 60, 1e-9), 1),
        "stopped": stopped,
    }
    log.info("prefetch finished: %s", res)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Warm the source cache for the top-N active suppliers")
    ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between live source calls")
    ap.add_argument("--suppliers", type=Path, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = args.suppliers or Path(os.environ.get("DATA_PROCESSED_DIR", "data/processed")) / "suppliers.parquet"
    res = run(top_inns(path, args.top), engine_from_env(), pause_s=args.pause)
    return 1 if res["stopped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
