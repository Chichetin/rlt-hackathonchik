"""FNS status coverage of the top-10 (#147): how many shown candidates have a checked legal status.

    uv run python -m src.eval.fns_coverage --exp C-fns-v1            # cache only, no network
    uv run python -m src.eval.fns_coverage --exp C-fns-v1 --enqueue  # + queue the unchecked INNs (worker, paced)

Liveness is the FNS card status from the cache only (D-059): no card -> status None, the candidate is kept and
shown as «не проверено» (src/match/filters.py). Queries: the real cases `eval/cases/real.jsonl` + the e2e demo
lot. Each query runs the product selection (src/match/pipeline.py, offline: no live registry calls); per block
(«С опытом» = history, «Новые» = discovered) the top-10 is counted: checked (status known), active (status
«действующ…»), unchecked. Writes eval/results/<exp>.json.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime

from src.contracts import CounterpartyMatch, Query
from src.eval.run import RESULTS, git
from src.eval.split import ROOT

DEMO = Query(query_id="demo-e2e", text="Бумага офисная А4", okpd2=["17.12.14.110"])  # src/ui/e2e/demo.spec.ts
BLOCKS = {"history": "С опытом", "discovered": "Новые"}


def queries() -> list[Query]:
    out = [DEMO]
    for line in (ROOT / "eval/cases/real.jsonl").read_text(encoding="utf-8").splitlines():
        q = Query(**json.loads(line)["query"])
        out.append(q.model_copy(update={"as_of": None}))  # the product view today, as the demo shows it
    return out


def count(rows: list[CounterpartyMatch]) -> dict:
    """Checked / active / unchecked among the rows; unchecked INNs listed (to enqueue)."""
    checked = [m for m in rows if m.counterparty.status]
    active = [m for m in checked if "действ" in (m.counterparty.status or "").lower()]
    unchecked = [m.counterparty.inn for m in rows if not m.counterparty.status]
    return {"n": len(rows), "checked": len(checked), "active": len(active), "unchecked": len(unchecked),
            "unchecked_inn": [i for i in unchecked if i]}


def cache_info() -> dict:
    """Which source cache was measured: the coverage is a property of the cache, not of the code."""
    from src.sources.cache import SourceCache

    cache = SourceCache()
    pages = cache.path.exists() and sqlite3.connect(cache.path).execute("select count(*) from pages").fetchone()[0]
    return {"path": cache.path.as_posix(), "pages": pages or 0}


def share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def measure(top: int = 10) -> dict:
    from src.match import pipeline

    per_query: list[dict] = []
    totals = {b: {"n": 0, "checked": 0, "active": 0, "unchecked": 0} for b in BLOCKS}
    for q in queries():
        res = pipeline.pick(q, offline=True)
        row: dict = {"query_id": q.query_id}
        for block in BLOCKS:
            c = count([m for m in res.matches if m.origin == block][:top])
            row[block] = c
            for k in totals[block]:
                totals[block][k] += c[k]
        per_query.append(row)
    summary = {b: {**t, "checked_share": share(t["checked"], t["n"]), "active_share": share(t["active"], t["n"])}
               for b, t in totals.items()}
    n = sum(t["n"] for t in totals.values())
    checked = sum(t["checked"] for t in totals.values())
    summary["all"] = {"n": n, "checked": checked, "checked_share": share(checked, n)}
    return {"top": top, "n_queries": len(per_query), "summary": summary, "per_query": per_query}


def enqueue_unchecked(body: dict) -> int:
    """Queue enrichment of the unchecked top-10 INNs; the worker fetches them with its pauses (src/worker)."""
    from src.worker.queue import engine_from_env, enqueue

    engine = engine_from_env()
    inns = {i for row in body["per_query"] for b in BLOCKS for i in row[b]["unchecked_inn"]}
    for inn in sorted(inns):
        enqueue(engine, "enrich", inn, retry_failed=False)
    return len(inns)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="покрытие статуса ФНС в топ-10 (#147)")
    ap.add_argument("--exp", required=True, help="id результата, например C-fns-v1")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--enqueue", action="store_true", help="поставить непроверенные ИНН в очередь обогащения")
    args = ap.parse_args(argv)
    body = measure(args.top)
    rec = {"exp_id": args.exp, "kind": "fns_coverage", "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
           "commit": git("rev-parse", "HEAD"), "cache": cache_info(), **body}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for b, name in BLOCKS.items():
        s = body["summary"][b]
        print(f"{name}: проверено {s['checked']}/{s['n']} ({s['checked_share']}), действующих {s['active']}")
    if args.enqueue:
        print(f"в очередь обогащения: {enqueue_unchecked(body)} ИНН")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
