"""How distinguishable the scores of the returned top-k are (#196).

    uv run python -m src.eval.saturation                      # the product configuration
    uv run python -m src.eval.saturation --config-of H-196-sat500 --limit 300

Re-runs search() over the first `--limit` dev cases (as_of = cutoff) and reports how many different
scores the top-k holds: a top-10 of ten equal scores cannot explain why the first is above the tenth.
The numbers of research/ERROR_ANALYSIS.md ("Насыщение баллов") come from this command.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date

from src.eval.run import load_method_config, with_cutoff
from src.eval.split import load_cases, split_config
from src.match import search as search_mod

TOP_SCORE = 0.999  # a first place at (almost) the maximum of the scale


def summarize(tops: list[list[float]], k: int = 10, decimals: int = 4) -> dict:
    """`tops`: the scores of the returned top-k per case, best first; empty answers are skipped.

    `decimals`: the precision the scores are compared at — 4 is what the API returns, 2 is what the
    interface shows (`toFixed(2)`): scores that differ in the API can still look equal on the screen.
    """
    tops = [[round(s, decimals) for s in t[:k]] for t in tops if t]
    distinct = [len(set(t)) for t in tops]
    return {
        "cases": len(tops),
        "distinct_scores_mean": round(statistics.mean(distinct), 2) if distinct else None,
        "share_le_3_distinct": round(sum(d <= 3 for d in distinct) / len(tops), 3) if tops else None,
        "share_all_equal": round(sum(d == 1 for d, t in zip(distinct, tops, strict=True) if len(t) > 1) / len(tops), 3)
        if tops else None,
        "share_first_at_max": round(sum(t[0] >= TOP_SCORE for t in tops) / len(tops), 3) if tops else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-of", default=None, help="entry of configs/experiments.yaml; default — the product")
    ap.add_argument("--limit", type=int, default=2000, help="first N dev cases (the whole sample takes ~10 min)")
    ap.add_argument("-k", type=int, default=10)
    args = ap.parse_args(argv)
    try:
        cfg = load_method_config(args.config_of or "product", config_of=args.config_of)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    split = split_config()
    cutoff = date.fromisoformat(str(split["cutoff"]))
    cases = load_cases("dev", split)[: args.limit]
    tops = [[m.score for m in search_mod.search(with_cutoff(c.query, cutoff), k=args.k, weights=cfg.get("weights"),
                                                config=cfg)] for c in cases]
    print(f"Насыщение баллов в top-{args.k} | конфигурация: {args.config_of or 'продукт'} | dev, первые {len(cases)}")
    for decimals, where in ((4, "как в ответе API (4 знака)"), (2, "как на экране (2 знака)")):
        print(f"— {where}")
        print(json.dumps(summarize(tops, args.k, decimals), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
