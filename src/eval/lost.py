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


def explain(lost: list[tuple[str, str]], cases: dict[str, EvalCase], cutoff: date, k: int = 10,
            recorded: dict[str, list[str]] | None = None) -> dict:
    """Re-runs search() over the candidate pool for the lost pairs and aggregates the reasons.

    `recorded`: query_id -> ranked list of the stored run; a re-run whose top-k differs is counted
    in `rerun_differs` (the method changed since the run — the ranks below are not its ranks).
    """
    weights = search_mod.load_config()["weights"]
    by_case: dict[str, list[str]] = {}
    for qid, inn in lost:
        by_case.setdefault(qid, []).append(inn)
    ranks: list[int] = []
    deltas: dict[str, list[float]] = {}
    worst: Counter[str] = Counter()
    distinct: list[int] = []
    geo: Counter[str] = Counter()
    differs = 0
    for qid, inns in by_case.items():
        matches = search_mod.search(with_cutoff(cases[qid].query, cutoff), k=search_mod.CANDIDATES)
        order = [m.counterparty.inn for m in matches]
        by_inn = {m.counterparty.inn: m for m in matches}
        comps = {inn: {c.name: c.value for c in m.components if c.weight > 0} for inn, m in by_inn.items()}
        top = order[:k]
        if recorded is not None and top != recorded.get(qid, [])[:k]:
            differs += 1
        distinct.append(len({m.score for m in matches[:k]}))
        for inn in inns:
            if inn not in comps:
                worst["not_in_candidates"] += 1
                continue
            ranks.append(order.index(inn) + 1)
            d = {n: v - statistics.mean(comps[t][n] for t in top) for n, v in comps[inn].items()}
            for n, v in d.items():
                deltas.setdefault(n, []).append(v)
            name = min(d, key=lambda n: d[n] * weights.get(n, 0.0))
            # no component below the top-k mean: the score ties with the top-k, the order is by INN
            worst[name if d[name] * weights.get(name, 0.0) < 0 else "tie_by_inn"] += 1
            if comps[inn].get("geography") == 0:
                geo["zero"] += 1
                if len(inn) == 12:  # an individual entrepreneur
                    kpp = by_inn[inn].counterparty.kpp
                    geo["ip"] += 1
                    geo["ip_no_kpp" if not kpp else "ip_kpp_zeros" if set(kpp) == {"0"} else "ip_other_region"] += 1
                    if inn[:2] in HOME_INN:
                        geo["ip_home_inn"] += 1
                        # upper bound of H-15c: with geography = 1 the score reaches the k-th place
                        gain = weights.get("geography", 0.0)
                        geo["ip_home_inn_reaches_top"] += by_inn[inn].score + gain >= matches[len(top) - 1].score
    return {
        "cases": len(by_case), "lost": len(lost), "rerun_differs": differs,
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
    rec = explain(lost, cases, date.fromisoformat(str(split["cutoff"])), args.k,
                  recorded={q["query_id"]: q["ranked"] for q in a["per_query"]})
    print(f"В top-{args.k} у {args.b}, но не у {args.a} | part={a['part']}")
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
