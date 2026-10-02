"""Table «we / art7235» on the same lots (#342): reads our eval/results/X-art-*.json and their per-lot json.

    uv run python scripts/compare_art7235.py \\
        --their-a eval/external/art7235-A.json --their-b eval/external/art7235-B.json

Setting A: history before 2025-01-01 (their protocol), our method v2 (no ranker: the ranker is trained on
2025-04…06 lots). Setting B: before 2025-07-01, our product method, lots from 2025-07-01 only (our dev).
winner@10 — a winner of the lot is in the top-10 (what art7235 reports). Intervals: bootstrap over lots;
the difference is paired (the same lot in both systems). Their rank is cut at 10, so our winner MRR is cut
at 10 too (a reciprocal rank below 0.1 counts as 0).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # runs without PYTHONPATH

from src.eval.external import publish_dates  # noqa: E402
from src.eval.metrics import bootstrap_ci  # noqa: E402
from src.eval.split import ROOT  # noqa: E402
from src.match.store import processed_dir  # noqa: E402

DEV_START = date(2025, 7, 1)  # the window where the weights of v2 were tuned (configs/split.yaml cutoff)
SETTINGS = (("A", "X-art-A-v2", "их протокол: история до 2025-01-01, метод v2"),
            ("B", "X-art-B-prod", "наш dev: история до 2025-07-01, продукт с ранжировщиком"))


def _ours(exp: str) -> dict[str, dict]:
    path = ROOT / "eval" / "results" / f"{exp}.json"
    rows = json.loads(path.read_text(encoding="utf-8"))["per_query"]
    return {q["query_id"].removeprefix("lot-"): q for q in rows}


def _fmt(v: list[float] | np.ndarray) -> str:
    lo, hi = bootstrap_ci(list(v))
    return f"{np.mean(v):.3f} [{lo:.3f}, {hi:.3f}]"


def _signed(v: np.ndarray) -> str:
    lo, hi = bootstrap_ci(list(v))
    return f"{v.mean():+.3f} [{lo:+.3f}, {hi:+.3f}]"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--their-a", type=Path, required=True)
    ap.add_argument("--their-b", type=Path, required=True)
    args = ap.parse_args(argv)
    processed = processed_dir()
    for (tag, prod, note), their_file in zip(SETTINGS, (args.their_a, args.their_b), strict=True):
        theirs = {str(r["lot_id"]): r for r in json.loads(their_file.read_text(encoding="utf-8"))["rows"]}
        ours = _ours(prod)
        keys = [k for k in ours if k in theirs and ours[k].get("winner@10") is not None]
        ids = [int(k) for k in keys]  # only the evaluated lots: none of them is from the sealed period
        published = publish_dates(processed, ids)
        offers = dict(duckdb.sql(
            "SELECT lot_id, count(DISTINCT supplier_inn) FROM "
            f"'{(processed / 'participants.parquet').as_posix()}' WHERE lot_id IN ({','.join(map(str, ids))}) "
            "GROUP BY 1").fetchall())
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
        # channel, number of offers, publish date
        info = {k: (ours[k]["channel"], offers.get(int(k), 0), published[int(k)]) for k in keys}
        slices: tuple[tuple[str, Callable[[tuple], bool]], ...] = (
            ("все", lambda _i: True),
            ("ЭМ", lambda i: i[0] == "ЭМ"),
            ("АИС ГЗ", lambda i: i[0] == "АИС ГЗ"),
            ("ЭМ, ≥ 2 участников (как у Robox)", lambda i: i[0] == "ЭМ" and i[1] >= 2),
            ("до 2025-07-01 (вне нашего dev)", lambda i: i[2] < DEV_START),
            ("с 2025-07-01 (наш dev)", lambda i: i[2] >= DEV_START))
        print(f"  {'срез':36s} {'n':>5}  {'мы':22s} {'art7235':22s} разность (парная)")
        for name, pick in slices:
            sub = [k for k in keys if pick(info[k])]
            if not sub:
                continue
            o = np.array([ours[k]["winner@10"] for k in sub])
            a = np.array([float(theirs[k]["card"] > 0) for k in sub])
            print(f"  {name:36s} {len(sub):5d}  {_fmt(o):22s} {_fmt(a):22s} {_signed(o - a)}")
        mrr_o = np.mean([ours[k]["winner_mrr"] if ours[k]["winner_mrr"] >= 0.1 else 0.0 for k in keys])
        mrr_t = np.mean([1 / theirs[k]["card"] if theirs[k]["card"] else 0.0 for k in keys])
        print(f"  MRR по победителю в топ-10: мы {mrr_o:.3f}, art7235 {mrr_t:.3f}")
        print(f"  recall@10 (у нас — по релевантным: ЭМ все участники, АИС ГЗ победители; у них — по всем участникам "
              f"с ИНН, определения разные): мы {np.mean([ours[k]['recall@10'] for k in keys]):.3f}, "
              f"art7235 {np.mean([theirs[k]['recall'] for k in keys]):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
