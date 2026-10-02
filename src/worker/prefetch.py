"""`python -m src.worker.prefetch --top 200`: warm the source cache for the most active suppliers.

Selection rule (deterministic): valid INN, ordered by lots_won DESC, lots_participated DESC,
last_seen DESC, inn ASC (wins first: a winner is the likelier invitee; recency and INN break ties).
The INNs go into the task queue (idempotent by (kind, key), D-008), the loop drains them with pauses
between live source calls. A restart does not repeat finished INNs: `done` tasks stay done and the
cache serves the rest. A pb captcha is never bypassed: enrich() falls back to rmsp/bo (D-052); the run
stops, returning the task to the queue, only when no source answered (D-056).
The suppliers come from the store search() reads (#293): Postgres in `make up`, ingest parquet in `make demo`.

`--rnp` (D-152): РНП ЕИС for a shortlist (`--inns-file`, one INN per line, or the top-N). enrich() reads the РНП
from the cache only — a live РНП request costs the 60 s Crawl-delay of zakupki.gov.ru, at most 1 INN/min — so this
pass is the only place the worker side asks it live. It does not look at the enrich queue: an INN whose card task
is already `done` is checked too.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.contracts import inn_is_valid
from src.enrich.enrich import enrich
from src.match.store import Store, get_store
from src.sources import eis_rnp
from src.sources.cache import SourceCache
from src.sources.pb_nalog import CaptchaRequired
from src.worker.queue import claim_keys, engine_from_env, enqueue, finish, release, requeue_stale, tasks

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


def rnp_pass(inns: list[str], cache: SourceCache | None = None,
             check_fn: Callable[..., eis_rnp.RnpResult] | None = None,
             sleep: Callable[[float], None] = time.sleep, max_down: int = 3,
             progress_every: int = 10) -> dict[str, object]:
    """Ask РНП ЕИС live for every INN of the shortlist (D-152), paced by eis_rnp (Crawl-delay 60 s).

    Idempotent through the cache: an answer younger than eis_rnp.MAX_AGE_S is read, not asked again, so a rerun
    goes on where the last one stopped. A refusal (`Blocked`: 403/429, a page that is not the РНП search) stops the
    pass — the host is paused for 15 min. A network error, a timeout or a 5xx waits the short pause
    (eis_rnp.DOWN_COOLDOWN_S) and goes on with the next INN; `max_down` of them in a row stop the pass."""
    cache = cache or SourceCache()
    check_fn = check_fn or eis_rnp.check
    todo = [i for i in dict.fromkeys(inns) if inn_is_valid(i)]
    c: Counter[str] = Counter()
    stopped = None
    down = 0
    t0 = time.monotonic()
    log.info("РНП: %d ИНН в шорт-листе", len(todo))
    for n, inn in enumerate(todo, start=1):
        hit = cache.get(eis_rnp.search_url(inn), allow_stale=True)
        fresh = hit is not None and time.time() - hit.fetched_at <= eis_rnp.MAX_AGE_S
        try:
            res = check_fn(inn, cache, offline=False)
        except eis_rnp.Blocked as e:
            stopped = f"{inn}: {e}"
            log.error("РНП: отказ на %s — проход остановлен, повторный запуск продолжит: %s", inn, e)
            break
        except httpx.HTTPError as e:
            c["errors"] += 1
            down += 1
            log.warning("РНП: %s не проверен (%s)", inn, type(e).__name__)
            if down >= max_down:
                stopped = f"{inn}: {down} сбоя подряд ({type(e).__name__})"
                log.error("РНП: %d сбоя подряд — проход остановлен", down)
                break
            sleep(eis_rnp.DOWN_COOLDOWN_S + 1)
            continue
        down = 0
        c["from_cache" if fresh else "live"] += 1
        c[{True: "in_rnp", False: "not_in_rnp", None: "undetermined"}[res.in_rnp]] += 1
        if n % progress_every == 0:
            log.info("РНП: %d/%d (%s)", n, len(todo), dict(c))
    res_all: dict[str, object] = {"total": len(todo), **{k: c[k] for k in (
        "live", "from_cache", "in_rnp", "not_in_rnp", "undetermined", "errors")},
        "elapsed_s": round(time.monotonic() - t0, 1), "stopped": stopped}
    log.info("РНП: готово %s", res_all)
    return res_all


def read_inns(path: Path) -> list[str]:
    """One INN per line; blank lines and `#` comments are skipped."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.split("#", 1)[0].strip() for ln in lines if ln.split("#", 1)[0].strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Warm the source cache for the top-N active suppliers")
    ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between live source calls")
    ap.add_argument("--rnp", action="store_true",
                    help="РНП ЕИС for the shortlist instead of the cards (1 INN/min, D-152)")
    ap.add_argument("--inns-file", type=Path, default=None, help="the shortlist: one INN per line (default: top-N)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # the store of the environment: DATA_STORE, DATABASE_URL, DATA_PROCESSED_DIR (src/match/store.py)
    inns = read_inns(args.inns_file) if args.inns_file else top_inns(get_store(), args.top)
    if args.rnp:
        out = rnp_pass(inns)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out["stopped"] is None and not out["errors"] else 1
    res = run(inns, engine_from_env(), pause_s=args.pause)
    return 0 if res["done_total"] == res["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
