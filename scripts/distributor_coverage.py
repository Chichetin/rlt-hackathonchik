"""Distributors in the block «Новые» (#108): how many `kind=distributor` per query, before and after.

The same command measures both rules on the same cache and the same organizers' data:
  --rule v0 — the table WHOLESALE and MAX_REQUESTS of D-036 (frozen copy below, «было»);
  --rule v1 — the current src/enrich/discover.py («стало»).
Queries: the real cases `eval/cases/real.jsonl` (with their OKPD2) + the e2e demo lot «Бумага офисная А4».

Default — cache only (no network). `--warm --max-requests N` fetches the missing registry pages live
(paced by src/sources/rmsp.py, through SourceCache, stop on captcha/403/429), refusing when more than N
pages are missing. Result: JSON with commit, rule, per-query counts «в пуле» (all candidates after the
filters) and «в выдаче» (top-10 of the block).

  uv run python scripts/distributor_coverage.py --rule v0 --out eval/results/K3-distributors-v0.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.contracts import Query  # noqa: E402
from src.enrich import discover as D  # noqa: E402
from src.enrich.evidence import check_counterparty  # noqa: E402
from src.sources import rmsp  # noqa: E402
from src.sources.cache import SourceCache  # noqa: E402

# D-036 as merged in #16 (before #108): one wholesale code per row, 4 requests per query.
V0_WHOLESALE: dict[str, tuple[str, ...]] = {
    "10": ("46.3",), "11": ("46.34",), "13": ("46.41",), "14": ("46.42",), "15": ("46.42",),
    "16": ("46.73",), "23": ("46.73",), "17": ("46.49.33",), "19.20": ("46.71",),
    "20.41": ("46.44.2",), "20.42": ("46.45",), "21": ("46.46.1",),
    "22.19.60": ("46.46.2",), "32.50": ("46.46.2",), "26.20": ("46.51",), "26.30": ("46.52",),
    "27.40": ("46.47.3",), "28.23": ("46.66",), "28": ("46.69",), "31.01": ("46.65",), "31": ("46.47.1",),
    "32.30": ("46.49.43",), "32.40": ("46.49.42",),
}
V0_MAX_REQUESTS = 4
DEMO = {"query_id": "demo-e2e", "text": "Бумага офисная А4", "okpd2": ["17.12.14.110"]}  # src/ui/e2e/demo.spec.ts


def use_rule(rule: str) -> None:
    if rule == "v0":
        D.WHOLESALE = V0_WHOLESALE  # type: ignore[misc]
        D.MAX_REQUESTS = V0_MAX_REQUESTS  # type: ignore[misc]


def queries(cases: Path) -> list[Query]:
    out = [Query(**DEMO)]
    for line in cases.read_text(encoding="utf-8").splitlines():
        q = json.loads(line)["query"]
        out.append(Query(query_id=q["query_id"], text=q["text"], okpd2=q.get("okpd2") or []))
    return out


def planned_urls(q: Query) -> list[str]:
    jobs = [(ch, region, np) for region in D._regions(q) for ch in D.okved_plan(q.okpd2)
            for np in D._np_types()][:D.MAX_REQUESTS]
    return [rmsp.search_url(ch.okved, region, np) for ch, region, np in jobs]


def measure(q: Query, cache: SourceCache, processed: Path) -> dict:
    res = D.discover(q, cache=cache, processed=processed, offline=True)
    pool, shown = Counter(m.counterparty.kind for m in res.pool), Counter(m.counterparty.kind for m in res.matches)
    bad = sum(bool(check_counterparty(m.counterparty, cache.page_text)) for m in res.pool)
    return {
        "query_id": q.query_id, "text": q.text[:80], "okpd2": q.okpd2,
        "okved_plan": [ch.okved for ch in D.okved_plan(q.okpd2)],
        "history_checked": res.history_checked,
        "pool": len(res.pool), "pool_distributors": pool.get("distributor", 0),
        "shown": len(res.matches), "shown_distributors": shown.get("distributor", 0),
        "pool_by_kind": {str(k): v for k, v in sorted(pool.items(), key=lambda kv: str(kv[0]))},
        "evidence_failed": bad, "requests": res.requests,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rule", choices=("v0", "v1"), required=True)
    ap.add_argument("--cases", type=Path, default=ROOT / "eval" / "cases" / "real.jsonl")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--warm", action="store_true", help="fetch missing registry pages live first")
    ap.add_argument("--max-requests", type=int, default=0, help="refuse to warm when more pages are missing")
    ap.add_argument("--skip-warm", nargs="*", default=[],
                    help="query_ids not warmed (a source that times out on them pauses the rest)")
    args = ap.parse_args(argv)
    use_rule(args.rule)
    cache, processed = SourceCache(), D.processed_dir()
    qs = queries(args.cases)

    warm = [q for q in qs if q.query_id not in args.skip_warm]
    missing = [u for q in warm for u in planned_urls(q) if cache.get(u, allow_stale=True) is None]
    missing = list(dict.fromkeys(missing))
    if args.warm and missing:
        if len(missing) > args.max_requests:
            print(f"нет в кеше {len(missing)} страниц > --max-requests {args.max_requests}: не прогреваю")
            return 2
        for q in warm:  # the same calls as /api/search, with a long budget
            r = D.discover(q, cache=cache, processed=processed, offline=False, budget_s=120.0)
            for line in r.requests + r.notes:
                print(f"  {q.query_id}: {line}")
    missing = [u for q in qs for u in planned_urls(q) if cache.get(u, allow_stale=True) is None]
    missing = list(dict.fromkeys(missing))

    rows = [measure(q, cache, processed) for q in qs]
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                            cwd=ROOT).stdout.strip()
    total = {k: sum(r[k] for r in rows) for k in ("pool", "pool_distributors", "shown", "shown_distributors",
                                                  "evidence_failed")}
    total["queries_with_shown_distributors"] = sum(r["shown_distributors"] > 0 for r in rows)
    result = {"exp": f"K3-distributors-{args.rule}", "rule": args.rule, "commit": commit,
              "at": datetime.now(UTC).isoformat(timespec="seconds"), "cache_only": True,
              "pages_not_in_cache": missing, "max_requests": D.MAX_REQUESTS, "queries": len(rows),
              "total": total, "per_query": rows}
    for r in rows:
        print(f"{r['query_id']:<12} {','.join(r['okpd2']):<14} пул {r['pool']:>3} (дистр. {r['pool_distributors']:>3})"
              f"  выдача {r['shown']:>2} (дистр. {r['shown_distributors']:>2})  план {' '.join(r['okved_plan'])}")
    print(f"ИТОГО {args.rule}: {json.dumps(total, ensure_ascii=False)}; страниц нет в кеше: {len(missing)}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
