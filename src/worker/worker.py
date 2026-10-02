"""`python -m src.worker.worker`: drains the enrichment queue (docker-compose service `worker`).

Resumable: state lives in the task table; tasks left `running` by a crashed worker longer than
QUEUE_STALE_RUNNING_S go back to the queue (not all `running` — other workers may hold them).
Pauses between tasks protect external sources (section 6).
"""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy.exc import OperationalError

from src.enrich.enrich import enrich
from src.sources.cache import SourceCache
from src.sources.pb_nalog import CaptchaRequired
from src.worker.queue import claim, engine_from_env, finish, release, requeue_stale, status_counts

log = logging.getLogger("worker")


PROGRESS_EVERY = 10
DB_RETRY_S = 10.0  # the database is down (container restart, network): wait and try again, never exit
_live_writes = 0  # pages written to the source cache = answers fetched from a source (not served from cache)


def _count_live_writes() -> None:
    """A task that wrote nothing to the cache was served from it: no source was hit, no pause needed.
    Explicit counter instead of a timing guess (a cache read on a slow volume, /mnt/c, is not «fast»)."""
    if getattr(SourceCache.put, "_counts_live", False):
        return
    orig = SourceCache.put

    def put(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        global _live_writes
        _live_writes += 1
        return orig(self, *args, **kwargs)

    put._counts_live = True  # type: ignore[attr-defined]
    SourceCache.put = put  # type: ignore[method-assign]


def _connect(retry_s: float):  # type: ignore[no-untyped-def]
    while True:
        try:
            eng = engine_from_env()
            requeue_stale(eng)
            return eng
        except OperationalError as e:
            log.error("database unavailable (%s) — retry in %.0f s", str(e).splitlines()[0][:160], retry_s)
            time.sleep(retry_s)


def run(once: bool = False, pause_s: float = 1.0, captcha_sleep_s: float | None = None,
        db_retry_s: float = DB_RETRY_S) -> int:
    """Drain the queue. `captcha_sleep_s`: None = stop on captcha (default); a number = night mode: the task
    goes back, the worker sleeps that long (sources' own 15-min cooldown) and continues. The cooldown
    lives in the process: a container restart forgets it (D-142), so the sleep is the only protection.
    РНП ЕИС (after #111) is not covered: its refusal does not fail the task (the card goes without `in_rnp`),
    eis_rnp keeps its own Crawl-delay and 15-min pause after a refusal (D-141).
    Database errors never end the run: wait `db_retry_s` and continue (the `running` task of a lost
    connection returns to the queue by requeue_stale)."""
    _count_live_writes()
    eng = _connect(db_retry_s)
    done, errors, last_requeue, t0 = 0, 0, time.monotonic(), time.monotonic()
    while True:
        try:
            if time.monotonic() - last_requeue > 60:  # a sibling worker may have crashed meanwhile
                requeue_stale(eng)
                last_requeue = time.monotonic()
            task = claim(eng, "enrich")
            if task is None:
                if once:
                    return done
                time.sleep(pause_s)
                continue
            task_id, inn = task
            writes_before = _live_writes
            try:
                enrich(inn)
                finish(eng, task_id)
                done += 1
            except CaptchaRequired:
                # captcha: not solved here (D-085 allows it, nothing implements it) — never keep hitting the host
                release(eng, task_id)
                if captcha_sleep_s is None:
                    log.error("captcha on %s — worker stops, task %s back in the queue", inn, task_id)
                    return done
                log.error("captcha on %s and no fallback answered — sleeping %.0f s, task %s back in the queue",
                          inn, captcha_sleep_s, task_id)
                time.sleep(captcha_sleep_s)
                continue
            except OperationalError:
                raise
            except Exception as e:  # noqa: BLE001 — a task failure must not kill the worker
                errors += 1
                log.warning("task %s (%s) failed: %s", task_id, inn, e)
                finish(eng, task_id, error=str(e)[:500])
            n = done + errors
            if n % PROGRESS_EVERY == 0:
                log.info("progress: this run %d (errors %d, %.1f INN/min incl. cache hits), queue %s", n, errors,
                         n / max((time.monotonic() - t0) / 60, 1e-9), status_counts(eng))
            if _live_writes > writes_before:
                time.sleep(pause_s)
        except OperationalError as e:
            log.error("database unavailable (%s) — retry in %.0f s", str(e).splitlines()[0][:160], db_retry_s)
            time.sleep(db_retry_s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="drain the queue and exit")
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--captcha-sleep", type=float, default=None,
                    help="night mode: seconds to sleep after a captcha with no fallback instead of exiting")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    n = run(once=args.once, pause_s=args.pause, captcha_sleep_s=args.captcha_sleep)
    log.info("processed %d tasks", n)


if __name__ == "__main__":
    main()
