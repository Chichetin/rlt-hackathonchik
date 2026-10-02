"""OKVED → OKPD2 link learned from history: which activity codes the suppliers of an OKPD2 class have (#250).

There is no official OKPD2 ↔ OKVED key. Rule D-036 (src/enrich/discover.py) takes the class itself as an
OKVED prefix plus a hand-made wholesale table for 145 of 530 classes. Here the link is counted instead:
suppliers of the organizers' data → their main OKVED in the FNS MSP pool (src/db/pool.py, #185) → the OKPD2
classes of the lots they took part in. A new company with the same main OKVED inherits the profile.

    uv run python -m src.enrich.okved_link build [--as-of 2025-07-01]   # → data/processed/okved_link.parquet
    uv run python -m src.enrich.okved_link show 21.20
    uv run python -m src.eval.okved_link --exp L-okved-link-v1          # → eval/results/<exp>.json

Row (cls, okved): `suppliers` — pool-matched suppliers of the class whose MAIN OKVED is `okved` (class level,
`46.46`); `share` — their part among the pool-matched suppliers of the class; `lift` — `share` against the
part of that OKVED among all pool-matched suppliers (46.90 «неспециализированная торговля» is in every
class, its lift is ≈ 1; 46.46 for 21.20 is 4.5). Only lots published before `as_of` are read: the
evaluation learns on train (configs/split.yaml `cutoff`) and never opens the holdout.

The evaluation (src/eval/okved_link.py): for a dev winner absent from train («холодный») and present in the
pool — is its main OKVED among the OKVED chosen for the classes of its lot, and how many СПб+ЛО pool
companies that choice selects.
The pool is one snapshot (dump of 10.09.2026): a company's OKVED is today's, not the one at the lot's date.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import yaml

from src.enrich.discover import wholesale_okveds

ROOT = Path(__file__).resolve().parents[2]
PROCESSED = ROOT / "data" / "processed"
POOL = ROOT / "data" / "external" / "rsmp" / "pool"
TABLE = "okved_link.parquet"
SPLIT = ROOT / "configs" / "split.yaml"
REGIONS = ("78", "47")

# an OKVED at least this many times more frequent in the class than among all suppliers (L-okved-link-v1)
MIN_LIFT = 10.0
MIN_SUPPLIERS = 2  # one supplier is an anecdote, not a link
TOP = 10  # OKVED codes per class at most


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def src_of(path: Path) -> str:
    return f"read_parquet({lit(path.as_posix())})"


# class level of an OKVED code: `46.46.1` → `46.46`; shorter codes stay as they are
OKV = "CASE WHEN length(code) >= 5 THEN substr(code, 1, 5) ELSE code END"


def prepare(con: duckdb.DuckDBPyConnection, processed: Path, pool: Path, as_of: date,
            until: date | None = None) -> None:
    """Temp tables: lc (lot → class), ok (supplier → main OKVED: full `code` and class-level `okv`),
    tr (class → supplier before as_of), link. Lots published from `until` (default `as_of`) on are not read."""
    con.execute(f"CREATE TEMP VIEW p AS SELECT * FROM {src_of(processed / 'participants.parquet')} "
                f"WHERE inn_valid AND publish_date < DATE {lit((until or as_of).isoformat())}")
    con.execute("CREATE TEMP TABLE lc AS SELECT DISTINCT lot_id, okpd2_l4 AS cls "
                f"FROM {src_of(processed / 'items.parquet')} WHERE okpd2_l4 IS NOT NULL")
    con.execute(f"CREATE TEMP TABLE ok AS SELECT DISTINCT o.inn, o.code, {OKV} AS okv "
                f"FROM {src_of(pool / 'okved.parquet')} o WHERE o.main AND o.inn IN (SELECT supplier_inn FROM p)")
    con.execute("CREATE TEMP TABLE tr AS SELECT DISTINCT lc.cls, p.supplier_inn AS inn FROM p JOIN lc USING (lot_id) "
                f"JOIN ok ON ok.inn = p.supplier_inn WHERE p.publish_date < DATE {lit(as_of.isoformat())}")
    total = con.execute("SELECT count(DISTINCT inn) FROM tr").fetchone()[0]  # type: ignore[index]
    con.execute(f"""CREATE TEMP TABLE link AS
        SELECT l.cls, l.okv AS okved, l.n AS suppliers, c.n AS class_suppliers, l.n * 1.0 / c.n AS share,
               (l.n * 1.0 / c.n) / (o.n * 1.0 / {max(total, 1)}) AS lift
        FROM (SELECT tr.cls, ok.okv, count(DISTINCT tr.inn) AS n FROM tr JOIN ok USING (inn) GROUP BY 1, 2) l
        JOIN (SELECT cls, count(*) AS n FROM tr GROUP BY 1) c USING (cls)
        JOIN (SELECT ok.okv, count(DISTINCT inn) AS n FROM ok WHERE inn IN (SELECT inn FROM tr) GROUP BY 1) o
          USING (okv)""")


def build(as_of: date, processed: Path = PROCESSED, pool: Path = POOL, out: Path | None = None) -> dict[str, int]:
    """Link table of the lots published before `as_of` → `out` (default `<processed>/okved_link.parquet`)."""
    out = out or processed / TABLE
    con = duckdb.connect()
    prepare(con, processed, pool, as_of)
    tmp = out.with_suffix(".parquet.tmp")
    con.execute("COPY (SELECT * FROM link ORDER BY cls, suppliers DESC, okved) "
                f"TO {lit(tmp.as_posix())} (FORMAT parquet)")
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


def select(min_lift: float, min_suppliers: int, top: int, table: str = "link") -> str:
    return (f"SELECT cls, okved, suppliers, class_suppliers, lift FROM (SELECT *, row_number() OVER "
            f"(PARTITION BY cls ORDER BY suppliers DESC, okved) AS r FROM {table} "
            f"WHERE lift >= {float(min_lift)} AND suppliers >= {int(min_suppliers)}) WHERE r <= {int(top)}")


def okveds(cls: str, table: Path = PROCESSED / TABLE, *, min_lift: float = MIN_LIFT,
           min_suppliers: int = MIN_SUPPLIERS, top: int = TOP) -> list[Link]:
    """OKVED codes learned for an OKPD2 class, the most frequent first; [] without the table or the class.

    A code is the class level of a company's main OKVED and is matched EXACTLY at that level: `46.46` covers
    46.46, 46.46.1, 46.46.2; a short code (`46.3`) means companies registered with exactly that code, not
    every 46.3x — the counts and the lift of the row are those of the exact code."""
    if not table.exists():
        return []
    rows = duckdb.connect().execute(
        f"SELECT okved, suppliers, class_suppliers, lift FROM ({select(min_lift, min_suppliers, top, src_of(table))}) "
        "WHERE cls = ? ORDER BY suppliers DESC, okved", [cls]).fetchall()
    return [Link(*r) for r in rows]


def rule_d036(classes: Sequence[str]) -> list[tuple[str, str]]:
    """(class, OKVED prefix) of rule D-036: the class itself + the wholesale row of the table for the class.
    A prefix is matched against the FULL main OKVED of a company (`46.46.2` is a row of the table)."""
    return [(c, p) for c in classes for p in (c, *wholesale_okveds(c))]


def split_dates(path: Path = SPLIT) -> tuple[date, date]:
    """(cutoff, holdout_from) of configs/split.yaml."""
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    return date.fromisoformat(cfg["cutoff"]), date.fromisoformat(cfg["holdout_from"])


def missing(processed: Path, pool: Path) -> Path | None:
    """The first absent input file, None when all are there."""
    needs = (processed / "participants.parquet", processed / "items.parquet", pool / "okved.parquet")
    return next((n for n in needs if not n.exists()), None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OKVED → OKPD2 link learned from history (#250)")
    ap.add_argument("command", choices=("build", "show"))
    ap.add_argument("cls", nargs="?", help="show: OKPD2 class, e.g. 21.20")
    ap.add_argument("--as-of", type=date.fromisoformat,
                    help="build: lots before this date (default — cutoff of the split)")
    ap.add_argument("--processed", type=Path, default=PROCESSED)
    ap.add_argument("--pool", type=Path, default=POOL)
    ap.add_argument("--min-lift", type=float, default=MIN_LIFT)
    ap.add_argument("--min-suppliers", type=int, default=MIN_SUPPLIERS)
    ap.add_argument("--top", type=int, default=TOP)
    args = ap.parse_args(argv)
    if args.command == "show":
        if not args.cls:
            print("show <класс ОКПД2>", file=sys.stderr)
            return 2
        for link in okveds(args.cls, args.processed / TABLE, min_lift=args.min_lift,
                           min_suppliers=args.min_suppliers, top=args.top):
            print(link.why(args.cls))
        return 0
    if (need := missing(args.processed, args.pool)) is not None:
        print(f"нет {need}: данные организаторов — make data, пул — make pool-load (#185)", file=sys.stderr)
        return 2
    cutoff, holdout_from = split_dates()
    as_of = args.as_of or cutoff
    if as_of > holdout_from:
        print(f"--as-of {as_of} позже начала holdout ({holdout_from}): holdout не читаем", file=sys.stderr)
        return 2
    print(json.dumps({"as_of": as_of.isoformat(), **build(as_of, args.processed, args.pool)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
