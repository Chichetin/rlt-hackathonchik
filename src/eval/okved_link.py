"""Evaluation of the learned OKVED → OKPD2 link (src/enrich/okved_link.py, #250) on cold dev winners.

    uv run python -m src.eval.okved_link --exp L-okved-link-v1   # → eval/results/<exp>.json

For a dev winner absent from train («холодный») and present in the FNS MSP pool: is its main OKVED among the
OKVED chosen for the classes of its lot — by the class itself, by rule D-036 (class + hand-made wholesale
table), by the learned link and by their unions — and how many СПб+ЛО pool companies each choice selects.
The link is learned on lots before `cutoff`; lots from `holdout_from` on are never read.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from src.enrich.okved_link import (
    MIN_LIFT,
    MIN_SUPPLIERS,
    OKV,
    POOL,
    PROCESSED,
    REGIONS,
    ROOT,
    TOP,
    lit,
    missing,
    prepare,
    rule_d036,
    select,
    split_dates,
    src_of,
)
from src.eval.metrics import bootstrap_ci

RESULTS = ROOT / "eval" / "results"


def evaluate(cutoff: date, holdout_from: date, processed: Path = PROCESSED, pool: Path = POOL, *,
             min_lift: float = MIN_LIFT, min_suppliers: int = MIN_SUPPLIERS, top: int = TOP) -> dict:
    """Cold dev winners: share whose main OKVED is selected, per rule, with the size of the selection."""
    con = duckdb.connect()
    prepare(con, processed, pool, cutoff)
    lo, hi = lit(cutoff.isoformat()), lit(holdout_from.isoformat())
    con.execute(f"""CREATE TEMP TABLE cold AS SELECT DISTINCT lot_id, supplier_inn AS inn FROM p
        WHERE is_winner AND publish_date >= DATE {lo} AND publish_date < DATE {hi}
          AND supplier_inn NOT IN (SELECT supplier_inn FROM p WHERE publish_date < DATE {lo})""")
    cold_all = con.execute("SELECT count(*) FROM cold").fetchone()[0]  # type: ignore[index]
    con.execute("CREATE TEMP TABLE dev AS SELECT c.lot_id, c.inn, ok.okv FROM cold c JOIN ok USING (inn)")
    pairs = con.execute("SELECT lot_id, inn FROM dev ORDER BY lot_id, inn").fetchall()
    con.execute(f"CREATE TEMP TABLE cnt AS SELECT {OKV} AS okv, count(*) AS n FROM {src_of(pool / 'okved.parquet')} "
                f"WHERE main AND region_code IN ({','.join(map(lit, REGIONS))}) GROUP BY 1")
    con.execute("CREATE TEMP TABLE okall AS SELECT okv FROM ok UNION SELECT okv FROM cnt")
    classes = [r[0] for r in con.execute("SELECT DISTINCT cls FROM lc").fetchall()]
    con.execute("CREATE TEMP TABLE d036 (cls VARCHAR, pfx VARCHAR)")
    con.executemany("INSERT INTO d036 VALUES (?, ?)", rule_d036(classes))
    learned = f"SELECT cls, okved AS pfx FROM ({select(min_lift, min_suppliers, top)})"
    rules = {
        "class": "SELECT cls, cls AS pfx FROM (SELECT DISTINCT cls FROM lc)",
        "d036": "SELECT cls, pfx FROM d036",
        "learned": learned,
        "class+learned": f"SELECT cls, cls AS pfx FROM (SELECT DISTINCT cls FROM lc) UNION {learned}",
        "d036+learned": f"SELECT cls, pfx FROM d036 UNION {learned}",
    }
    hits: dict[str, list[float]] = {}
    out: dict[str, dict] = {}
    for name, sql in rules.items():
        con.execute("DROP TABLE IF EXISTS rx")
        con.execute(f"CREATE TEMP TABLE rx AS SELECT DISTINCT r.cls, k.okv FROM ({sql}) r JOIN okall k "
                    "ON starts_with(k.okv, r.pfx)")
        found = set(con.execute("SELECT DISTINCT d.lot_id, d.inn FROM dev d JOIN lc USING (lot_id) "
                                "JOIN rx ON rx.cls = lc.cls AND rx.okv = d.okv").fetchall())
        hits[name] = [1.0 if p in found else 0.0 for p in pairs]
        size = con.execute("""SELECT median(s), avg(s) FROM (SELECT d.lot_id, coalesce(sum(t.n), 0) AS s
            FROM (SELECT DISTINCT lot_id FROM dev) d LEFT JOIN (SELECT DISTINCT lc.lot_id, k.okv, k.n FROM lc
            JOIN rx USING (cls) JOIN cnt k USING (okv)) t USING (lot_id) GROUP BY 1)""").fetchone()
        mean = sum(hits[name]) / len(pairs) if pairs else 0.0
        out[name] = {"hit": round(mean, 4), "ci95": [round(v, 4) for v in bootstrap_ci(hits[name])],
                     "candidates_median": float(size[0] or 0),  # type: ignore[index]
                     "candidates_mean": round(float(size[1] or 0), 1)}  # type: ignore[index]
    deltas = {}
    for a, b in (("class+learned", "d036"), ("d036+learned", "d036"), ("learned", "d036")):
        diff = [x - y for x, y in zip(hits[a], hits[b], strict=True)]
        deltas[f"{a} - {b}"] = {"delta": round(sum(diff) / len(diff), 4) if diff else 0.0,
                                "ci95": [round(v, 4) for v in bootstrap_ci(diff)]}
    rows, link_classes = con.execute("SELECT count(*), count(DISTINCT cls) FROM link").fetchone()  # type: ignore[misc]
    return {"n": len(pairs), "cold_winner_pairs": cold_all,
            "cold_in_pool_share": round(len(pairs) / cold_all, 4) if cold_all else 0.0,
            "rules": out, "deltas": deltas, "link": {"rows": rows, "classes": link_classes}}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT, check=False).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Learned OKVED → OKPD2 link against rule D-036 on cold dev winners (#250)")
    ap.add_argument("--exp", default="L-okved-link-v1")
    ap.add_argument("--processed", type=Path, default=PROCESSED)
    ap.add_argument("--pool", type=Path, default=POOL)
    ap.add_argument("--min-lift", type=float, default=MIN_LIFT)
    ap.add_argument("--min-suppliers", type=int, default=MIN_SUPPLIERS)
    ap.add_argument("--top", type=int, default=TOP)
    args = ap.parse_args(argv)
    if (need := missing(args.processed, args.pool)) is not None:
        print(f"нет {need}: данные организаторов — make data, пул — make pool-load (#185)", file=sys.stderr)
        return 2
    cutoff, holdout_from = split_dates()
    body = evaluate(cutoff, holdout_from, args.processed, args.pool, min_lift=args.min_lift,
                    min_suppliers=args.min_suppliers, top=args.top)
    rec = {"exp_id": args.exp, "part": "dev (cold winners in the MSP pool)",
           "created_at": datetime.now(UTC).isoformat(timespec="seconds"), "commit": _git("rev-parse", "HEAD"),
           "dirty": bool(_git("status", "--porcelain", "--", "src", "configs")),
           "config": {"cutoff": cutoff.isoformat(), "holdout_from": holdout_from.isoformat(), "min_lift": args.min_lift,
                      "min_suppliers": args.min_suppliers, "top": args.top, "regions": list(REGIONS)},
           **body}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{args.exp}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, r in body["rules"].items():
        print(f"{name:15s} hit {r['hit']:.3f} [{r['ci95'][0]:.3f}, {r['ci95'][1]:.3f}]  "
              f"кандидатов СПб+ЛО, медиана {r['candidates_median']:.0f}")
    for name, d in body["deltas"].items():
        print(f"{name:25s} {d['delta']:+.3f} [{d['ci95'][0]:+.3f}, {d['ci95'][1]:+.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
