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

from src.eval.metrics import METRICS, WINNER_METRICS, bootstrap_ci
from src.eval.run import CANDIDATE_METRICS, RESULTS, slice_names


def require_same_cases(a: dict, b: dict) -> None:
    """ValueError unless both runs cover the same query ids: the pairing of `paired` and of src.eval.parity."""
    ids_a = [q["query_id"] for q in a["per_query"]]
    ids_b = {q["query_id"] for q in b["per_query"]}
    if set(ids_a) != ids_b or len(ids_a) != len(ids_b):
        raise ValueError(f"{a['exp_id']} и {b['exp_id']}: разные наборы кейсов — парное сравнение невозможно")


def paired(a: dict, b: dict, alpha: float = 0.05) -> dict:
    """Paired per-query differences and percentile intervals (delta = a - b).

    The default remains 95%; E6's predeclared 90% test uses alpha=0.1.
    """
    if not 0 < alpha < 1:
        raise ValueError("alpha должен быть между 0 и 1")
    ci_key = f"ci{100 * (1 - alpha):g}"
    require_same_cases(a, b)
    rows_b = {q["query_id"]: q for q in b["per_query"]}
    groups: dict[str, list[tuple[dict, dict]]] = {"all": []}
    for q in a["per_query"]:
        qb = rows_b[q["query_id"]]
        # slice fields of either run: a result from before the type slice (#104) is compared with a newer one
        for name in ("all", *slice_names({**qb, **q})):
            groups.setdefault(name, []).append((q, qb))
    out = {}
    for name, pairs in sorted(groups.items(), key=lambda kv: (kv[0] != "all", kv[0])):
        metrics = {}
        for m in (*METRICS, *WINNER_METRICS, *CANDIDATE_METRICS):
            # winner metrics (#280): only cases with a winner in both runs; a result from before #280 has none
            used = [(x, y) for x, y in pairs if x.get(m) is not None and y.get(m) is not None]
            if not used:
                continue
            diffs = [x[m] - y[m] for x, y in used]
            lo, hi = bootstrap_ci(diffs, alpha=alpha)
            metrics[m] = {"delta": sum(diffs) / len(diffs), ci_key: [lo, hi], "n": len(used),
                          "a": sum(x[m] for x, _ in used) / len(used), "b": sum(y[m] for _, y in used) / len(used)}
        out[name] = {"n_queries": len(pairs), "metrics": metrics}
    return out


def main(argv: list[str] | None = None, results: Path = RESULTS) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a", help="experiment id (minuend)")
    ap.add_argument("b", help="experiment id (subtrahend), e.g. the best baseline")
    ap.add_argument("--alpha", type=float, default=0.05, help="interval error rate: .05 = 95%%, .1 = 90%%")
    args = ap.parse_args(argv)
    a, b = (json.loads((results / f"{x}.json").read_text(encoding="utf-8")) for x in (args.a, args.b))
    try:
        table = paired(a, b, alpha=args.alpha)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"Δ = {args.a} − {args.b} | part={a['part']} | парный bootstrap по запросам")
    confidence = f"{100 * (1 - args.alpha):g}"
    ci_key = f"ci{confidence}"
    for group in (list(METRICS), list(WINNER_METRICS), list(CANDIDATE_METRICS)):
        names = [n for n in group if n in table["all"]["metrics"]]
        if not names:
            continue
        print(f"{'slice':<32} {'n':>6}  " + "  ".join(f"{n + f' b → a, Δ [{confidence}% CI]':<42}" for n in names))
        for name, s in table.items():
            if names[0] not in s["metrics"]:
                continue
            cells = (f"{s['metrics'][m]['b']:.3f} → {s['metrics'][m]['a']:.3f}, {s['metrics'][m]['delta']:+.3f} "
                     f"[{s['metrics'][m][ci_key][0]:+.3f}, {s['metrics'][m][ci_key][1]:+.3f}]" for m in names)
            print(f"{name:<32} {s['metrics'][names[0]].get('n', s['n_queries']):>6}  "
                  + "  ".join(f"{c:<42}" for c in cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
