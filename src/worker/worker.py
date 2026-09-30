"""`python -m src.worker.worker`: drains the enrichment queue (docker-compose service `worker`).

Resumable: state lives in the task table, a crash leaves `running` tasks that are retried
after restart (`--recover`). Pauses between tasks protect external sources (section 6).
"""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import text

from src.enrich.enrich import enrich
from src.worker.queue import claim, engine_from_env, finish

log = logging.getLogger("worker")


def run(once: bool = False, pause_s: float = 1.0) -> int:
    eng = engine_from_env()
    with eng.begin() as c:  # tasks left running by a crashed worker go back to the queue
        c.execute(text("UPDATE tasks SET status='pending' WHERE status='running'"))
    done = 0
    while True:
        task = claim(eng, "enrich")
        if task is None:
            if once:
                return done
            time.sleep(pause_s)
            continue
        task_id, inn = task
        try:
            enrich(inn)
            finish(eng, task_id)
            done += 1
        except Exception as e:  # noqa: BLE001 — a task failure must not kill the worker
            log.warning("task %s (%s) failed: %s", task_id, inn, e)
            finish(eng, task_id, error=str(e)[:500])
        time.sleep(pause_s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="drain the queue and exit")
    ap.add_argument("--pause", type=float, default=1.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    n = run(once=args.once, pause_s=args.pause)
    log.info("processed %d tasks", n)


if __name__ == "__main__":
    main()
