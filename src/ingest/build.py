"""Ingest: organizers' CSV -> data/processed/*.parquet (T-01, research/DATA_DICTIONARY.md).

    uv run python -m src.ingest.build [--raw-dir DIR] [--out-dir data/processed] [--as-of YYYY-MM-DD]

Outputs (schema — research/DATA_DICTIONARY.md, "Обработанные таблицы"):
  notices.parquet       one row per lot (Извещения), typed; channel "ЭМ" / "АИС ГЗ"
  participants.parquet  lot x supplier INN (Поставщики), deduplicated, denormalized with the
                        lot date / channel / customer so that search and eval need no join
  items.parquet         lot positions (ТРУ) without the 412 077 exact duplicate rows, OKPD2
                        prefixes of level 2 / 4 / 6 ("32", "32.50", "32.50.13")
  suppliers.parquet     aggregates per supplier INN over lots published strictly before as_of
                        (all data when as_of is not given); keys of Counterparty.stats
  suppliers_<date>.parquet  the same as of the split cutoff (configs/split.yaml) — for eval
  manifest.json         row counts, as_of, build time (fetched_at of data: Evidence)

Raw dir: --raw-dir, then env DATA_RAW_DIR, then data/raw (D-020). INN validity is a flag,
rows are never dropped for a bad INN: Counterparty with an invalid INN gets inn=None (load.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from src.ingest.profile import FILES, inn_valid_sql
from src.split_config import split_config

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "data" / "processed"

# Region code of Saint Petersburg: first two digits of INN / KPP (customers_spb).
SPB = "78"


def _q(path: Path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def _read_csv(path: Path) -> str:
    return f"read_csv('{_q(path)}', delim=';', header=true, quote='\"', escape='\"', all_varchar=true)"


def load_raw(con: duckdb.DuckDBPyConnection, raw: Path) -> None:
    """Typed tables n (notices), s (participants), t (items) from the three CSV files."""
    for fname in FILES.values():
        if not (raw / fname).exists():
            raise FileNotFoundError(f"missing {raw / fname} (set --raw-dir or DATA_RAW_DIR)")
    con.execute(
        f"""
        CREATE TABLE n AS SELECT * EXCLUDE (rn) FROM (
            SELECT
                TRY_CAST(lot_id AS BIGINT) AS lot_id,
                TRY_CAST(procedure_id AS BIGINT) AS procedure_id,
                TRY_CAST(publish_date AS DATE) AS publish_date,
                TRY_CAST(start_price AS DOUBLE) AS start_price,
                nullif(trim(reqnum), '') AS reqnum,
                nullif(trim(procedure_name), '') AS procedure_name,
                nullif(trim(subject), '') AS subject,
                TRY_CAST(is_smp AS BOOLEAN) AS is_smp,
                nullif(trim(customer_inn), '') AS customer_inn,
                nullif(trim(customer_kpp), '') AS customer_kpp,
                nullif(trim(is_eshop_or_aisgz), '') AS channel,
                row_number() OVER (PARTITION BY TRY_CAST(lot_id AS BIGINT)
                                   ORDER BY TRY_CAST(publish_date AS DATE)) AS rn
            FROM {_read_csv(raw / FILES["n"])}
        ) WHERE rn = 1 AND lot_id IS NOT NULL
        """
    )
    con.execute(
        f"""
        CREATE TABLE s AS SELECT
            TRY_CAST(lot_id AS BIGINT) AS lot_id,
            nullif(trim(supplier_inn), '') AS supplier_inn,
            nullif(trim(supplier_kpp), '') AS supplier_kpp,
            TRY_CAST(is_winner AS BOOLEAN) AS is_winner
        FROM {_read_csv(raw / FILES["s"])}
        """
    )
    con.execute(
        f"""
        CREATE TABLE t AS SELECT DISTINCT
            TRY_CAST(lot_id AS BIGINT) AS lot_id,
            nullif(trim(product_name), '') AS product_name,
            nullif(trim(okpd2_code), '') AS okpd2_code
        FROM {_read_csv(raw / FILES["t"])}
        """
    )


def build_tables(con: duckdb.DuckDBPyConnection) -> None:
    """notices / participants / items views over the typed raw tables."""
    con.execute(
        f"""
        CREATE TABLE notices AS SELECT *, {inn_valid_sql("customer_inn")} AS customer_inn_valid
        FROM n ORDER BY lot_id
        """
    )
    # 31 duplicate (lot, INN) pairs: one row, winner if any duplicate says so.
    con.execute(
        f"""
        CREATE TABLE participants AS
        SELECT p.lot_id, p.supplier_inn, p.supplier_kpp, p.is_winner,
               {inn_valid_sql("p.supplier_inn")} AS inn_valid,
               n.publish_date, n.channel, n.customer_inn, n.start_price
        FROM (
            SELECT lot_id, supplier_inn,
                   max(supplier_kpp) AS supplier_kpp,  -- deterministic for the 31 duplicates
                   coalesce(bool_or(is_winner), false) AS is_winner
            FROM s WHERE lot_id IS NOT NULL AND supplier_inn IS NOT NULL
            GROUP BY lot_id, supplier_inn
        ) p JOIN notices n USING (lot_id)
        ORDER BY p.lot_id, p.supplier_inn
        """
    )
    # OKPD2 "XX.XX.XX.XXX": level 2 = "XX", level 4 = "XX.XX", level 6 = "XX.XX.XX"; shorter codes
    # ("85.1", "21.20") get only the levels they have.
    con.execute(
        """
        CREATE TABLE items AS
        SELECT lot_id, product_name, okpd2_code,
               CASE WHEN length(okpd2_code) >= 2 THEN substr(okpd2_code, 1, 2) END AS okpd2_l2,
               CASE WHEN length(okpd2_code) >= 5 THEN substr(okpd2_code, 1, 5) END AS okpd2_l4,
               CASE WHEN length(okpd2_code) >= 8 THEN substr(okpd2_code, 1, 8) END AS okpd2_l6
        FROM t WHERE lot_id IS NOT NULL
        ORDER BY lot_id, okpd2_code, product_name
        """
    )


def build_suppliers(con: duckdb.DuckDBPyConnection, as_of: date | None, table: str = "suppliers") -> None:
    """Aggregates per supplier INN over lots published strictly before as_of (no leak, D-018).

    Columns are the keys of Counterparty.stats: lots_participated, lots_won, win_rate, customers,
    customers_spb, first_seen, last_seen, median_start_price, top_okpd2 (3 most frequent level-4
    groups, comma-separated), plus kpp (most frequent) and inn_valid, and the profile keys
    (D-064, research/DATA_DICTIONARY.md): assortment, scale, customers, activity, channels, legal.
    """
    cond = f"publish_date < DATE '{as_of.isoformat()}'" if as_of else "TRUE"
    # End of the history: lots_last_90d counts lots in [end - 90 days, end).
    end = f"DATE '{as_of.isoformat()}'" if as_of else "(SELECT max(publish_date) + 1 FROM p)"
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(
        f"""
        CREATE TABLE {table} AS
        WITH p AS (SELECT * FROM participants WHERE {cond}),
        groups AS (
            SELECT p.supplier_inn, i.okpd2_l4, count(DISTINCT p.lot_id) AS n
            FROM p JOIN items i USING (lot_id)
            WHERE i.okpd2_l4 IS NOT NULL
            GROUP BY ALL
        ),
        top AS (
            SELECT supplier_inn,
                   string_agg(okpd2_l4, ',' ORDER BY n DESC, okpd2_l4) AS top_okpd2
            FROM (
                SELECT *, row_number() OVER (PARTITION BY supplier_inn ORDER BY n DESC, okpd2_l4) AS r
                FROM groups
            ) WHERE r <= 3
            GROUP BY supplier_inn
        ),
        kpp AS (  -- most frequent KPP, ties broken by the KPP itself: rebuilds are identical
            SELECT supplier_inn AS inn, arg_max(supplier_kpp, (n, supplier_kpp)) AS kpp
            FROM (SELECT supplier_inn, supplier_kpp, count(*) AS n FROM p
                  WHERE supplier_kpp IS NOT NULL GROUP BY ALL)
            GROUP BY supplier_inn
        ),
        lot_n AS (SELECT lot_id, count(*) AS n FROM p GROUP BY lot_id),
        lot_items AS (
            SELECT lot_id, count(*) AS n_items FROM items GROUP BY lot_id
        ),
        cls AS (  -- lots per (supplier, level-2 class)
            SELECT p.supplier_inn, i.okpd2_l2, count(DISTINCT p.lot_id) AS n
            FROM p JOIN items i USING (lot_id) WHERE i.okpd2_l2 IS NOT NULL GROUP BY ALL
        ),
        cls_agg AS (
            SELECT supplier_inn, count(*) AS okpd2_classes, max(n) AS top_n,
                   arg_max(okpd2_l2, (n, okpd2_l2)) AS top_class FROM cls GROUP BY supplier_inn
        ),
        cls_lots AS (
            SELECT p.supplier_inn, count(DISTINCT p.lot_id) AS n
            FROM p JOIN items i USING (lot_id) WHERE i.okpd2_l2 IS NOT NULL GROUP BY p.supplier_inn
        ),
        grp_agg AS (  -- normalized Shannon entropy over level-4 groups: 0 for one group, 1 for even
            SELECT supplier_inn, count(*) AS okpd2_groups,
                   CASE WHEN count(*) > 1 THEN -sum(share * ln(share)) / ln(count(*)) ELSE 0 END
                   AS assortment_breadth
            FROM (SELECT supplier_inn, n / sum(n) OVER (PARTITION BY supplier_inn) AS share FROM groups)
            GROUP BY supplier_inn
        ),
        codes AS (
            SELECT p.supplier_inn, count(DISTINCT i.okpd2_code) AS distinct_codes
            FROM p JOIN items i USING (lot_id) WHERE i.okpd2_code IS NOT NULL GROUP BY p.supplier_inn
        ),
        cust AS (
            SELECT supplier_inn, max(n) AS top_n, sum(n) AS total
            FROM (SELECT supplier_inn, customer_inn, count(*) AS n FROM p
                  WHERE customer_inn IS NOT NULL GROUP BY ALL) GROUP BY supplier_inn
        ),
        repeat AS (  -- a win is a repeat when the same customer already gave him a win earlier
            SELECT supplier_inn, count(*) FILTER (WHERE r > 1) AS rep, count(*) AS wins
            FROM (SELECT supplier_inn, row_number() OVER (
                      PARTITION BY supplier_inn, customer_inn ORDER BY publish_date, lot_id) AS r
                  FROM p WHERE is_winner AND customer_inn IS NOT NULL)
            GROUP BY supplier_inn
        ),
        per_lot AS (
            SELECT p.supplier_inn, p.channel, n.is_smp, p.is_winner, ln.n - 1 AS rivals, li.n_items
            FROM p JOIN notices n USING (lot_id) JOIN lot_n ln USING (lot_id)
            LEFT JOIN lot_items li USING (lot_id)
        ),
        lot_agg AS (
            SELECT supplier_inn,
                   median(n_items) AS items_per_lot,
                   avg(rivals) FILTER (WHERE channel = 'ЭМ') AS avg_rivals,
                   count(*) FILTER (WHERE is_winner AND is_smp) AS smp_contracts
            FROM per_lot GROUP BY supplier_inn
        ),
        prof AS (
            SELECT l.supplier_inn,
                   c.okpd2_classes, c.top_class, g.okpd2_groups,
                   round(c.top_n / cl.n, 4) AS top_class_share,
                   round(g.assortment_breadth, 4) AS assortment_breadth,
                   cd.distinct_codes, l.items_per_lot,
                   round(cu.top_n / cu.total, 4) AS top_customer_share,
                   round(r.rep / r.wins, 4) AS repeat_win_share,
                   round(l.avg_rivals, 2) AS avg_rivals, l.smp_contracts
            FROM lot_agg l
            LEFT JOIN cls_agg c USING (supplier_inn) LEFT JOIN cls_lots cl USING (supplier_inn)
            LEFT JOIN grp_agg g USING (supplier_inn) LEFT JOIN codes cd USING (supplier_inn)
            LEFT JOIN cust cu USING (supplier_inn) LEFT JOIN repeat r USING (supplier_inn)
        )
        SELECT a.*, top.top_okpd2, prof.* EXCLUDE (supplier_inn)
        FROM (
            SELECT supplier_inn AS inn,
                   kpp.kpp,
                   bool_and(inn_valid) AS inn_valid,
                   count(DISTINCT lot_id) AS lots_participated,
                   count(DISTINCT lot_id) FILTER (WHERE is_winner) AS lots_won,
                   round(count(DISTINCT lot_id) FILTER (WHERE is_winner)
                         / count(DISTINCT lot_id), 4) AS win_rate,
                   count(DISTINCT customer_inn) AS customers,
                   count(DISTINCT customer_inn) FILTER (WHERE customer_inn LIKE '{SPB}%') AS customers_spb,
                   min(publish_date) AS first_seen,
                   max(publish_date) AS last_seen,
                   median(start_price) AS median_start_price,
                   quantile_cont(start_price, 0.9) AS p90_start_price,
                   count(DISTINCT date_trunc('month', publish_date)) AS active_months,
                   count(DISTINCT lot_id) FILTER (WHERE publish_date >= {end} - 90) AS lots_last_90d,
                   count(DISTINCT lot_id) FILTER (WHERE channel = 'ЭМ') AS em_offers,
                   count(DISTINCT lot_id) FILTER (WHERE channel = 'ЭМ' AND is_winner) AS em_wins,
                   count(DISTINCT lot_id) FILTER (WHERE channel = 'АИС ГЗ' AND is_winner) AS aisgz_contracts,
                   CASE length(supplier_inn) WHEN 10 THEN 'ЮЛ' WHEN 12 THEN 'ИП' END AS entity_type,
                   count(DISTINCT supplier_kpp) AS kpp_count
            FROM p LEFT JOIN kpp ON kpp.inn = p.supplier_inn GROUP BY supplier_inn, kpp.kpp
        ) a LEFT JOIN top ON top.supplier_inn = a.inn
        LEFT JOIN prof ON prof.supplier_inn = a.inn
        ORDER BY a.lots_participated DESC, a.inn
        """
    )


def build(raw: Path, out: Path, as_of: date | None = None, cutoff: date | None = None) -> dict:
    """Write all parquet files and manifest.json; returns the manifest."""
    t0 = time.time()
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    load_raw(con, raw)
    build_tables(con)
    build_suppliers(con, as_of)
    tables = ["notices", "participants", "items", "suppliers"]
    if cutoff:
        name = f"suppliers_{cutoff.isoformat()}"
        build_suppliers(con, cutoff, table="suppliers_cutoff")
        con.execute(f"COPY suppliers_cutoff TO '{_q(out / name)}.parquet' (FORMAT parquet)")
    for tbl in tables:
        con.execute(f"COPY {tbl} TO '{_q(out / tbl)}.parquet' (FORMAT parquet)")

    def count(sql: str) -> int:
        return con.sql(sql).fetchone()[0]  # type: ignore[index]

    manifest = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "raw_dir": str(raw),
        "as_of": as_of.isoformat() if as_of else None,
        "cutoff": cutoff.isoformat() if cutoff else None,
        "rows": {tbl: count(f"SELECT count(*) FROM {tbl}") for tbl in tables},
        "invalid_supplier_inns": count("SELECT count(*) FROM suppliers WHERE NOT inn_valid"),
        "seconds": round(time.time() - t0, 1),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    con.close()
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", type=Path, default=Path(os.environ.get("DATA_RAW_DIR") or ROOT / "data" / "raw"))
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--as-of", type=date.fromisoformat, default=None, help="history strictly before this date")
    ap.add_argument("--no-cutoff", action="store_true", help="skip suppliers_<cutoff>.parquet")
    args = ap.parse_args(argv)
    cutoff = None if args.no_cutoff else date.fromisoformat(str(split_config()["cutoff"]))
    try:
        manifest = build(args.raw_dir, args.out_dir, args.as_of, cutoff)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
