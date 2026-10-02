"""Table «we / art7235» on the same lots (#342): reads our eval/results/X-art-*.json and their per-lot json.

    uv run python scripts/compare_art7235.py --their-a art_A.json --their-b art_B.json

Setting A: history before 2025-01-01 (their protocol), our method v2 (no ranker: the ranker is trained on
2025-04…06 lots). Setting B: before 2025-07-01, our product method, lots from 2025-07-01 only.
winner@10 — a winner of the lot is in the top-10 (what art7235 reports). Intervals: bootstrap over lots.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import numpy as np

from src.eval.metrics import bootstrap_ci
from src.eval.split import ROOT
from src.match.store import processed_dir

SETTINGS = (("A", "X-art-A-v2", "их протокол: история до 2025-01-01, метод v2"),
            ("B", "X-art-B-prod", "наш dev: история до 2025-07-01, продукт с ранжировщиком"))


def _ours(exp: str) -> dict[str, dict]:
    path = ROOT / "eval" / "results" / f"{exp}.json"
    rows = json.loads(path.read_text(encoding="utf-8"))["per_query"]
    return {q["query_id"].removeprefix("lot-"): q for q in rows}


def _fmt(v: list[float]) -> str:
    lo, hi = bootstrap_ci(v)
    return f"{np.mean(v):.3f} [{lo:.3f}, {hi:.3f}]"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--their-a", type=Path, required=True)
    ap.add_argument("--their-b", type=Path, required=True)
    args = ap.parse_args(argv)
    parts = processed_dir() / "participants.parquet"
    n_offers = dict(duckdb.sql(
        f"SELECT lot_id::VARCHAR, count(DISTINCT supplier_inn) FROM '{parts.as_posix()}' GROUP BY 1").fetchall())
    for (tag, prod, note), their_file in zip(SETTINGS, (args.their_a, args.their_b), strict=True):
        theirs = {str(r["lot_id"]): r for r in json.loads(their_file.read_text(encoding="utf-8"))["rows"]}
        ours = _ours(prod)
        keys = [k for k in ours if k in theirs and ours[k].get("winner@10") is not None]
        print(f"\n== {tag}: {note}; наши кейсы {len(ours)}, их лоты {len(theirs)}, общих с победителем {len(keys)}")
        rows = {f"мы {prod}": [ours[k]["winner@10"] for k in keys],
                "art7235 (движок)": [float(theirs[k]["card"] > 0) for k in keys],
                "art7235 база «заказчик + ОКПД2»": [float(theirs[k]["base_customer"] > 0) for k in keys],
                "art7235 база «топ по ОКПД2»": [float(theirs[k]["base_okpd"] > 0) for k in keys]}
        for base in ("B-pop", "B-inc", "B-bm25"):
            b = _ours(f"X-art-{tag}-{base}")
            rows[f"мы {base}"] = [b[k]["winner@10"] for k in keys]
        for name, v in rows.items():
            print(f"  {name:36s} winner@10 {_fmt(v)}")
        diff = np.array(rows[f"мы {prod}"]) - np.array(rows["art7235 (движок)"])
        lo, hi = bootstrap_ci(diff)
        print(f"  парная разность мы − art7235: {diff.mean():+.3f} [{lo:+.3f}, {hi:+.3f}]  (n={len(diff)})")
        for name, pick in (("ЭМ", lambda k, o=ours: o[k]["channel"] == "ЭМ"),
                           ("АИС ГЗ", lambda k, o=ours: o[k]["channel"] == "АИС ГЗ"),
                           ("ЭМ, ≥ 2 участников (как у Robox)",
                            lambda k, o=ours: o[k]["channel"] == "ЭМ" and n_offers.get(k, 0) >= 2)):
            sub = [k for k in keys if pick(k)]
            print(f"  {name:36s} n={len(sub):4d} мы {_fmt([ours[k]['winner@10'] for k in sub])}  "
                  f"art7235 {_fmt([float(theirs[k]['card'] > 0) for k in sub])}")
        print(f"  recall@10 по участникам: мы {np.mean([ours[k]['recall@10'] for k in keys]):.3f}, "
              f"art7235 {np.mean([theirs[k]['recall'] for k in keys]):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
