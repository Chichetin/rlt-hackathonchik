"""Eval on dev cases that were NOT used to tune the scoring (#145, docs/REVIEW.md).

The 2 000-case dev sample (eval/cases/dev.jsonl) is where the H-15* weights and options were chosen
by trying variants, so its numbers can be optimistic. This draws a seeded sample from dev_full minus
those 2 000 cases (same period, same as_of = cutoff, holdout untouched) and runs the same `evaluate()`
as `make eval`. `--free-text` turns every case into what a user types in the UI: the lot subject only
(the first «; » segment of the case text) and no OKPD2 codes.

    uv run python scripts/fresh_eval.py --exp R-fresh-v1                       # product config
    uv run python scripts/fresh_eval.py --exp R-fresh-H-15f --config-of H-15f  # configs/experiments.yaml
    uv run python scripts/fresh_eval.py --exp R-fresh-B-bm25 --baseline B-bm25 # configs/baselines.yaml
    uv run python scripts/fresh_eval.py --exp R-free-v1 --free-text
    uv run python -m src.eval.compare R-fresh-H-15f R-fresh-B-bm25            # paired delta

DEV_FULL — path of dev_full.jsonl when it is not under this checkout (a worktree without data/).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # `uv run python scripts/fresh_eval.py` without PYTHONPATH

from src.eval.run import (  # noqa: E402
    RESULTS,
    ROOT,
    dump_record,
    evaluate,
    load_method_config,
    print_table,
    result_record,
)
from src.eval.split import EvalCase, load_cases, split_config  # noqa: E402

SEED = 145
N = 2000


def fresh_cases(cfg: dict, n: int = N, seed: int = SEED) -> list[EvalCase]:
    """n dev_full cases outside the tuning sample, seeded, ordered by query_id."""
    tuned = {c.query.query_id for c in load_cases("dev", cfg)}
    pool = [c for c in load_cases("dev_full", cfg) if c.query.query_id not in tuned]
    if n > len(pool):
        raise ValueError(f"в dev_full вне выборки подбора {len(pool)} кейсов, запрошено {n}")
    return sorted(random.Random(seed).sample(pool, n), key=lambda c: c.query.query_id)


def free_text(case: EvalCase) -> EvalCase:
    """The UI path: the subject a user would type, no OKPD2 (customer, budget, channel are kept)."""
    q = case.query
    return case.model_copy(update={"query": q.model_copy(update={"text": q.text.split("; ")[0], "okpd2": []})})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exp", required=True)
    ap.add_argument("--baseline", default=None, help="entry of configs/baselines.yaml")
    ap.add_argument("--config-of", default=None, help="entry of configs/experiments.yaml")
    ap.add_argument("--free-text", action="store_true", help="subject only, no OKPD2 — the UI free-text path")
    ap.add_argument("--n", type=int, default=N)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--force", action="store_true", help="overwrite an existing eval/results/<exp>.json")
    args = ap.parse_args(argv)
    if args.exp.startswith("final"):
        print("id final* зарезервирован за make eval-final", file=sys.stderr)
        return 2
    if args.baseline and args.config_of:
        print("--baseline и --config-of вместе не задаются", file=sys.stderr)
        return 2
    if (RESULTS / f"{args.exp}.json").exists() and not args.force:
        # eval/results/H-15f.json and others are cited by D-063 and pitch/facts.md: never replaced silently
        print(f"eval/results/{args.exp}.json уже есть — другой --exp или --force", file=sys.stderr)
        return 2
    cfg_split = split_config()
    if os.environ.get("DEV_FULL"):
        cfg_split["cases"] = {**cfg_split["cases"], "dev_full": os.environ["DEV_FULL"]}
    cases = fresh_cases(cfg_split, args.n, args.seed)
    if args.free_text:
        cases = [free_text(c) for c in cases]
    cfg = load_method_config(args.exp, args.baseline, args.config_of)
    part = f"dev_fresh{'_free_text' if args.free_text else ''}(n={args.n},seed={args.seed})"
    rec = result_record(args.exp, part, cfg, evaluate(cases, cfg))
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(dump_record(rec), encoding="utf-8")
    print_table(rec)
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
