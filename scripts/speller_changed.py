"""How often the speller (#282) changes a CLEAN query — the price of `spell: true` the eval metrics cannot show.

Two inputs, dictionary of the backtest index (as_of = cutoff, no leak):
- long: the 2 000 dev queries (lot texts) — `make eval` input;
- short: 2 000 «manual» queries — the first 1–3 words of position names of the dev lots (seed 282).
Writes eval/results/S-282-changed.json: shares, every (typed, searched) pair with its count.

    uv run python scripts/speller_changed.py
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.eval.run import tree_dirty  # noqa: E402
from src.eval.split import load_cases, split_config  # noqa: E402
from src.match import speller  # noqa: E402
from src.match.index import get_index, processed_dir  # noqa: E402

SEED = 282
N_SHORT = 2000


def _changed(sp: speller.Speller, texts: list[str]) -> dict:
    fixed = [sp.correct(t) for t in texts]
    pairs = Counter(p for c in fixed for p in c.changes)
    n = sum(c.changed for c in fixed)
    return {"n": len(texts), "changed": n, "share": round(n / len(texts), 4) if texts else 0.0,
            "pairs": [[typed, searched, k] for (typed, searched), k in pairs.most_common()]}


def main() -> int:
    cfg = split_config()
    cutoff = date.fromisoformat(str(cfg["cutoff"]))
    sp = speller.for_index(get_index(None, cutoff).path)
    cases = load_cases("dev", cfg)
    lots = sorted({int(c.query.query_id.split("-", 1)[1]) for c in cases if c.query.query_id.startswith("lot-")})
    names = [r[0] for r in duckdb.execute(
        "SELECT product_name FROM read_parquet(?) WHERE lot_id IN (SELECT unnest(?)) AND product_name IS NOT NULL "
        "ORDER BY lot_id, product_name", [str(processed_dir() / "items.parquet"), lots]).fetchall()]
    rng = random.Random(SEED)
    rng.shuffle(names)
    short = [" ".join(n.split()[: rng.randint(1, 3)]) for n in names if n.split()][:N_SHORT]
    git = ["git", "-C", str(ROOT)]
    commit = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = tree_dirty()  # the same paths as every eval result and the eval-final gate (#455)
    rec = {"exp_id": "S-282-changed", "created_at": datetime.now(UTC).isoformat(timespec="seconds"), "commit": commit,
           "dirty": dirty, "as_of": cutoff.isoformat(), "long": _changed(sp, [c.query.text for c in cases]),
           "short": _changed(sp, short)}
    out = ROOT / "eval" / "results" / "S-282-changed.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for part in ("long", "short"):
        r = rec[part]
        print(f"{part}: изменено {r['changed']} из {r['n']} ({r['share']:.2%}); {r['pairs'][:15]}")
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
