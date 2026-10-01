"""Where real winners come from, by OKPD2 class: the basis of purchase_type.REMOTE_CLASSES and of the share of
winners that «Может работать у заказчика» = auto hides (#242, D-092).

Wins of customers with INN 78 / 47 in data/processed (organizers' data). The supplier's region is its KPP (a 00…
stub is not a region), an ИП without a KPP — its INN, else unknown; «far» — neither the customer's region nor its
neighbour (78 <-> 47), as src/match/filters.py. Part 1: per class (the mode of the lot's positions) — wins and the
far share; REMOTE_CLASSES are the service classes with >= 100 wins and >= 20 %. Part 2: per purchase type of the
lot (purchase_type over all its positions) — what auto applies and the share of winners it hides (far + unknown).

    uv run python scripts/near_by_class.py [data/processed]
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.match.purchase_type import default_near, purchase_type  # noqa: E402

NEAR = ("78", "47")


def main(data: Path) -> None:
    d = data.as_posix()
    con = duckdb.connect()
    con.execute(f"""
        create temp table w as
        select p.lot_id,
          case when regexp_matches(trim(coalesce(p.supplier_kpp, '')), '^[0-9]{{2}}') and left(trim(p.supplier_kpp), 2) <> '00'
                 then left(trim(p.supplier_kpp), 2)
               when length(p.supplier_inn) = 12 then left(p.supplier_inn, 2) end as sreg
        from '{d}/participants.parquet' p join '{d}/notices.parquet' n using (lot_id)
        where p.is_winner and left(n.customer_inn, 2) in {NEAR}""")
    rows = con.execute(f"""
        with cls as (select lot_id, mode(okpd2_l2) as cls from '{d}/items.parquet'
                     where okpd2_l2 is not null group by lot_id)
        select cls, count(*), 100.0 * avg(case when sreg is null then null when sreg in {NEAR} then 0 else 1 end)
        from w join cls using (lot_id) group by 1 order by 1""").fetchall()
    print("class\twins\tfar_%")
    for cls, wins, far in rows:
        print(f"{cls}\t{wins}\t{far:.1f}")

    codes = dict(con.execute(f"""select lot_id, list(okpd2_code) from '{d}/items.parquet'
                                 where okpd2_code is not null group by lot_id""").fetchall())
    seen: Counter[tuple[str, str]] = Counter()
    for lot_id, sreg in con.execute("select lot_id, sreg from w").fetchall():
        kind = purchase_type(codes.get(lot_id, []))
        where = "unknown" if sreg is None else "near" if sreg in NEAR else "far"
        seen[(str(kind), where)] += 1
    print("\ntype\tauto\twins\thidden_%\t(far_%, unknown_%)")
    for kind in ("goods", "works", "services", "remote", "None"):
        n = {w: seen[(kind, w)] for w in ("near", "far", "unknown")}
        total = sum(n.values())
        if not total:
            continue
        auto = default_near(None if kind == "None" else kind)  # type: ignore[arg-type]
        hidden = 0 if auto == "any" else n["far"] + n["unknown"]
        print(f"{kind}\t{auto}\t{total}\t{100 * hidden / total:.1f}\t"
              f"({100 * n['far'] / total:.1f}, {100 * n['unknown'] / total:.1f})")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "processed")
