"""Paired difference of two experiments over the same cases, per slice, with a bootstrap interval.

    uv run python -m src.eval.compare E-002 B-inc      # delta = E-002 minus B-inc

Reads eval/results/<id>.json (per_query); both runs must cover the same query ids. The interval is
a percentile bootstrap of the per-query differences (same seed as `make eval`): an interval that
does not contain 0 is a difference, anything else is noise (research/EVAL.md, "Шум").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.eval.metrics import METRICS, bootstrap_ci
from src.eval.run import RESULTS, slice_names


def paired(a: dict, b: dict) -> dict:
    """{slice: {n_queries, metrics: {name: {delta, ci95}}}} for records a and b (delta = a - b)."""
    rows_b = {q["query_id"]: q for q in b["per_query"]}
    ids_a = [q["query_id"] for q in a["per_query"]]
    if set(ids_a) != set(rows_b) or len(ids_a) != len(rows_b):
        raise ValueError(f"{a['exp_id']} и {b['exp_id']}: разные наборы кейсов — парное сравнение невозможно")
    groups: dict[str, list[tuple[dict, dict]]] = {"all": []}
    for q in a["per_query"]:
        qb = rows_b[q["query_id"]]
        # slice fields of either run: a result from before the type slice (#104) is compared with a newer one
        for name in ("all", *slice_names({**qb, **q})):
            groups.setdefault(name, []).append((q, qb))
    out = {}
    for name, pairs in sorted(groups.items(), key=lambda kv: (kv[0] != "all", kv[0])):
        metrics = {}
        for m in METRICS:
            diffs = [x[m] - y[m] for x, y in pairs]
            lo, hi = bootstrap_ci(diffs)
            metrics[m] = {"delta": sum(diffs) / len(diffs), "ci95": [lo, hi],
                          "a": sum(x[m] for x, _ in pairs) / len(pairs), "b": sum(y[m] for _, y in pairs) / len(pairs)}
        out[name] = {"n_queries": len(pairs), "metrics": metrics}
    return out


def main(argv: list[str] | None = None, results: Path = RESULTS) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a", help="experiment id (minuend)")
    ap.add_argument("b", help="experiment id (subtrahend), e.g. the best baseline")
    args = ap.parse_args(argv)
    a, b = (json.loads((results / f"{x}.json").read_text(encoding="utf-8")) for x in (args.a, args.b))
    try:
        table = paired(a, b)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"Δ = {args.a} − {args.b} | part={a['part']} | парный bootstrap по запросам")
    print(f"{'slice':<32} {'n':>6}  " + "  ".join(f"{n + ' b → a, Δ [95% CI]':<42}" for n in METRICS))
    for name, s in table.items():
        cells = (f"{m['b']:.3f} → {m['a']:.3f}, {m['delta']:+.3f} [{m['ci95'][0]:+.3f}, {m['ci95'][1]:+.3f}]"
                 for m in s["metrics"].values())
        print(f"{name:<32} {s['n_queries']:>6}  " + "  ".join(f"{c:<42}" for c in cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
