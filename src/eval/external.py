"""`python -m src.eval.external --exp <id> --lots <csv>`: our method on another team's lot list (#342).

Their protocol (art7235/RLT_hack, app/eval/procedure_eval.py): lots of 2025 with a known winner, history
before 2025-01-01, «a winner in the top-10». Here the same lots and the same history go through our pipeline
(src/eval/run.py evaluate), so winner@10 is comparable with theirs. Lots from the sealed period
(holdout_from, configs/split.yaml) are dropped before any case is built: only their ids and dates are looked at.
Result: eval/results/<exp>.json, the same record as `make eval`.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from src.eval import build_cases as BC
from src.eval import run as R
from src.eval.split import ROOT, EvalCase, split_config
from src.match.store import processed_dir

DEFAULT_LOTS = ROOT / "eval" / "cases" / "art7235_lots.csv"
DEFAULT_CUTOFF = date(2025, 1, 1)


def select_cases(lots: list[BC.Lot], ids: list[int], cutoff: date,
                 holdout_from: date) -> tuple[list[EvalCase], dict[str, int]]:
    """Cases for the listed lot ids, in the order of the list; stats say why a listed lot was dropped."""
    by_id = {lot.lot_id: lot for lot in lots}
    stats: dict[str, int] = dict.fromkeys(
        ("listed", "not_in_data", "before_cutoff", "holdout_period", "no_winner", "no_text"), 0)
    stats["listed"] = len(ids)
    cases: list[EvalCase] = []
    for lot_id in ids:
        lot = by_id.get(lot_id)
        if lot is None:
            stats["not_in_data"] += 1
        elif lot.date < cutoff:
            stats["before_cutoff"] += 1
        elif lot.date >= holdout_from:
            stats["holdout_period"] += 1
        elif not any(g >= 2 for g in BC._relevant(lot).values()):
            stats["no_winner"] += 1
        elif not BC.query_text(lot.subject, lot.procedure_name, [n for n, _ in lot.items]):
            stats["no_text"] += 1
        else:
            cases.append(BC.to_case(lot, cutoff))
    return cases, stats


def read_ids(path: Path, n: int | None) -> list[int]:
    with path.open(encoding="utf-8", newline="") as f:
        ids = [int(row["lot_id"]) for row in csv.DictReader(f)]
    return ids[:n] if n else ids


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exp", required=True, help="experiment id (research/EXPERIMENTS.md)")
    ap.add_argument("--lots", type=Path, default=DEFAULT_LOTS, help="csv with a lot_id column")
    ap.add_argument("--n", type=int, default=1000, help="first N listed lots (their README: 1 000)")
    ap.add_argument("--cutoff", type=date.fromisoformat, default=DEFAULT_CUTOFF, help="history strictly before")
    ap.add_argument("--baseline", default=None, help="entry of configs/baselines.yaml (B-pop, B-inc, B-bm25)")
    ap.add_argument("--config-of", default=None,
                    help="entry of configs/experiments.yaml under this id (v2: no ranker, trained on 2025-04…06)")
    args = ap.parse_args(argv)
    cfg = R.load_method_config(args.exp, args.baseline, args.config_of)
    holdout_from = date.fromisoformat(str(split_config()["holdout_from"]))
    lots = BC.read_lots(processed_dir(), args.cutoff)
    cases, stats = select_cases(lots, read_ids(args.lots, args.n), args.cutoff, holdout_from)
    print("отбор:", stats, f"-> {len(cases)} кейсов", flush=True)
    rec = R.result_record(args.exp, f"ext-{args.lots.stem}-n{args.n}", cfg, R.evaluate(cases, cfg, args.cutoff))
    rec["selection"] = {**stats, "cases": len(cases), "cutoff": args.cutoff.isoformat(), "lots_file": args.lots.name}
    R.RESULTS.mkdir(parents=True, exist_ok=True)
    out = R.RESULTS / f"{args.exp}.json"
    out.write_text(R.dump_record(rec), encoding="utf-8")
    R.print_table(rec)
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
