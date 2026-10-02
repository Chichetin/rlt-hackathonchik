"""Experimental dev-only replay: fixed RRF of base retrieval and customer history (#318).

    uv run python scripts/e6_fusion.py --text-result eval/results/E6-base-500.json \
        --pool-result eval/results/E6-cust-untrained-500.json --cutoff 2025-07-01 --exp E6-fusion-500

No model, fitting or parameter search: score = 1/(60+r_base) + 1/(60+r_customer),
with a missing source contributing zero. Only the recorded extended pool is eligible.
Base candidate_ids preserve retrieval order before reranking; they cannot distinguish
BM25 from the fallback ladder on queries without text matches. This is an offline
replay of retrieval order, not a verified replacement for the product pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval import run as R  # noqa: E402
from src.eval.metrics import METRICS, WINNER_METRICS  # noqa: E402
from src.eval.split import EvalCase, load_cases, split_config  # noqa: E402
from src.match.candidates import _groups  # noqa: E402
from src.match.fallback import okpd2_codes  # noqa: E402
from src.match.profiles import procurement_type  # noqa: E402
from src.match.search import own_lot  # noqa: E402
from src.match.store import Store, get_store  # noqa: E402

RRF_K = 60
K = 20
CUSTOMER_LIMIT = 30
RESULTS = R.RESULTS


def fuse(text: list[str], customer: list[str], pool: list[str]) -> list[str]:
    """Label-free ranking; duplicates get one vote and absent pool members cannot enter."""
    eligible = set(pool)
    scores = dict.fromkeys(eligible, 0.0)
    for source in (text, customer):
        unique = dict.fromkeys(inn for inn in source if inn in eligible)
        for rank, inn in enumerate(unique, 1):
            scores[inn] += 1.0 / (RRF_K + rank)
    return sorted(eligible, key=lambda inn: (-scores[inn], inn))


def trace(record: dict, row: dict) -> list[str]:
    """Decode only complete candidate traces, never the displayed top-k `ranked` list."""
    inns, ids = record.get("candidate_inns"), row.get("candidate_ids")
    if not isinstance(inns, list) or any(not isinstance(inn, str) or not inn for inn in inns):
        raise ValueError("candidate_inns must contain supplier INNs")
    if not isinstance(ids, list) or row.get("n_candidates") != len(ids):
        raise ValueError("complete candidate_ids and matching n_candidates are required")
    if any(type(i) is not int or not 0 <= i < len(inns) for i in ids):
        raise ValueError("candidate_ids contains an invalid dictionary index")
    return [inns[i] for i in ids]


def _rows(record: dict) -> dict[str, dict]:
    rows = record.get("per_query", [])
    if not rows or record.get("n_queries") != len(rows):
        raise ValueError("n_queries must match nonempty per_query")
    by_id = {row["query_id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("duplicate query IDs")
    return by_id


def validate(text: dict, pool: dict, cases: list[EvalCase], cutoff: date,
             dev_end: date) -> list[EvalCase]:
    """Fail closed on a changed case set, history window or base-method lineage."""
    part = text.get("part", "")
    if not re.fullmatch(r"dev(?:-s[1-9]\d*)?", part) or pool.get("part") != part:
        raise ValueError("matching dev/dev-sN parts are required; holdout is forbidden")
    for field in ("commit", "config_sha256", "tokenizer_version", "store"):
        if not text.get(field) or text[field] != pool.get(field):
            raise ValueError(f"incompatible base lineage: {field}")
    tc, pc = text["config"], pool["config"]
    for cfg, count in ((tc, 0), (pc, CUSTOMER_LIMIT)):
        if cfg.get("k") != K or cfg.get("retriever") != "bm25" or cfg.get("method", "search") != "search":
            raise ValueError("source runs must use search, bm25 and k=20")
        if cfg.get("candidates") != {"customer": count, "okpd2": 0}:
            raise ValueError("source pools must be text-only and text union customer30")
    if {k: v for k, v in tc.items() if k != "candidates"} != {k: v for k, v in pc.items() if k != "candidates"}:
        raise ValueError("incompatible base lineage: source configs differ beyond candidate expansion")
    tr, pr = _rows(text), _rows(pool)
    if tr.keys() != pr.keys():
        raise ValueError("incompatible case sets in source results")
    available = {c.query.query_id: c for c in cases}
    if len(available) != len(cases) or not tr.keys() <= available.keys():
        raise ValueError("source query IDs must identify unique current dev cases")
    if part == "dev" and tr.keys() != available.keys():
        raise ValueError("dev result must contain the complete current dev case set")
    if "-s" in part and int(part.split("-s")[1]) != len(tr):
        raise ValueError("dev-sN sample size differs from its case set")
    selected = [available[qid] for qid in tr]
    for case in selected:
        q = case.query
        if not cutoff <= case.date < dev_end or q.as_of not in (None, cutoff):
            raise ValueError(f"{q.query_id}: dev date or explicit cutoff mismatch")
        if q.filters:
            raise ValueError(f"{q.query_id}: raw candidate traces cannot replay user filters")
        if not set(trace(text, tr[q.query_id])) <= set(trace(pool, pr[q.query_id])):
            raise ValueError(f"{q.query_id}: extended pool is missing base candidates")
    return selected


def replay(text: dict, pool: dict, cases: list[EvalCase], cutoff: date,
           dev_end: date, store: Store) -> dict:
    """Rank every query before consulting relevance/winner labels for standard metrics."""
    selected = validate(text, pool, cases, cutoff, dev_end)
    if store.name != text["store"]:
        raise ValueError("customer-history store differs from source lineage")
    text_rows, pool_rows = _rows(text), _rows(pool)
    ranked_queries = []
    started = time.monotonic()
    for case in selected:
        q = case.query
        base = trace(text, text_rows[q.query_id])
        eligible = trace(pool, pool_rows[q.query_id])
        codes, _ = okpd2_codes(q.okpd2)
        groups = _groups(codes)
        customer = []
        if q.customer_inn and groups:
            own = own_lot(q)
            customer = [r[0] for r in store.customer_suppliers(q.customer_inn, groups, cutoff, CUSTOMER_LIMIT,
                                                             -1 if own is None else own)]
        ranked_queries.append((case, eligible, fuse(base, customer, eligible)[:K]))

    per_query = []
    for case, eligible, ranked in ranked_queries:
        source = pool_rows[case.query.query_id]
        relevant = set(case.relevant_inns)
        winners = {inn for inn, grade in case.grades.items() if grade >= 2}
        row = {"query_id": case.query.query_id, "ranked": ranked,
               "channel": case.query.channel, "type": procurement_type(case.query.okpd2) or "unknown",
               **{name: fn(ranked, relevant) for name, fn in METRICS.items()},
               **{name: fn(ranked, winners) if winners else None for name, fn in WINNER_METRICS.items()},
               "candidate_ids": list(source["candidate_ids"]), "n_candidates": source["n_candidates"],
               "candidate_winner_hit": float(bool(winners.intersection(eligible))) if winners else None}
        if "winner_new" in source:
            row["winner_new"] = source["winner_new"]
        per_query.append(row)
    return {"n_queries": len(per_query), "elapsed_s": round(time.monotonic() - started, 1),
            "metrics": R._summary(per_query), "slices": R.slices(per_query),
            "candidate_inns": list(pool["candidate_inns"]), "per_query": per_query}


def _read(path: Path) -> tuple[dict, dict]:
    data = path.read_bytes()
    return json.loads(data), {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text-result", type=Path, required=True)
    ap.add_argument("--pool-result", type=Path, required=True)
    ap.add_argument("--cutoff", type=date.fromisoformat, required=True)
    ap.add_argument("--exp", required=True)
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.exp) or args.exp.startswith("final"):
        ap.error("--exp must be a plain experiment name outside the reserved final* namespace")
    out = RESULTS / f"{args.exp}.json"
    try:
        if out.exists():
            raise ValueError(f"{out} already exists; choose another --exp")
        split = split_config()
        if args.cutoff != date.fromisoformat(str(split["cutoff"])):
            raise ValueError("--cutoff must equal the explicit dev split cutoff")
        text, text_source = _read(args.text_result)
        pool, pool_source = _read(args.pool_result)
        cases = load_cases("dev", split)  # no caller-controlled part or path to holdout
        body = replay(text, pool, cases, args.cutoff, date.fromisoformat(str(split["holdout_from"])), get_store())
        selected_ids = {row["query_id"] for row in body["per_query"]}
        selected = sorted((c for c in cases if c.query.query_id in selected_ids), key=lambda c: c.query.query_id)
        case_bytes = "\n".join(c.model_dump_json() for c in selected).encode()
        cfg = {**text["config"], "method": "offline_e6_fusion", "ranker": None, "k": K,
               "candidates": dict(pool["config"]["candidates"]), "cutoff": args.cutoff.isoformat(),
               "fusion": {"name": "rrk60", "rank_constant": RRF_K, "tie_break": "inn",
                          "sources": ["base_retrieval_order", "customer_history"], "missing_source": 0}}
        rec = R.result_record(args.exp, text["part"], cfg, body)
        rec["offline_replay"] = {
            "experimental": True, "sources": {"text_result": text_source, "pool_result": pool_source},
            "cases_sha256": hashlib.sha256(case_bytes).hexdigest(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "base_lineage": {key: text[key] for key in ("commit", "config_sha256", "tokenizer_version", "store")},
            "limitations": ["Base traces do not distinguish BM25 retrieval from fallback order.",
                            "Source results do not fingerprint historical store contents; same snapshot is required.",
                            "Offline replay has not been validated through the product pipeline."],
        }
        RESULTS.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as f:
            f.write(R.dump_record(rec))
        R.print_table(rec)
        print(f"-> {out}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
