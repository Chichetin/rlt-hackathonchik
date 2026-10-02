"""Enrichment coverage of the top-10 (#147, #396): status, name, role with basis, fact with its fetch date per shown candidate.

    uv run python -m src.eval.fns_coverage --exp C-fns-v1            # cache only, no network
    uv run python -m src.eval.fns_coverage --exp C-fns-v1 --enqueue  # + queue the unchecked INNs (worker, paced)
    SOURCE_CACHE=<copy>.sqlite ENRICH_OFFLINE=1 DISCOVERY_OFFLINE=1 \\
        uv run python -m src.eval.fns_coverage --exp C-fns-v2 --lots 5903085,5875992,6050858  # reproducible offline

Coverage is a property of the source cache: measure a frozen copy (SOURCE_CACHE), without --warm-top.
`--warm-top N` first enriches the unchecked INNs of the top-N over the network (paced) and so CHANGES the cache.

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
from collections import Counter
from datetime import UTC, datetime

from src.contracts import CounterpartyMatch, Query
from src.eval.run import RESULTS, git
from src.eval.split import ROOT

DEMO = Query(query_id="demo-e2e", text="Бумага офисная А4", okpd2=["17.12.14.110"])  # src/ui/e2e/demo.spec.ts
BLOCKS = {"history": "С опытом", "discovered": "Новые"}


def queries(lots: list[str] | None = None) -> list[Query]:
    out = [DEMO]
    from src.api.lots import find_lot  # notices of organizers' data by number (the demo lots, #396)

    for ident in lots or []:
        out.append(find_lot(ident).model_copy(update={"as_of": None}))
    for line in (ROOT / "eval/cases/real.jsonl").read_text(encoding="utf-8").splitlines():
        q = Query(**json.loads(line)["query"])
        out.append(q.model_copy(update={"as_of": None}))  # the product view today, as the demo shows it
    return out


def _has_name(m: CounterpartyMatch) -> bool:
    """The organizers' data has no names: the loader puts «ИНН <inn>» until enrich() finds the real one."""
    return not m.counterparty.name.startswith("ИНН ")


def _has_role_basis(m: CounterpartyMatch) -> bool:
    """A role (kind) AND its basis: the `okved` Evidence that passed strip_unverified (D-057)."""
    c = m.counterparty
    return c.kind in ("manufacturer", "distributor", "dealer") and any(e.field == "okved" for e in c.evidence)


def _has_dated_fact(m: CounterpartyMatch) -> bool:
    """At least one fact from a web source (not `data:`) with the date it was fetched."""
    return any(e.fetched_at is not None and not e.url.startswith("data:") for e in m.counterparty.evidence)


def _name_source(m: CounterpartyMatch) -> str | None:
    """Source of the `name` fact (Evidence.source), None when the name is the «ИНН …» placeholder."""
    return next((e.source for e in m.counterparty.evidence if e.field == "name"), None)


def count(rows: list[CounterpartyMatch]) -> dict:
    """Checked / active / unchecked among the rows (+ named, role with basis, dated fact); unchecked INNs listed."""
    checked = [m for m in rows if m.counterparty.status]
    active = [m for m in checked if "действ" in (m.counterparty.status or "").lower()]
    unchecked = [m.counterparty.inn for m in rows if not m.counterparty.status]
    return {"n": len(rows), "checked": len(checked), "active": len(active), "unchecked": len(unchecked),
            "named": sum(_has_name(m) for m in rows), "role_basis": sum(_has_role_basis(m) for m in rows),
            "dated": sum(_has_dated_fact(m) for m in rows),
            "roles": dict(Counter(m.counterparty.kind or "none" for m in rows)),
            "name_sources": dict(Counter(_name_source(m) or "none" for m in rows)),
            "unchecked_inn": [i for i in unchecked if i]}


def cache_info() -> dict:
    """Which source cache was measured: the coverage is a property of the cache, not of the code."""
    from src.sources.cache import SourceCache

    cache = SourceCache()
    pages = cache.path.exists() and sqlite3.connect(cache.path).execute("select count(*) from pages").fetchone()[0]
    return {"path": cache.path.as_posix(), "pages": pages or 0}


