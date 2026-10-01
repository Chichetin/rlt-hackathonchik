"""«Thin» OKPD2 categories: where the organizers' data shows few suppliers for the demand (#122, D-072).

    uv run python -m src.eval.thin T-thin-v1            # -> eval/results/T-thin-v1.json

The expert feedback of checkpoint 1: strengthen not the strong categories but those with few suppliers.
Unit: the OKPD2 group (`XX.XX`, `items.okpd2_l4`) over the lots of ONE channel (Электронный магазин by
default — the channel where a customer invites suppliers himself) published BEFORE the split cutoff
(configs/split.yaml): the dev/holdout periods are not read. A lot with positions of several groups
counts in each of them (the same convention as the class vote in discover.py).

Per group: lots, share of lots with exactly one offer (distinct valid supplier INNs), median offers,
distinct suppliers, suppliers per 100 lots. A group is thin when ALL hold (thresholds in `Thresholds`,
the choice is justified by `sensitivity` in the result file and by D-072):
- lots >= min_lots  (below that the share of single-offer lots is noise: its standard error at 50
  lots is up to 0.07);
- single_share >= min_single  (at least half of the lots have no competition at all);
- suppliers_per_100_lots <= max_per_100  (the supplier pool is small relative to the demand).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from src.eval.run import RESULTS, git
from src.eval.split import ROOT, split_config
from src.match.index import processed_dir


@dataclass(frozen=True)
class Thresholds:
    min_lots: int = 50
    min_single: float = 0.5
    max_per_100: float = 25.0


@dataclass(frozen=True)
class Group:
    okpd2: str
    lots: int
    single_share: float
    median_offers: float
    suppliers: int
    per_100_lots: float


_SQL = """
WITH lot_class AS (
    SELECT DISTINCT i.okpd2_l4 AS cls, i.lot_id
    FROM read_parquet(?) i JOIN read_parquet(?) n USING (lot_id)
    WHERE n.channel = ? AND n.publish_date < CAST(? AS DATE) AND i.okpd2_l4 IS NOT NULL
), offers AS (
    SELECT DISTINCT lot_id, supplier_inn FROM read_parquet(?)
    WHERE channel = ? AND inn_valid AND supplier_inn IS NOT NULL
), per_lot AS (
    SELECT lot_id, count(*) AS n FROM offers GROUP BY lot_id
), lots AS (
    SELECT lc.cls, count(*) AS lots,
           count(*) FILTER (WHERE pl.n = 1) AS single, median(pl.n) AS med
    FROM lot_class lc JOIN per_lot pl USING (lot_id) GROUP BY lc.cls
), sup AS (
    SELECT lc.cls, count(DISTINCT o.supplier_inn) AS suppliers
    FROM lot_class lc JOIN offers o USING (lot_id) GROUP BY lc.cls
)
SELECT l.cls, l.lots, l.single, l.med, s.suppliers
FROM lots l JOIN sup s USING (cls) ORDER BY l.cls
"""


def compute_groups(processed: Path, cutoff: str, channel: str = "ЭМ") -> list[Group]:
    """Metrics of every OKPD2 group over the lots of `channel` published before `cutoff`."""
    items, notices, parts = (str(processed / f"{n}.parquet") for n in ("items", "notices", "participants"))
    con = duckdb.connect()
    try:
        rows = con.execute(_SQL, [items, notices, channel, cutoff, parts, channel]).fetchall()
    finally:
        con.close()
    return [Group(okpd2=cls, lots=lots, single_share=round(single / lots, 4), median_offers=float(med),
                  suppliers=sup, per_100_lots=round(sup * 100 / lots, 2))
            for cls, lots, single, med, sup in rows]


def is_thin(g: Group, t: Thresholds) -> bool:
    return g.lots >= t.min_lots and g.single_share >= t.min_single and g.per_100_lots <= t.max_per_100


def thin_groups(groups: list[Group], t: Thresholds) -> list[Group]:
    """Thin groups, the biggest demand first (ties: code), so `[:N]` is «the N thin groups that matter most»."""
    return sorted((g for g in groups if is_thin(g, t)), key=lambda g: (-g.lots, g.okpd2))


def _quantiles(values: list[float]) -> dict[str, float]:
    if len(values) < 2:
        return {}
    q = statistics.quantiles(values, n=4, method="inclusive")
    return {"p25": round(q[0], 3), "p50": round(q[1], 3), "p75": round(q[2], 3)}


def sensitivity(groups: list[Group], base: Thresholds) -> list[dict]:
    """How many groups are thin when one threshold moves and the others stay: the choice is not knife-edge."""
    out = []
    for field, values in (("min_lots", (30, 50, 100)), ("min_single", (0.4, 0.5, 0.6)),
                          ("max_per_100", (15.0, 25.0, 40.0))):
        for v in values:
            t = Thresholds(**{**asdict(base), field: v})
            thin = thin_groups(groups, t)
            out.append({"param": field, "value": v, "thin_groups": len(thin), "thin_lots": sum(g.lots for g in thin)})
    return out


def build_record(exp_id: str, groups: list[Group], t: Thresholds, cutoff: str, channel: str, top_n: int,
                 source: dict | None = None) -> dict:
    eligible = [g for g in groups if g.lots >= t.min_lots]
    thin = thin_groups(groups, t)
    return {
        "exp_id": exp_id,
        "kind": "thin-categories",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--", "src", "configs")),
        "config": {"channel": channel, "period": f"publish_date < {cutoff} (cutoff of configs/split.yaml)",
                   "unit": "items.okpd2_l4 (XX.XX)", **asdict(t), "top_n": top_n},
        "data": source or {},
        "population": {
            "groups": len(groups), "eligible_groups": len(eligible),
            "single_share_quantiles": _quantiles([g.single_share for g in eligible]),
            "per_100_lots_quantiles": _quantiles([g.per_100_lots for g in eligible]),
        },
        "metrics": {
            "thin_groups": len(thin), "thin_lots": sum(g.lots for g in thin),
            "eligible_lots": sum(g.lots for g in eligible),
            "thin_suppliers_sum": sum(g.suppliers for g in thin),  # a supplier of two groups counts twice
        },
        "sensitivity": sensitivity(groups, t),
        "top": [asdict(g) for g in thin[:top_n]],
        "thin": [asdict(g) for g in thin],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Тонкие категории ОКПД2 по данным организаторов -> eval/results/<id>.json")
    ap.add_argument("exp")
    ap.add_argument("--channel", default="ЭМ")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-lots", type=int, default=Thresholds.min_lots)
    ap.add_argument("--min-single", type=float, default=Thresholds.min_single)
    ap.add_argument("--max-per-100", type=float, default=Thresholds.max_per_100)
    args = ap.parse_args(argv)
    t = Thresholds(args.min_lots, args.min_single, args.max_per_100)
    cutoff = str(split_config()["cutoff"])
    processed = processed_dir()
    try:
        groups = compute_groups(processed, cutoff, args.channel)
    except (duckdb.IOException, FileNotFoundError) as e:
        print(f"данные организаторов не загружены ({processed}): {e}", file=sys.stderr)
        return 2
    manifest = processed / "manifest.json"
    source = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    rec = build_record(args.exp, groups, t, cutoff, args.channel, args.top, {"manifest": source})
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    m = rec["metrics"]
    print(f"групп {rec['population']['groups']}, с >= {t.min_lots} лотов {rec['population']['eligible_groups']}; "
          f"тонких {m['thin_groups']} ({m['thin_lots']} лотов {args.channel})")
    print("ОКПД2   лотов  один-оферта  медиана  поставщиков  на 100 лотов")
    for g in rec["top"]:
        print(f"{g['okpd2']:6} {g['lots']:7} {g['single_share']:10.2f} {g['median_offers']:8.1f} "
              f"{g['suppliers']:11} {g['per_100_lots']:12.1f}")
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
