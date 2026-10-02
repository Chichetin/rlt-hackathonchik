"""Adds winner@k / winner_mrr (#280) to saved eval results from their stored `ranked` lists.

    uv run python -m src.eval.winner_backfill E-003 B-bm25 B-inc

The ranking is not re-run: commit, config and `ranked` of the result stay as recorded, only the
winner metrics (derived from `ranked` and the case grades) and the summaries over them are added.
Only parts `dev` and `real` (their cases are rebuilt from eval/cases); a result without `ranked`
lists, or whose query ids differ from the part's cases, is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.eval import run as R
from src.eval.metrics import WINNER_METRICS
from src.eval.split import load_cases, split_config


def add_winner_metrics(rec: dict) -> dict:
    """The record with winner metrics in per_query, metrics and slices (a copy; the input is not changed)."""
    if rec["part"] not in ("dev", "real"):
        raise ValueError(f"{rec['exp_id']}: часть {rec['part']!r} — досчёт только для dev и real")
    rows = rec["per_query"]
    if any("ranked" not in q for q in rows):
        raise ValueError(f"{rec['exp_id']}: нет `ranked` в per_query — нечего досчитывать")
    cases = {c.query.query_id: c for c in load_cases(rec["part"], split_config())}
    if {q["query_id"] for q in rows} != set(cases):
        raise ValueError(f"{rec['exp_id']}: кейсы результата не совпадают с кейсами части {rec['part']}")
    out = []
    for q in rows:
        winners = {inn for inn, g in cases[q["query_id"]].grades.items() if g >= 2}
        out.append({**q, **{n: fn(q["ranked"], winners) if winners else None for n, fn in WINNER_METRICS.items()}})
    return {**rec, "metrics": R._summary(out), "slices": R.slices(out), "per_query": out}


def main(argv: list[str] | None = None, results: Path = R.RESULTS) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+", help="experiment ids in eval/results")
    args = ap.parse_args(argv)
    code = 0
    for exp in args.ids:
        path = results / f"{exp}.json"
        try:
            rec = add_winner_metrics(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"{exp}: пропущен — {e}", file=sys.stderr)
            code = 1
            continue
        path.write_text(R.dump_record(rec), encoding="utf-8")
        m = rec["metrics"].get("winner@10")
        print(f"{exp}: winner@10 = {m['value']:.3f} [{m['ci95'][0]:.3f}, {m['ci95'][1]:.3f}] (n={m['n']})" if m
              else f"{exp}: в кейсах нет победителей")
    return code


if __name__ == "__main__":
    sys.exit(main())
