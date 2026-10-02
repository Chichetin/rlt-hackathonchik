"""Read-only benchmark of lot_hits against its pre-#522 implementation; no eval or training.

    uv run python -m scripts.bench_lot_hits --index indexes/bm25-0591312044e44272

Loads an existing index only. Selects evenly spaced rows of dev.jsonl, retrieves once, checks exact
array/dtype parity, then times only warm document-to-lot aggregation in alternating order. Memory
is a separate tracemalloc run (Python/NumPy allocations, not process RSS); index and retrieval are
excluded from both measurements. Output is JSON, suitable for redirecting to an evidence file.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import statistics
import time
import tracemalloc
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

import numpy as np

from src.match.index import LotIndex, _load
from src.match.store import LotHits
from src.match.text import tokenize, tokenizer_version

ROOT = Path(__file__).resolve().parents[1]
Aggregation = Callable[[LotIndex, list[tuple[int, float]]], LotHits]


def reference_old(idx: LotIndex, hits: list[tuple[int, float]]) -> LotHits:
    """Verbatim aggregation from main 75bd77e (the only change is self -> idx)."""
    lots_by_doc, off, _ = idx._maps()
    parts = [(lots_by_doc[off[d]:off[d + 1]], s, d, r) for r, (d, s) in enumerate(hits)]
    if not parts:
        return LotHits(np.empty(0, np.int64), np.empty(0), np.empty(0, np.int32))
    lot = np.concatenate([p[0] for p in parts]).astype(np.int64)
    score = np.concatenate([np.full(len(p[0]), p[1]) for p in parts])
    doc = np.concatenate([np.full(len(p[0]), p[2], np.int32) for p in parts])
    rank = np.concatenate([np.full(len(p[0]), p[3]) for p in parts])
    order = np.lexsort((rank, lot))
    pick = order[np.unique(lot[order], return_index=True)[1]]
    return LotHits(lot[pick], score[pick], doc[pick])


def peak_bytes(fn: Aggregation, idx: LotIndex, hits: list[tuple[int, float]]) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        out = fn(idx, hits)
        _, peak = tracemalloc.get_traced_memory()
        del out
        return peak
    finally:
        tracemalloc.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path, help="Existing saved BM25 index; never builds one")
    parser.add_argument("--cases", type=Path, default=ROOT / "eval/cases/dev.jsonl")
    parser.add_argument("--queries", type=int, default=24)
    parser.add_argument("--repeats", type=int, default=25)
    parser.add_argument("--top", type=int, default=500)
    args = parser.parse_args()
    if min(args.queries, args.repeats, args.top) < 1:
        parser.error("queries, repeats and top must be positive")
    meta = json.loads((args.index / "meta.json").read_text(encoding="utf-8"))
    if meta["tokenizer"] != tokenizer_version():
        parser.error("saved index tokenizer differs from the configured tokenizer")
    cases_raw = args.cases.read_bytes()
    cases = [json.loads(line)["query"] for line in cases_raw.splitlines() if line.strip()]
    if not cases:
        parser.error("cases file is empty")
    rows = np.linspace(0, len(cases) - 1, min(args.queries, len(cases)), dtype=int)
    queries = [cases[i] for i in rows]
    if any(q.get("as_of") != meta["as_of"] for q in queries):
        parser.error("case cutoff differs from the saved index cutoff")
    idx = _load(args.index.name.removeprefix("bm25-"), args.index)
    _, off, _ = idx._maps()  # map I/O excluded from timing and allocation peaks
    methods = [reference_old, LotIndex.lot_hits]
    records = []
    for q in queries:
        hits = idx.retrieve(tokenize(q["text"]), args.top)
        old, new = (method(idx, hits) for method in methods)
        for field in ("lot_id", "score", "doc_id"):
            np.testing.assert_array_equal(getattr(old, field), getattr(new, field), strict=True)
        timings: list[list[float]] = [[], []]
        for repeat in range(args.repeats):
            for m in ((0, 1) if repeat % 2 == 0 else (1, 0)):
                started = time.perf_counter_ns()
                methods[m](idx, hits)
                timings[m].append((time.perf_counter_ns() - started) / 1_000_000)
        records.append({
            "query_id": q["query_id"], "hits": len(hits), "lots": len(new),
            "expanded_pairs": int(sum(off[d + 1] - off[d] for d, _ in hits)),
            "old_ms": statistics.median(timings[0]), "new_ms": statistics.median(timings[1]),
            "old_peak_bytes": peak_bytes(methods[0], idx, hits),
            "new_peak_bytes": peak_bytes(methods[1], idx, hits),
        })
    report = {
        "stage": "warm LotIndex.lot_hits only; excludes index load, BM25 retrieval and supplier search",
        "reference_commit": "75bd77e038a5962cec1a879ec15f0e7a6ac41338",
        "python": platform.python_version(), "numpy": np.__version__, "bm25s": version("bm25s"),
        "index": str(args.index.resolve()), "index_meta": meta,
        "docs_sha256": hashlib.sha256((args.index / "docs.parquet").read_bytes()).hexdigest(),
        "implementation_sha256": hashlib.sha256((ROOT / "src/match/index.py").read_bytes()).hexdigest(),
        "cases": str(args.cases.resolve()), "cases_sha256": hashlib.sha256(cases_raw).hexdigest(),
        "selection": "evenly spaced rows including endpoints; no shuffle",
        "queries": len(records), "top": args.top, "repeats_per_method": args.repeats,
        "all_arrays_and_dtypes_equal": True,
        "median_old_ms": statistics.median(r["old_ms"] for r in records),
        "median_new_ms": statistics.median(r["new_ms"] for r in records),
        "median_paired_speedup": statistics.median(r["old_ms"] / r["new_ms"] for r in records),
        "memory_scope": "peak traced Python/NumPy allocations per call, measured separately; not RSS",
        "median_old_peak_bytes": statistics.median(r["old_peak_bytes"] for r in records),
        "median_new_peak_bytes": statistics.median(r["new_peak_bytes"] for r in records),
        "records": records,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
