"""Lot whose OKPD2 code disagrees with its text (#229, decision 18): search by the code, by the text, or adaptive.

    uv run python -m src.eval.code_text --exp X-code-text [--n 400] [--seed 0]

No leak: the index holds history strictly before the split cutoff (the dev cases carry `as_of = cutoff`);
the queries are dev lots, the holdout is never read. Two sets of the same lots:
  real — the lot's own codes;
  swap — one code of ANOTHER section, drawn from the codes of the other sampled lots (the organizers'
         «код не бьётся с текстом», research/feedback/2026-10-01-checkpoint1.md).
Three answers per lot: `code` — search() with the codes (what /api/search does); `text` — search() without
codes; `adaptive` — `make predict` full mode: the text when src.ingest.predict.code_disagrees() says the
code's sections get < MISMATCH_SHARE of the votes of the lots similar by text, else the code.
Metric — recall@10 over the relevant INNs of the case (winner and offers), bootstrap CI over lots;
`flagged` — the share of lots the adaptive rule sent to the text.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from src.eval.metrics import bootstrap_ci, recall_at_k
from src.eval.run import result_record
from src.eval.split import ROOT, EvalCase, load_cases, split_config
from src.ingest import predict as predict_mod
from src.match import search as search_mod
from src.match.store import processed_dir

K = 10


def _swap_codes(cases: list[EvalCase], seed: int) -> dict[str, list[str]]:
    """For each case one code of a section none of its own codes has (deterministic by seed)."""
    rng = random.Random(seed)
    pool = sorted({c for case in cases for c in case.query.okpd2 if len(c) >= 5})
    out = {}
    for case in cases:
        own = {c[:2] for c in case.query.okpd2}
        other = [c for c in pool if c[:2] not in own]
        out[case.query.query_id] = [rng.choice(other)] if other else []
    return out


def _ci(vals: list[float]) -> dict:
    lo, hi = bootstrap_ci(vals)
    return {"mean": round(sum(vals) / len(vals), 4) if vals else None, "ci95": [round(lo, 4), round(hi, 4)],
            "n": len(vals)}


def evaluate(cases: list[EvalCase], processed: Path, seed: int = 0) -> dict:
    started = time.monotonic()
    swap = _swap_codes(cases, seed)
    known = predict_mod.known_classes(processed)
    per = []
    for case in cases:
        q = case.query
        rel = set(case.relevant_inns)
        by_text = search_mod.search(q.model_copy(update={"okpd2": []}), k=K)
        shares = predict_mod.section_shares(by_text, processed)
        text_ranked = [m.counterparty.inn or "" for m in by_text]
        row: dict = {"query_id": q.query_id, "text": q.text[:120], "codes": q.okpd2, "swap": swap[q.query_id]}
        for kind, codes in (("real", q.okpd2), ("swap", swap[q.query_id])):
            by_code = search_mod.search(q.model_copy(update={"okpd2": codes}), k=K)
            code_ranked = [m.counterparty.inn or "" for m in by_code]
            why = predict_mod.code_disagrees(codes, shares, None, known)
            row[kind] = {"code": recall_at_k(code_ranked, rel, K), "text": recall_at_k(text_ranked, rel, K),
                         "adaptive": recall_at_k(text_ranked if why else code_ranked, rel, K), "flagged": bool(why)}
        per.append(row)
    metrics = {kind: {**{a: _ci([r[kind][a] for r in per]) for a in ("code", "text", "adaptive")},
                      "flagged": round(sum(r[kind]["flagged"] for r in per) / len(per), 4) if per else None}
               for kind in ("real", "swap")}
    return {"n_queries": len(per), "elapsed_s": round(time.monotonic() - started, 1), "metrics": metrics,
            "per_query": per}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="код ОКПД2 против текста лота (#229)")
    ap.add_argument("--exp", required=True)
    ap.add_argument("--n", type=int, default=400, help="лотов dev (детерминированная выборка по seed)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    split = split_config()
    cases = [c for c in load_cases("dev", split) if c.query.okpd2]
    cases = sorted(random.Random(args.seed).sample(cases, min(args.n, len(cases))), key=lambda c: c.query.query_id)
    body = evaluate(cases, processed_dir(), args.seed)
    cfg = {"method": "code_text", "k": K, "n": args.n, "seed": args.seed, "cutoff": str(split["cutoff"]),
           "mismatch_share": predict_mod.MISMATCH_SHARE, "vote_matches": predict_mod.VOTE_MATCHES}
    rec = result_record(args.exp, "dev", cfg, body)
    out = ROOT / "eval" / "results" / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    for kind, m in body["metrics"].items():
        cells = " · ".join(f"{a} {m[a]['mean']:.3f} [{m[a]['ci95'][0]:.3f}, {m[a]['ci95'][1]:.3f}]"
                           for a in ("code", "text", "adaptive"))
        print(f"{kind}: recall@{K} {cells}; по тексту {m['flagged']:.0%} лотов")
    print(f"→ {out.relative_to(ROOT).as_posix()}, n={body['n_queries']}, {body['elapsed_s']} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
