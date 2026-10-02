"""OKVED → OKPD2 link learned from history: which activity codes the suppliers of an OKPD2 class have (#250).

There is no official OKPD2 ↔ OKVED key. Rule D-036 (src/enrich/discover.py) takes the class itself as an
OKVED prefix plus a hand-made wholesale table for 145 of 530 classes. Here the link is counted instead:
suppliers of the organizers' data → their main OKVED in the FNS MSP pool (src/db/pool.py, #185) → the OKPD2
classes of the lots they took part in. A new company with the same main OKVED inherits the profile.

    uv run python -m src.enrich.okved_link build [--as-of 2025-07-01]   # → data/processed/okved_link.parquet
    uv run python -m src.enrich.okved_link show 21.20
    uv run python -m src.enrich.okved_link eval --exp L-okved-link-v1   # → eval/results/<exp>.json

Row (cls, okved): `suppliers` — pool-matched suppliers of the class whose MAIN OKVED is `okved` (class level,
`46.46`); `share` — their part among the pool-matched suppliers of the class; `lift` — `share` against the
part of that OKVED among all pool-matched suppliers (46.90 «неспециализированная торговля» is in every
class, its lift is ≈ 1; 46.46 for 21.20 is 4.5). Only lots published before `as_of` are read: the
evaluation learns on train (configs/split.yaml `cutoff`) and never opens the holdout.

`eval`: for a dev winner absent from train («холодный») and present in the pool — is its main OKVED among
the OKVED chosen for the classes of its lot, and how many СПб+ЛО pool companies that choice selects.
The pool is one snapshot (dump of 10.09.2026): a company's OKVED is today's, not the one at the lot's date.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from src.enrich.discover import wholesale_okveds
from src.eval.metrics import bootstrap_ci
from src.eval.split import split_config

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
POOL = ROOT / "data" / "external" / "rsmp" / "pool"
TABLE = "okved_link.parquet"
RESULTS = ROOT / "eval" / "results"
REGIONS = ("78", "47")

# an OKVED at least this many times more frequent in the class than among all suppliers (L-okved-link-v1)
MIN_LIFT = 10.0
MIN_SUPPLIERS = 2  # one supplier is an anecdote, not a link
TOP = 10  # OKVED codes per class at most


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _src(path: Path) -> str:
    return f"read_parquet({_lit(path.as_posix())})"


# class level of an OKVED code: `46.46.1` → `46.46`; shorter codes stay as they are
_OKV = "CASE WHEN length(code) >= 5 THEN substr(code, 1, 5) ELSE code END"


def _prepare(con: duckdb.DuckDBPyConnection, processed: Path, pool: Path, as_of: date) -> None:
    """Temp tables: lc (lot → class), ok (supplier → main OKVED), tr (class → supplier before as_of), link."""
    con.execute(f"CREATE TEMP VIEW p AS SELECT * FROM {_src(processed / 'participants.parquet')} WHERE inn_valid")
    con.execute("CREATE TEMP TABLE lc AS SELECT DISTINCT lot_id, okpd2_l4 AS cls "
                f"FROM {_src(processed / 'items.parquet')} WHERE okpd2_l4 IS NOT NULL")
    con.execute(f"CREATE TEMP TABLE ok AS SELECT DISTINCT o.inn, {_OKV} AS okv FROM {_src(pool / 'okved.parquet')} o "
                "WHERE o.main AND o.inn IN (SELECT supplier_inn FROM p)")
    con.execute("CREATE TEMP TABLE tr AS SELECT DISTINCT lc.cls, p.supplier_inn AS inn FROM p JOIN lc USING (lot_id) "
                f"JOIN ok ON ok.inn = p.supplier_inn WHERE p.publish_date < DATE {_lit(as_of.isoformat())}")
    total = con.execute("SELECT count(DISTINCT inn) FROM tr").fetchone()[0]  # type: ignore[index]
    con.execute(f"""CREATE TEMP TABLE link AS
        SELECT l.cls, l.okv AS okved, l.n AS suppliers, c.n AS class_suppliers, l.n * 1.0 / c.n AS share,
               (l.n * 1.0 / c.n) / (o.n * 1.0 / {max(total, 1)}) AS lift
        FROM (SELECT tr.cls, ok.okv, count(*) AS n FROM tr JOIN ok USING (inn) GROUP BY 1, 2) l
        JOIN (SELECT cls, count(*) AS n FROM tr GROUP BY 1) c USING (cls)
        JOIN (SELECT ok.okv, count(*) AS n FROM ok WHERE inn IN (SELECT inn FROM tr) GROUP BY 1) o USING (okv)""")


def build(as_of: date, processed: Path = PROCESSED, pool: Path = POOL, out: Path | None = None) -> dict[str, int]:
    """Link table of the lots published before `as_of` → `out` (default `<processed>/okved_link.parquet`)."""
    out = out or processed / TABLE
    con = duckdb.connect()
    _prepare(con, processed, pool, as_of)
    tmp = out.with_suffix(".parquet.tmp")
    con.execute("COPY (SELECT * FROM link ORDER BY cls, suppliers DESC, okved) "
                f"TO {_lit(tmp.as_posix())} (FORMAT parquet)")
    tmp.replace(out)
    rows, classes = con.execute("SELECT count(*), count(DISTINCT cls) FROM link").fetchone()  # type: ignore[misc]
    return {"rows": rows, "classes": classes}


@dataclass(frozen=True)
class Link:
    okved: str
    suppliers: int
    class_suppliers: int
    lift: float

    def why(self, cls: str) -> str:
        return (f"основной ОКВЭД {self.okved} — у {self.suppliers} из {self.class_suppliers} поставщиков класса "
                f"{cls} в истории закупок (в {self.lift:.1f} раза чаще, чем в среднем)")


def _select(min_lift: float, min_suppliers: int, top: int, table: str = "link") -> str:
    return (f"SELECT cls, okved, suppliers, class_suppliers, lift FROM (SELECT *, row_number() OVER "
            f"(PARTITION BY cls ORDER BY suppliers DESC, okved) AS r FROM {table} "
            f"WHERE lift >= {float(min_lift)} AND suppliers >= {int(min_suppliers)}) WHERE r <= {int(top)}")


def okveds(cls: str, table: Path = PROCESSED / TABLE, *, min_lift: float = MIN_LIFT,
           min_suppliers: int = MIN_SUPPLIERS, top: int = TOP) -> list[Link]:
    """OKVED codes learned for an OKPD2 class, the most frequent first; [] without the table or the class."""
    if not table.exists():
        return []
    rows = duckdb.connect().execute(
        f"SELECT okved, suppliers, class_suppliers, lift FROM ({_select(min_lift, min_suppliers, top, _src(table))}) "
        "WHERE cls = ? ORDER BY suppliers DESC, okved", [cls]).fetchall()
    return [Link(*r) for r in rows]


def rule_d036(classes: Sequence[str]) -> list[tuple[str, str]]:
    """(class, OKVED prefix) of rule D-036 at class level: the class itself + the wholesale row of the table."""
    return [(c, p) for c in classes for p in (c, *wholesale_okveds(c))]


def evaluate(cutoff: date, holdout_from: date, processed: Path = PROCESSED, pool: Path = POOL, *,
             min_lift: float = MIN_LIFT, min_suppliers: int = MIN_SUPPLIERS, top: int = TOP) -> dict:
    """Cold dev winners: share whose main OKVED is selected, per rule, with the size of the selection."""
    con = duckdb.connect()
    _prepare(con, processed, pool, cutoff)
    lo, hi = _lit(cutoff.isoformat()), _lit(holdout_from.isoformat())
    con.execute(f"""CREATE TEMP TABLE cold AS SELECT DISTINCT lot_id, supplier_inn AS inn FROM p
        WHERE is_winner AND publish_date >= DATE {lo} AND publish_date < DATE {hi}
          AND supplier_inn NOT IN (SELECT supplier_inn FROM p WHERE publish_date < DATE {lo})""")
    cold_all = con.execute("SELECT count(*) FROM cold").fetchone()[0]  # type: ignore[index]
    con.execute("CREATE TEMP TABLE dev AS SELECT c.lot_id, c.inn, ok.okv FROM cold c JOIN ok USING (inn)")
    pairs = con.execute("SELECT lot_id, inn FROM dev ORDER BY lot_id, inn").fetchall()
    con.execute(f"CREATE TEMP TABLE cnt AS SELECT {_OKV} AS okv, count(*) AS n FROM {_src(pool / 'okved.parquet')} "
                f"WHERE main AND region_code IN ({','.join(map(_lit, REGIONS))}) GROUP BY 1")
    con.execute("CREATE TEMP TABLE okall AS SELECT okv FROM ok UNION SELECT okv FROM cnt")
    classes = [r[0] for r in con.execute("SELECT DISTINCT cls FROM lc").fetchall()]
    con.execute("CREATE TEMP TABLE d036 (cls VARCHAR, pfx VARCHAR)")
    con.executemany("INSERT INTO d036 VALUES (?, ?)", rule_d036(classes))
    learned = f"SELECT cls, okved AS pfx FROM ({_select(min_lift, min_suppliers, top)})"
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
    ap = argparse.ArgumentParser(description="OKVED → OKPD2 link learned from history (#250)")
    ap.add_argument("command", choices=("build", "show", "eval"))
    ap.add_argument("cls", nargs="?", help="show: OKPD2 class, e.g. 21.20")
    ap.add_argument("--as-of", type=date.fromisoformat,
                    help="build: lots before this date (default — cutoff of the split)")
    ap.add_argument("--processed", type=Path, default=PROCESSED)
    ap.add_argument("--pool", type=Path, default=POOL)
    ap.add_argument("--exp", default="L-okved-link-v1")
    ap.add_argument("--min-lift", type=float, default=MIN_LIFT)
    ap.add_argument("--min-suppliers", type=int, default=MIN_SUPPLIERS)
    ap.add_argument("--top", type=int, default=TOP)
    args = ap.parse_args(argv)
    needs = (args.processed / "participants.parquet", args.processed / "items.parquet", args.pool / "okved.parquet")
    for need in needs:
        if args.command != "show" and not need.exists():
            print(f"нет {need}: данные организаторов — make data, пул — make pool-load (#185)", file=sys.stderr)
            return 2
    split = split_config()
    cutoff, holdout_from = date.fromisoformat(split["cutoff"]), date.fromisoformat(split["holdout_from"])
    if args.command == "build":
        as_of = args.as_of or cutoff
        if as_of > holdout_from:
            print(f"--as-of {as_of} позже начала holdout ({holdout_from}): holdout не читаем", file=sys.stderr)
            return 2
        print(json.dumps({"as_of": as_of.isoformat(), **build(as_of, args.processed, args.pool)}, ensure_ascii=False))
        return 0
    if args.command == "show":
        if not args.cls:
            print("show <класс ОКПД2>", file=sys.stderr)
            return 2
        for link in okveds(args.cls, args.processed / TABLE, min_lift=args.min_lift,
                           min_suppliers=args.min_suppliers, top=args.top):
            print(link.why(args.cls))
        return 0
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
