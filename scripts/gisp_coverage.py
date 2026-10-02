"""ГИСП in the block «Новые» (#74): on how many lots a candidate with product_fit 1.0 confirmed by the registry
of industrial products appears — before (no export) and after (the export of src/sources/gisp.py).

Both runs are offline and read the same things: the source cache (a temporary copy — registry pages written by
discovery do not touch the original) and organizers' data (DATA_PROCESSED_DIR). Only the export directory differs.
Per lot: «Новые» non-empty, any product_fit 1.0, a registry-confirmed product (`Discovery.gisp_backed` in the
top-10), a company added by the registry alone, the Evidence check of every registry-confirmed candidate.

  uv run python scripts/gisp_coverage.py --gisp-dir /mnt/storage/rlt236/gisp \\
      --cases eval/cases/dev.jsonl --out eval/results/K3-gisp-dev.json \\
      --sample 30 --seed 74 --pairs-out eval/labels/gisp_pairs_<версия>.csv

`--pairs-out`: a seeded random sample of the shown registry-confirmed pairs (lot × company × product) for hand
labelling «поставляет ли этот товар» (the label column is empty: a person or a reviewer fills it).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.contracts import CounterpartyMatch, Query  # noqa: E402
from src.enrich import discover as D  # noqa: E402
from src.enrich.evidence import check_counterparty  # noqa: E402
from src.settings import get_settings  # noqa: E402
from src.sources import gisp  # noqa: E402
from src.sources.cache import SourceCache  # noqa: E402

_PRODUCT = re.compile(r"«(.*)» — ОКПД2 (\S+),")


def _pair(query: Query, m: CounterpartyMatch) -> dict:
    """What the user sees for one registry-confirmed company: the lot, the company, its product and the step."""
    fact = next((e.fact for e in m.counterparty.evidence
                 if e.source == gisp.SOURCE and e.fact.startswith("Производит")), "")
    found = _PRODUCT.search(fact)
    return {"lot": query.query_id, "lot_okpd2": " ".join(query.okpd2), "lot_text": query.text[:400],
            "inn": m.counterparty.inn, "company": m.counterparty.name, "product": found.group(1) if found else "",
            "product_okpd2": found.group(2) if found else "", "product_fit": m.components[0].value, "label": ""}


def _run(query: Query, cache: SourceCache, gisp_dir: Path, pairs: list[dict] | None = None) -> dict:
    gisp.DIR = gisp_dir
    res = D.discover(query, cache=cache, offline=True)
    shown = res.matches
    backed = [m for m in shown if (m.counterparty.inn or "") in res.gisp_backed]
    if pairs is not None:
        pairs.extend(_pair(query, m) for m in backed)
    fails = sum(bool(check_counterparty(m.counterparty, cache.page_text)) for m in backed)
    return {"new": len(shown), "fit1": sum(m.components[0].value >= 1.0 for m in shown), "gisp": len(backed),
            "gisp_only": sum(not any(e.source != gisp.SOURCE for e in m.counterparty.evidence) for m in backed),
            "evidence_fail": fails}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--cases", type=Path, default=ROOT / "eval" / "cases" / "dev.jsonl")
    ap.add_argument("--gisp-dir", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=0, help="first N lots (0 — all)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--pairs-out", type=Path, help="CSV: a random sample of shown registry-confirmed pairs")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--seed", type=int, default=74)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    if not gisp.available(args.gisp_dir):
        print(f"нет выгрузки ГИСП в {args.gisp_dir}: python -m src.sources.gisp build", file=sys.stderr)
        return 2
    lines = args.cases.read_text(encoding="utf-8").splitlines()
    cases = [json.loads(x) for x in lines if x.strip()][: args.limit or None]
    src = get_settings().source_cache
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                            cwd=ROOT).stdout.strip()  # at the start: the code that is measured
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "sources.sqlite"
        if src.is_file():
            shutil.copy(src, copy)
        cache = SourceCache(copy)
        absent = Path(tmp) / "no-gisp"
        per: list[dict] = []
        pairs: list[dict] = []
        for case in cases:
            q = Query(**case["query"])
            per.append({"lot": q.query_id, "okpd2": q.okpd2, "before": _run(q, cache, absent),
                        "after": _run(q, cache, args.gisp_dir, pairs)})
    with_code = [p for p in per if p["okpd2"]]

    def lots(when: str, key: str) -> int:
        return sum(p[when][key] > 0 for p in per)

    summary = {
        "lots": len(per), "lots_with_okpd2": len(with_code),
        "lots_with_new": {"before": lots("before", "new"), "after": lots("after", "new")},
        "lots_with_fit1": {"before": lots("before", "fit1"), "after": lots("after", "fit1")},
        "lots_with_gisp_fit1": {"before": lots("before", "gisp"), "after": lots("after", "gisp")},
        "lots_with_gisp_only_company": lots("after", "gisp_only"),
        "gisp_candidates_in_top10": sum(p["after"]["gisp"] for p in per),
        "gisp_candidates_evidence_fail": sum(p["after"]["evidence_fail"] for p in per),
    }
    result = {"experiment": "K3-gisp", "issue": 74, "commit": commit, "cases": str(args.cases.relative_to(ROOT)
              if args.cases.is_relative_to(ROOT) else args.cases), "gisp_meta": gisp.meta(args.gisp_dir),
              "source_cache": str(src), "offline": True, "seconds": round(time.time() - t0, 1),
              "summary": summary, "per_lot": per}
    print(json.dumps({k: v for k, v in result.items() if k != "per_lot"}, ensure_ascii=False, indent=1))
    if args.pairs_out:
        sample = random.Random(args.seed).sample(pairs, min(args.sample, len(pairs)))
        with args.pairs_out.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(sample[0]) if sample else ["lot"])
            w.writeheader()
            w.writerows(sample)
        result["pairs"] = {"shown": len(pairs), "sampled": len(sample), "seed": args.seed, "file": str(args.pairs_out)}
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
