"""Where the scoring of search() loses relevant suppliers that another run keeps in the top-k.

    uv run python -m src.eval.lost E-002 B-bm25     # relevant in B-bm25 top-10, not in E-002 top-10

Reads eval/results/<id>.json (per_query.ranked) and re-runs search() with the whole candidate pool
for the cases where the second run has the higher recall@10: the rank of every lost supplier, its
components against the mean of the returned top-k and the component with the largest weighted
deficit. The numbers of research/ERROR_ANALYSIS.md come from this command.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from src.eval.run import RESULTS, with_cutoff
from src.eval.split import EvalCase, load_cases, split_config
from src.match import search as search_mod

HOME_INN = ("78", "47")  # INN prefixes of Saint Petersburg and the Leningrad region


def lost_relevant(a: dict, b: dict, relevant: dict[str, set[str]], k: int = 10) -> list[tuple[str, str]]:
    """(query_id, inn): relevant, in the top-k of `b`, not in the top-k of `a` — only for the cases
    where `b` has the higher recall@k (records of `make eval`, same cases)."""
    rows_b = {q["query_id"]: q for q in b["per_query"]}
    out: list[tuple[str, str]] = []
    for q in a["per_query"]:
        other = rows_b[q["query_id"]]
        if other[f"recall@{k}"] <= q[f"recall@{k}"]:
            continue
        kept = set(other["ranked"][:k]) & relevant[q["query_id"]]
        out.extend((q["query_id"], inn) for inn in sorted(kept - set(q["ranked"][:k])))
    return out


def explain(lost: list[tuple[str, str]], cases: dict[str, EvalCase], cutoff: date, k: int = 10) -> dict:
    """Re-runs search() over the candidate pool for the lost pairs and aggregates the reasons."""
    weights = search_mod.load_config()["weights"]
    by_case: dict[str, list[str]] = {}
    for qid, inn in lost:
        by_case.setdefault(qid, []).append(inn)
    ranks: list[int] = []
    deltas: dict[str, list[float]] = {}
    worst: Counter[str] = Counter()
    distinct: list[int] = []
    geo = Counter()
    for qid, inns in by_case.items():
        matches = search_mod.search(with_cutoff(cases[qid].query, cutoff), k=search_mod.CANDIDATES)
        order = [m.counterparty.inn for m in matches]
        comps = {m.counterparty.inn: {c.name: c.value for c in m.components if c.weight > 0} for m in matches}
        top = order[:k]
        distinct.append(len({m.score for m in matches[:k]}))
        for inn in inns:
            if inn not in comps:
                worst["нет среди кандидатов"] += 1
                continue
            ranks.append(order.index(inn) + 1)
            d = {n: v - statistics.mean(comps[t][n] for t in top) for n, v in comps[inn].items()}
            for n, v in d.items():
                deltas.setdefault(n, []).append(v)
            worst[min(d, key=lambda n: d[n] * weights.get(n, 0.0))] += 1
            if comps[inn].get("geography") == 0:
                geo["geography=0"] += 1
                if len(inn) == 12:
                    geo["из них ИП (ИНН 12 знаков)"] += 1
                    geo["из них ИП с ИНН 78/47"] += inn[:2] in HOME_INN
    return {
        "cases": len(by_case), "lost": len(lost),
        "rank": {"median": statistics.median(ranks) if ranks else None,
                 f"{k + 1}-20": sum(k < r <= 20 for r in ranks), "21-50": sum(20 < r <= 50 for r in ranks),
                 ">50": sum(r > 50 for r in ranks)},
        "distinct_scores_top": {"mean": round(statistics.mean(distinct), 2) if distinct else None,
                                "<=3": sum(d <= 3 for d in distinct)},
        "component_delta": {n: round(statistics.mean(v), 3) for n, v in deltas.items()},
        "largest_weighted_deficit": dict(worst.most_common()),
        "geography": dict(geo),
    }


def main(argv: list[str] | None = None, results: Path = RESULTS) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a", help="experiment id of search() whose losses are explained, e.g. E-002")
    ap.add_argument("b", help="experiment id that keeps the suppliers, e.g. B-bm25")
    ap.add_argument("-k", type=int, default=10)
    args = ap.parse_args(argv)
    a, b = (json.loads((results / f"{x}.json").read_text(encoding="utf-8")) for x in (args.a, args.b))
    if a["part"] != b["part"] or {q["query_id"] for q in a["per_query"]} != {q["query_id"] for q in b["per_query"]}:
        print(f"{args.a} и {args.b}: разные наборы кейсов", file=sys.stderr)
        return 2
    split = split_config()
    cases = {c.query.query_id: c for c in load_cases(a["part"], split)}
    lost = lost_relevant(a, b, {qid: set(c.relevant_inns) for qid, c in cases.items()}, args.k)
    rec = explain(lost, cases, date.fromisoformat(str(split["cutoff"])), args.k)
    print(f"В top-{args.k} у {args.b}, но не у {args.a} | part={a['part']}")
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
