"""`python -m src.worker.worker`: drains the enrichment queue (docker-compose service `worker`).

Resumable: state lives in the task table; tasks left `running` by a crashed worker longer than
QUEUE_STALE_RUNNING_S go back to the queue (not all `running` — other workers may hold them).
Pauses between tasks protect external sources (section 6).
"""

from __future__ import annotations

import argparse
import logging
import time

from src.enrich.enrich import enrich
from src.sources.pb_nalog import CaptchaRequired
from src.worker.queue import claim, engine_from_env, finish, release, requeue_stale, status_counts

log = logging.getLogger("worker")


CACHE_HIT_S = 0.05  # a task faster than this was served by the cache: no source was hit, no pause
PROGRESS_EVERY = 25


def run(once: bool = False, pause_s: float = 1.0, captcha_sleep_s: float | None = None) -> int:
    """Drain the queue. `captcha_sleep_s`: None = stop on captcha (default); a number = night mode: the task
    goes back, the worker sleeps that long (sources' own 15-min cooldown) and continues, so a container
    with `restart` does not restart into the same block and hit the host again."""
    eng = engine_from_env()
    requeue_stale(eng)
    done, errors, last_requeue, t0 = 0, 0, time.monotonic(), time.monotonic()
    while True:
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
        started = time.monotonic()
        try:
            enrich(inn)
            finish(eng, task_id)
            done += 1
        except CaptchaRequired:
            # research/SOURCES.md: captcha → never solve it, never keep hitting the host
            release(eng, task_id)
            if captcha_sleep_s is None:
                log.error("captcha on %s — worker stops, task %s back in the queue", inn, task_id)
                return done
            log.error("captcha on %s and no fallback answered — sleeping %.0f s, task %s back in the queue",
                      inn, captcha_sleep_s, task_id)
            time.sleep(captcha_sleep_s)
            continue
        except Exception as e:  # noqa: BLE001 — a task failure must not kill the worker
            errors += 1
            log.warning("task %s (%s) failed: %s", task_id, inn, e)
            finish(eng, task_id, error=str(e)[:500])
        n = done + errors
        if n % PROGRESS_EVERY == 0:
            log.info("progress: this run %d (errors %d, %.1f INN/min), queue %s", n, errors,
                     n / max((time.monotonic() - t0) / 60, 1e-9), status_counts(eng))
        if time.monotonic() - started >= CACHE_HIT_S:
            time.sleep(pause_s)


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