def share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


FIELDS = ("checked", "active", "named", "role_basis", "dated")


def measure(top: int = 10, lots: list[str] | None = None) -> dict:
    from src.match import pipeline

    per_query: list[dict] = []
    totals: dict = {b: {"n": 0, "unchecked": 0, **{k: 0 for k in FIELDS}} for b in BLOCKS}
    for q in queries(lots):
        res = pipeline.pick(q, offline=True)
        row: dict = {"query_id": q.query_id}
        for block in BLOCKS:
            c = count([m for m in res.matches if m.origin == block][:top])
            row[block] = c
            for k in totals[block]:
                totals[block][k] += c[k]
        per_query.append(row)
    for b in BLOCKS:
        for key in ("roles", "name_sources"):
            c: Counter = Counter()
            for row in per_query:
                c.update(row[b][key])
            totals[b][key] = dict(c)
    summary: dict = {b: {**t, **{f"{k}_share": share(t[k], t["n"]) for k in FIELDS}} for b, t in totals.items()}
    n = sum(t["n"] for t in totals.values())
    total: dict = {"n": n}
    for k in FIELDS:
        total[k] = sum(t[k] for t in totals.values())
        total[f"{k}_share"] = share(total[k], n)
    summary["all"] = total
    return {"top": top, "n_queries": len(per_query), "summary": summary, "per_query": per_query}


def enqueue_unchecked(body: dict) -> int:
    """Queue enrichment of the unchecked top-10 INNs; the worker fetches them with its pauses (src/worker)."""
    from src.worker.queue import engine_from_env, enqueue

    engine = engine_from_env()
    inns = {i for row in body["per_query"] for b in BLOCKS for i in row[b]["unchecked_inn"]}
    for inn in sorted(inns):
        enqueue(engine, "enrich", inn, retry_failed=False)
    return len(inns)


def warm(body: dict, pause_s: float) -> dict:
    """Enrich the unchecked INNs of `body` (paced, idempotent: src.worker.prefetch.run); returns its counters."""
    from src.worker.prefetch import run
    from src.worker.queue import engine_from_env

    inns = sorted({i for row in body["per_query"] for b in BLOCKS for i in row[b]["unchecked_inn"]})
    return {"inns": len(inns), **run(inns, engine_from_env(), pause_s=pause_s)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="покрытие обогащения в топ-10 (#147, #396): статус, название, роль с основанием, дата факта; "
                                 "для воспроизведения — офлайн по копии кеша (SOURCE_CACHE), без --warm-top")
    ap.add_argument("--exp", required=True, help="id результата, например C-fns-v1")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--enqueue", action="store_true", help="поставить непроверенные ИНН в очередь обогащения")
    ap.add_argument("--lots", default="", help="номера закупок через запятую (демо-лоты): ещё запросы к 8 реальным")
    ap.add_argument("--warm-top", type=int, default=0, help="сначала обогатить непроверенные из топ-N по сети (с паузами): МЕНЯЕТ кеш; 0 — не обогащать")
    ap.add_argument("--pause", type=float, default=1.0, help="секунд между живыми вызовами источников при --warm-top")
    args = ap.parse_args(argv)
    lots = [x.strip() for x in args.lots.split(",") if x.strip()]
    warmed = warm(measure(args.warm_top, lots), args.pause) if args.warm_top else None
    body = measure(args.top, lots)
    if warmed:
        body["warm"] = {"top": args.warm_top, **warmed}
    rec = {"exp_id": args.exp, "kind": "fns_coverage", "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
           "commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")), "cache": cache_info(), **body}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for b, name in BLOCKS.items():
        s = body["summary"][b]
        print(f"{name}: статус {s['checked']}/{s['n']} ({s['checked_share']}), действующих {s['active']}, "
              f"название {s['named']}, роль с основанием {s['role_basis']}, дата получения факта {s['dated']}, "
              f"роли {s['roles']}, источники названий {s['name_sources']}")
    if args.enqueue:
        print(f"в очередь обогащения: {enqueue_unchecked(body)} ИНН")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
