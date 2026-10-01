"""Two eval results give the same answer query by query (#49: search on Postgres = search on parquet).

    DATA_STORE=parquet  make eval EXP=bm25-dev-parquet
    DATA_STORE=postgres make eval EXP=bm25-dev-pg
    uv run python scripts/same_eval.py bm25-dev-parquet bm25-dev-pg

Exit 0 when the metrics and every per-query value are equal, 1 with the differing queries otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "eval" / "results"


def load(exp: str) -> dict:
    return json.loads((RESULTS / f"{exp}.json").read_text(encoding="utf-8"))


def diff(a: dict, b: dict) -> list[str]:
    out = [f"{k}: {a['metrics'][k]['value']} != {b['metrics'][k]['value']}"
           for k in a["metrics"] if a["metrics"][k]["value"] != b["metrics"].get(k, {}).get("value")]
    qa = {q["query_id"]: q for q in a["per_query"]}
    qb = {q["query_id"]: q for q in b["per_query"]}
    if qa.keys() != qb.keys():
        out.append(f"разные наборы запросов: {len(qa)} и {len(qb)}")
    out += [f"{qid}: {qa[qid]} != {qb[qid]}" for qid in sorted(qa.keys() & qb.keys()) if qa[qid] != qb[qid]]
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    a, b = map(load, argv)
    bad = diff(a, b)
    stores = f"{a.get('store', '?')} / {b.get('store', '?')}"
    if bad:
        print(f"{argv[0]} и {argv[1]} ({stores}) расходятся в {len(bad)} местах:", *bad[:20], sep="\n  ")
        return 1
    print(f"{argv[0]} = {argv[1]} ({stores}): метрики и {len(a['per_query'])} запросов совпадают")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
