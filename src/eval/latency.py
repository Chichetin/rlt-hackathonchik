"""Latency of `POST /api/search` for the slide of item 8 (Q-22): p50 / p95 on dev queries, warmed service.

    DATA_STORE=postgres uv run python -m src.eval.latency --exp latency-pg-v1      # make latency EXP=…
    DATA_STORE=parquet  uv run python -m src.eval.latency --exp latency-parquet-v1

The texts, OKPD2 and customers of the dev cases are sent the way the product sends them (as_of=None:
all data, first page of 20) through the FastAPI app in-process — server time without the network.
The first WARMUP queries (index load, model, connections) are not counted. Writes
eval/results/<exp>.json: store, retriever, commit, machine, p50/p95/mean/max and every query's time.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
from datetime import UTC, datetime

from src.eval.run import RESULTS, git, tree_dirty
from src.eval.split import load_cases, split_config

WARMUP = 5


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100) — no interpolation: the value is one of the measured times."""
    if not values:
        raise ValueError("нет замеров")
    s = sorted(values)
    return s[max(0, math.ceil(q / 100 * len(s)) - 1)]


def measure(n: int, part: str = "dev", limit: int = 20) -> dict:
    from fastapi.testclient import TestClient

    from src.api.app import app
    from src.match import search as search_mod
    from src.match.store import get_store

    cases = load_cases(part, split_config())[: n + WARMUP]
    times: list[dict] = []
    with TestClient(app) as client:  # runs the lifespan: pg_hybrid warms its model there (Q-24)
        for i, case in enumerate(cases):
            body = case.query.model_copy(update={"as_of": None}).model_dump(mode="json")
            t0 = time.perf_counter()
            r = client.post(f"/api/search?limit={limit}", json=body)
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code != 200:
                raise RuntimeError(f"{case.query.query_id}: HTTP {r.status_code} {r.text[:200]}")
            if i >= WARMUP:
                times.append({"query_id": case.query.query_id, "ms": round(ms, 2),
                              "total": int(r.headers["X-Total-History"])})
        store = get_store().name
        retriever = search_mod.load_config()["retriever"]
    ms = [t["ms"] for t in times]
    return {
        "store": store,
        "retriever": retriever,
        "n_queries": len(ms),
        "warmup": WARMUP,
        "limit": limit,
        "latency_ms": {"p50": percentile(ms, 50), "p95": percentile(ms, 95), "mean": round(statistics.fmean(ms), 2),
                       "max": max(ms)},
        "per_query": times,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="p50/p95 POST /api/search (Q-22)")
    ap.add_argument("--exp", required=True, help="id результата, например latency-pg-v1")
    ap.add_argument("--n", type=int, default=200, help="запросов в замере (после прогрева)")
    ap.add_argument("--part", default="dev", choices=("dev", "dev_full", "real"))
    args = ap.parse_args(argv)
    body = measure(args.n, args.part)
    rec = {
        "exp_id": args.exp,
        "kind": "latency",
        "part": args.part,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": tree_dirty(),
        "machine": {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()},
        **body,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lat = rec["latency_ms"]
    print(f"{args.exp} | store={rec['store']} retriever={rec['retriever']} | n={rec['n_queries']} | "
          f"p50 {lat['p50']:.0f} мс, p95 {lat['p95']:.0f} мс, max {lat['max']:.0f} мс → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
