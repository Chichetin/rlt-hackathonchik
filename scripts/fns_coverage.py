"""Do the open FNS datasets cover the suppliers of the organizers' data — large companies included? (#219)

    uv run python scripts/fns_coverage.py [--dir data/external/fns_opendata] [--processed data/processed]
                                          [--msp <parquet of the SME registry> --msp-inn <column>]

Reads the archives `sshr2019.zip` (headcount) and `revexp.zip` (income / expenses) of nalog.gov.ru/opendata
and data/processed/participants.parquet. The parsed archives are kept next to them (`fns_inns.parquet`),
so a second run takes seconds. Prints, for legal entities and sole traders separately (the datasets list
legal entities only): how many suppliers, participations and wins of the organizers' data have an INN in
the datasets, and how many of them are above the SME thresholds (more than 250 employees or income above
2 bn ₽) — these cannot be in the SME registry, so the registry-based pool of «Новые» (#185) never sees them.
With `--msp` the same is split by membership in the SME registry: what share of the non-SME wins the open
datasets explain. `candidates` — legal entities above the thresholds with an INN issued in Saint Petersburg
or the Leningrad region that are NOT among the organizers' suppliers: the size of a «large and new» pool.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
# <СведНП НаимОрг="…" ИННЮЛ="7728872867"/><СведССЧР КолРаб="1"/> — the value element follows the INN directly
PAIR = {"sshr2019": re.compile("ИННЮЛ=\"(\\d{10})\"/><СведССЧР КолРаб=\"(\\d+)\"".encode()),
        "revexp": re.compile("ИННЮЛ=\"(\\d{10})\"/><СведДохРасх СумДоход=\"(-?[\\d.]+)\"".encode())}
ANY_INN = re.compile("ИННЮЛ=\"\\d{10}\"".encode())
SME_EMPLOYEES, SME_INCOME = 250, 2_000_000_000.0  # 209-ФЗ: ceiling of a medium enterprise
HOME = ("78", "47")


def read_dataset(path: Path, dataset: str) -> tuple[dict[str, float], int]:
    """INN -> value (the largest one if an INN repeats) and the number of documents the pattern missed."""
    out: dict[str, float] = {}
    missed = 0
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            raw = z.read(name)
            pairs = PAIR[dataset].findall(raw)
            missed += len(ANY_INN.findall(raw)) - len(pairs)
            for inn, value in pairs:
                key = inn.decode()
                out[key] = max(out.get(key, float("-inf")), float(value))
    return out, missed


def parsed(directory: Path) -> tuple[Path, dict]:
    """fns_inns.parquet (inn, employees, income) next to the archives; built once."""
    out = directory / "fns_inns.parquet"
    meta_path = directory / "fns_inns.json"
    if out.exists() and meta_path.exists():
        return out, json.loads(meta_path.read_text(encoding="utf-8"))
    employees, missed_e = read_dataset(directory / "sshr2019.zip", "sshr2019")
    income, missed_i = read_dataset(directory / "revexp.zip", "revexp")
    inns = sorted(set(employees) | set(income))
    pq.write_table(pa.table({"inn": inns, "employees": [employees.get(i) for i in inns],
                             "income": [income.get(i) for i in inns]}), out)
    meta = {"sshr2019_inns": len(employees), "revexp_inns": len(income), "any_inns": len(inns),
            "documents_not_parsed": {"sshr2019": missed_e, "revexp": missed_i}}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return out, meta


def coverage(processed: Path, fns: Path, msp: Path | None = None, msp_inn: str = "inn") -> dict:
    con = duckdb.connect()
    large = f"(f.employees > {SME_EMPLOYEES} OR f.income > {SME_INCOME})"
    con.execute(f"CREATE VIEW fns AS SELECT * FROM read_parquet('{fns.as_posix()}')")
    con.execute(f"""
        CREATE TABLE p AS
        SELECT supplier_inn AS inn, lot_id, bool_or(is_winner) AS win
        FROM read_parquet('{(processed / "participants.parquet").as_posix()}') WHERE inn_valid
        GROUP BY supplier_inn, lot_id""")
    if msp is not None:
        con.execute(f"CREATE TABLE msp AS SELECT DISTINCT {msp_inn} AS inn FROM read_parquet('{msp.as_posix()}')")
        registry = "CASE WHEN m.inn IS NULL THEN 'not_in_sme_registry' ELSE 'in_sme_registry' END"
        join = "LEFT JOIN msp m USING (inn)"
    else:
        registry, join = "'registry_unknown'", ""
    q = f"""
        SELECT CASE WHEN length(p.inn) = 10 THEN 'legal' ELSE 'sole_trader' END || ':' || {registry} AS grp,
               count(DISTINCT p.inn) AS suppliers, count(*) AS participations, sum(win::INT) AS wins,
               count(DISTINCT p.inn) FILTER (f.inn IS NOT NULL) AS suppliers_in_fns,
               count(*) FILTER (f.inn IS NOT NULL) AS participations_in_fns,
               coalesce(sum(win::INT) FILTER (f.inn IS NOT NULL), 0) AS wins_in_fns,
               count(DISTINCT p.inn) FILTER ({large}) AS suppliers_large,
               count(*) FILTER ({large}) AS participations_large,
               coalesce(sum(win::INT) FILTER ({large}), 0) AS wins_large
        FROM p LEFT JOIN fns f USING (inn) {join} GROUP BY 1 ORDER BY 1"""
    cols = [d[0] for d in con.execute(q).description]
    groups = {r[0]: dict(zip(cols[1:], r[1:], strict=True)) for r in con.execute(q).fetchall()}
    total = con.execute("SELECT count(DISTINCT inn), count(*), sum(win::INT) FROM p").fetchone()
    home = " OR ".join(f"f.inn LIKE '{p}%'" for p in HOME)
    cand = con.execute(f"""
        SELECT count(*) FILTER ({large}), count(*) FILTER ({large} AND ({home})),
               count(*) FILTER ({large} AND ({home}) AND f.inn NOT IN (SELECT DISTINCT inn FROM p))
        FROM fns f""").fetchone()
    return {"total": dict(zip(("suppliers", "participations", "wins"), total, strict=True)), "groups": groups,
            "candidates": {"above_sme_thresholds": cand[0], "of_them_inn_78_47": cand[1],
                           "of_them_not_among_organizers_suppliers": cand[2]}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=ROOT / "data" / "external" / "fns_opendata")
    ap.add_argument("--processed", type=Path, default=ROOT / "data" / "processed")
    ap.add_argument("--msp", type=Path, default=None, help="parquet with the INNs of the SME registry (#185)")
    ap.add_argument("--msp-inn", default="inn", help="name of the INN column in --msp")
    args = ap.parse_args(argv)
    for name in ("sshr2019.zip", "revexp.zip"):
        if not (args.dir / name).exists() and not (args.dir / "fns_inns.parquet").exists():
            print(f"нет {args.dir / name}: скачать с https://www.nalog.gov.ru/opendata/7707329152-{name[:-4]}/",
                  file=sys.stderr)
            return 2
    fns, meta = parsed(args.dir)
    print(json.dumps({**meta, "organizers": coverage(args.processed, fns, args.msp, args.msp_inn)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
