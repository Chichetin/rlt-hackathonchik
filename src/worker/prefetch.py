"""`python -m src.worker.prefetch --top 200`: warm the source cache for the most active suppliers.

Selection rule (deterministic): valid INN, ordered by lots_won DESC, lots_participated DESC,
last_seen DESC, inn ASC (wins first: a winner is the likelier invitee; recency and INN break ties).
The INNs go into the task queue (idempotent by (kind, key), D-008), the loop drains them with pauses
between live source calls. A restart does not repeat finished INNs: `done` tasks stay done and the
cache serves the rest. A pb captcha is never bypassed: enrich() falls back to rmsp/bo (D-052); the run
stops, returning the task to the queue, only when no source answered (D-056).
The suppliers come from the store search() reads (#293): Postgres in `make up`, ingest parquet in `make demo`.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.enrich.enrich import enrich
from src.match.store import Store, get_store
from src.sources.pb_nalog import CaptchaRequired
from src.worker.queue import claim_keys, engine_from_env, enqueue, finish, release, requeue_stale, status_counts, tasks

log = logging.getLogger("prefetch")
CACHE_HIT_S = 0.05  # a task faster than this was served by the cache: no source was hit, no pause


def top_inns(store: Store, n: int) -> list[str]:
    return store.top_suppliers(n)


def _statuses(eng: Engine, inns: list[str]) -> dict[str, str]:
    with eng.connect() as c:
        rows = c.execute(select(tasks.c.key, tasks.c.status).where(tasks.c.kind == "enrich")).all()
    want = set(inns)
    return {k: s for k, s in rows if k in want}


def run(inns: list[str], eng: Engine, pause_s: float = 1.0,
        enrich_fn: Callable[[str], object] | None = None, progress_every: int = 10) -> dict[str, object]:
    """Enqueue `inns`, drain them. Returns counters; `stopped` is set when a source stopped the run."""
    enrich_fn = enrich_fn or enrich  # resolved at call time (tests patch `enrich`)
    requeue_stale(eng)
    for inn in inns:
        enqueue(eng, "enrich", inn)
    total = len(inns)
    already = sum(1 for s in _statuses(eng, inns).values() if s == "done")
    done = errors = 0
    stopped = None
    t0 = time.monotonic()
    log.info("prefetch: %d INN, %d already done, %d to do", total, already, total - already)
    while True:
        task = claim_keys(eng, "enrich", inns)  # only our keys: a foreign pending task stays untouched
        if task is None:
            break
        task_id, inn = task
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
        except KeyboardInterrupt:  # Ctrl+C: do not leave the task `running` for QUEUE_STALE_RUNNING_S
            release(eng, task_id)
            log.warning("interrupted on %s, task back in the queue", inn)
            raise
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
    if res["done_total"] != total:
        log.warning("prefetch incomplete: %s of %d done (failed %s, stopped: %s)",
                    res["done_total"], total, res["failed_total"], stopped)
    return res


def enqueue_only(inns: list[str], eng: Engine) -> dict[str, int]:
    """Night mode: only fill the queue (priority = enqueue order = activity rank); the compose `worker`
    drains it. Idempotent: done tasks stay done, a failed one goes back to pending."""
    for inn in inns:
        enqueue(eng, "enrich", inn)
    return status_counts(eng)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Warm the source cache for the top-N active suppliers")
    ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between live source calls")
    ap.add_argument("--enqueue-only", action="store_true", help="fill the queue and exit; `worker` drains it")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # the store of the environment: DATA_STORE, DATABASE_URL, DATA_PROCESSED_DIR (src/match/store.py)
    inns = top_inns(get_store(), args.top)
    if args.enqueue_only:
        print(enqueue_only(inns, engine_from_env()))
        return 0
    res = run(inns, engine_from_env(), pause_s=args.pause)
    return 0 if res["done_total"] == res["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
