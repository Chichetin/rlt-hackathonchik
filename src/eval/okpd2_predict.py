"""OKPD2 of a query from its text alone (#211, P-01): how often search()'s prediction (D-045) is right.

    uv run python -m src.eval.okpd2_predict --exp P-01 [--positions 3000] [--seed 0]

No leak: the index holds history strictly before the split cutoff; queries come from the dev lots
(eval/cases/dev.jsonl, [cutoff, holdout_from)), the holdout is never read.
  A — a position name of a dev lot → the code of that position;
  B — the lot text of the case (subject + position names) → a hit when the predicted class is a class
      of the lot's positions.
Levels: division XX, class XX.XX, subcategory XX.XX.X, full code XX.XX.XX.XXX. A level finer than the
true code is skipped for that query (truth `33.12.1` has no full code). Baselines: the most frequent
code of the history (one answer for all); for A also «the same name in the history» — the code most
often given to exactly this name before the cutoff, none for a new name.
Slices: A — the name was seen in the history or is new, and the mean over lots (one lot — one vote:
big estimates dominate the rows); B — lots of one class and of several (several classes make «any» easy).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import date
from pathlib import Path

import duckdb

from src.eval.metrics import bootstrap_ci
from src.eval.run import result_record
from src.eval.split import ROOT, EvalCase, load_cases, split_config
from src.match import fallback
from src.match import search as search_mod
from src.match.index import get_index
from src.match.store import get_store, processed_dir
from src.match.text import tokenize

LEVELS = {"division": 2, "class": 5, "subcategory": 7, "code": 12}


def level_hit(pred: str | None, truth: str, level: str) -> float | None:
    """1/0 at the level; None when the true code is coarser than the level."""
    n = LEVELS[level]
    if len(truth) < n:
        return None
    return float(pred is not None and pred[:n] == truth[:n])


def predict(text: str, store, idx) -> str | None:
    """The code search() would assume for a query without OKPD2 (the same retrieval and vote)."""
    hits = idx.retrieve(fallback.cap_tokens(idx, tokenize(text)), search_mod.TOP_DOCS)
    return search_mod._predict_okpd2(store, idx, hits, idx.lot_hits(hits)) if hits else None


def _positions(processed: Path, lot_ids: list[int]) -> list[tuple[int, str, str]]:
    return duckdb.execute(
        "SELECT lot_id, product_name, trim(okpd2_code) FROM read_parquet(?) WHERE lot_id IN (SELECT unnest(?)) "
        "AND okpd2_code IS NOT NULL AND trim(okpd2_code) <> '' AND product_name IS NOT NULL "
        "ORDER BY lot_id, product_name, okpd2_code",
        [(processed / "items.parquet").as_posix(), lot_ids]).fetchall()


def _majority(processed: Path, cutoff: date) -> str:
    return duckdb.execute(
        "SELECT trim(i.okpd2_code) c FROM read_parquet(?) i JOIN read_parquet(?) n USING (lot_id) "
        "WHERE n.publish_date < ? AND i.okpd2_code IS NOT NULL AND trim(i.okpd2_code) <> '' "
        "GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1",
        [(processed / "items.parquet").as_posix(), (processed / "notices.parquet").as_posix(), cutoff]).fetchone()[0]  # type: ignore[index]


def _history_codes(processed: Path, cutoff: date, names: list[str]) -> dict[str, str]:
    """The code most often given to exactly this position name before the cutoff (ties: by code)."""
    rows = duckdb.execute(
        "SELECT product_name, c FROM (SELECT i.product_name, trim(i.okpd2_code) c, row_number() OVER "
        "(PARTITION BY i.product_name ORDER BY count(*) DESC, trim(i.okpd2_code)) r FROM read_parquet(?) i "
        "JOIN read_parquet(?) n USING (lot_id) WHERE n.publish_date < ? AND i.product_name IN (SELECT unnest(?)) "
        "AND i.okpd2_code IS NOT NULL AND trim(i.okpd2_code) <> '' GROUP BY 1, 2) WHERE r = 1",
        [(processed / "items.parquet").as_posix(), (processed / "notices.parquet").as_posix(), cutoff,
         sorted(set(names))]).fetchall()
    return dict(rows)


def _by_lot(rows: list[dict], key: str) -> dict:
    """Mean over lots of the per-lot mean: one lot — one vote."""
    out = {}
    for level in LEVELS:
        per: dict[int, list[float]] = {}
        for r in rows:
            if r[key][level] is not None:
                per.setdefault(r["lot_id"], []).append(r[key][level])
        vals = [sum(v) / len(v) for v in per.values()]
        lo, hi = bootstrap_ci(vals)
        out[level] = {"mean": round(sum(vals) / len(vals), 4) if vals else None, "ci95": [round(lo, 4), round(hi, 4)],
                      "n": len(vals)}
    return out


def _summary(rows: list[dict], key: str) -> dict:
    out = {}
    for level in LEVELS:
        vals = [r[key][level] for r in rows if r[key][level] is not None]
        lo, hi = bootstrap_ci(vals)
        out[level] = {"mean": round(sum(vals) / len(vals), 4) if vals else None, "ci95": [round(lo, 4), round(hi, 4)],
                      "n": len(vals)}
    return out


def evaluate(cases: list[EvalCase], cutoff: date, positions: int | None = None, seed: int = 0) -> dict:
    started = time.monotonic()
    store, processed = get_store(), processed_dir()
    idx = get_index(store, cutoff)
    lot_of = {int(c.query.query_id.removeprefix("lot-")): c for c in cases}
    rows = _positions(processed, list(lot_of))
    if positions is not None and len(rows) > positions:
        rows = sorted(random.Random(seed).sample(rows, positions))
    base = _majority(processed, cutoff)
    seen = _history_codes(processed, cutoff, [name for _, name, _ in rows])

    a = []
    for lot_id, name, truth in rows:
        pred = predict(name, store, idx)
        a.append({"lot_id": lot_id, "text": name, "truth": truth, "pred": pred, "seen": name in seen,
                  "hit": {lv: level_hit(pred, truth, lv) for lv in LEVELS},
                  "baseline": {lv: level_hit(base, truth, lv) for lv in LEVELS},
                  "same_name": {lv: level_hit(seen.get(name), truth, lv) for lv in LEVELS}})
    lot_codes: dict[int, set[str]] = {}
    for lot_id, _, code in _positions(processed, list(lot_of)):
        lot_codes.setdefault(lot_id, set()).add(code)
    b = []
    for lot_id, case in sorted(lot_of.items()):
        codes = lot_codes.get(lot_id) or set(case.query.okpd2)
        if not codes:
            continue
        pred = predict(case.query.text, store, idx)
        b.append({"lot_id": lot_id, "text": case.query.text[:200], "truth": sorted(codes), "pred": pred,
                  "hit": {lv: _any_hit(pred, codes, lv) for lv in LEVELS},
                  "baseline": {lv: _any_hit(base, codes, lv) for lv in LEVELS}})
    return {
        "n_queries": len(a) + len(b), "elapsed_s": round(time.monotonic() - started, 1), "baseline_code": base,
        "metrics": {
            "positions": {"n": len(a), "undetermined": sum(r["pred"] is None for r in a),
                          "accuracy": _summary(a, "hit"), "accuracy_by_lot": _by_lot(a, "hit"),
                          "baseline": _summary(a, "baseline"), "same_name": _summary(a, "same_name"),
                          "seen": {"n": sum(r["seen"] for r in a),
                                   "accuracy": _summary([r for r in a if r["seen"]], "hit"),
                                   "same_name": _summary([r for r in a if r["seen"]], "same_name")},
                          "new": {"n": sum(not r["seen"] for r in a),
                                  "accuracy": _summary([r for r in a if not r["seen"]], "hit")}},
            "lots": {"n": len(b), "undetermined": sum(r["pred"] is None for r in b),
                     "accuracy": _summary(b, "hit"), "baseline": _summary(b, "baseline"),
                     "one_class": _summary([r for r in b if len({c[:5] for c in r["truth"]}) == 1], "hit"),
                     "several_classes": _summary([r for r in b if len({c[:5] for c in r["truth"]}) > 1], "hit")},
        },
        "per_query": {"positions": a, "lots": b},
    }


def _any_hit(pred: str | None, truths: set[str], level: str) -> float | None:
    hits = [h for t in truths if (h := level_hit(pred, t, level)) is not None]
    return max(hits) if hits else None


def _print(m: dict, keys: list[str]) -> None:
    for lv in LEVELS:
        cells = [f"{k} {m[k][lv]['mean']:.3f} [{m[k][lv]['ci95'][0]:.3f}, {m[k][lv]['ci95'][1]:.3f}] n={m[k][lv]['n']}"
                 for k in keys if m[k][lv]["mean"] is not None]
        print(f"  {lv:12s} " + " · ".join(cells))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="точность определения ОКПД2 по тексту (#211)")
    ap.add_argument("--exp", required=True)
    ap.add_argument("--positions", type=int, default=3000, help="случайная выборка позиций dev-лотов (0 — все)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    split = split_config()
    cutoff = date.fromisoformat(str(split["cutoff"]))
    body = evaluate(load_cases("dev", split), cutoff, args.positions or None, args.seed)
    cfg = {"method": "okpd2_predict", "top_docs": search_mod.TOP_DOCS, "vote_docs": search_mod.VOTE_DOCS,
           "vote_lots": search_mod.VOTE_LOTS, "positions": args.positions, "seed": args.seed, "cutoff": str(cutoff)}
    rec = result_record(args.exp, "dev", cfg, body)
    out = ROOT / "eval" / "results" / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    m = body["metrics"]
    print(f"A, позиции: n={m['positions']['n']}, не определён {m['positions']['undetermined']}; "
          f"название было в истории: {m['positions']['seen']['n']}")
    _print(m["positions"], ["accuracy", "accuracy_by_lot", "baseline", "same_name"])
    print("  — название было в истории:")
    _print(m["positions"]["seen"], ["accuracy", "same_name"])
    print("  — новое название:")
    _print(m["positions"]["new"], ["accuracy"])
    print(f"Б, лоты: n={m['lots']['n']}, не определён {m['lots']['undetermined']}")
    _print(m["lots"], ["accuracy", "one_class", "several_classes", "baseline"])
    print(f"→ {out.relative_to(ROOT).as_posix()}, {body['elapsed_s']} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
