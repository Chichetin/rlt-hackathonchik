"""Ingest parquet (D-026) → Postgres (D-054). DuckDB is the ETL engine (its `postgres` extension).

    DATABASE_URL=postgresql://app:app@127.0.0.1:5432/app uv run python -m src.db.load   # make db-load

Tables of data/processed are copied as is into schema `data_new`, indexed for the server-side
filters and pagination (D-024) and extended with `supplier_items` derived from them (D-135, the read path of
`class_history`); row counts are checked against the parquet files, then one transaction replaces schema `data`
with `data_new`. A rerun is idempotent and readers never see a half-loaded schema. Nothing outside `data` may
reference it: the swap drops it with CASCADE.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
TABLES = ("notices", "items", "participants", "suppliers")

# (index name, table, definition) — filters by date/customer/channel/OKPD2 prefix, lookups by lot and INN.
INDEXES = [
    ("notices_reqnum", "notices", "(reqnum)"),
    ("notices_publish_date", "notices", "(publish_date)"),
    ("notices_customer_inn", "notices", "(customer_inn)"),
    ("notices_channel_date", "notices", "(channel, publish_date)"),
    ("items_lot_id", "items", "(lot_id)"),
    ("items_okpd2", "items", "(okpd2_code text_pattern_ops)"),
    ("participants_lot_id", "participants", "(lot_id)"),
    ("participants_supplier_inn", "participants", "(supplier_inn, publish_date)"),
    ("participants_customer_inn", "participants", "(customer_inn)"),
    ("suppliers_inn", "suppliers", "(inn)"),
    ("supplier_items_inn", "supplier_items", "(supplier_inn, lot_id)"),
]

# (supplier, lot, OKPD2 code) of every participation, physically ordered by supplier: PgStore.class_history reads the
# candidates' own rows instead of joining each of their lots to `items` (D-135). A lot with several positions of one
# code is one row, so the class counts stay per lot.
SUPPLIER_ITEMS = """
CREATE TABLE {new}.supplier_items AS
SELECT DISTINCT p.supplier_inn, p.lot_id, p.publish_date, p.is_winner,
       i.okpd2_code, i.okpd2_l6, i.okpd2_l4, i.okpd2_l2
FROM {new}.participants p JOIN {new}.items i USING (lot_id)
ORDER BY p.supplier_inn, p.lot_id, i.okpd2_code
"""


def pg_dsn(url: str) -> str:
    """libpq URI from a SQLAlchemy URL (postgresql+psycopg://… in docker-compose)."""
    scheme, sep, rest = url.partition("://")
    return scheme.split("+", 1)[0] + sep + rest


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _tables(processed: Path, manifest: dict) -> dict[str, Path]:
    tables = {t: processed / f"{t}.parquet" for t in TABLES}
    if cutoff := manifest.get("cutoff"):
        tables[f"suppliers_{cutoff.replace('-', '_')}"] = processed / f"suppliers_{cutoff}.parquet"
    if (okpd2 := processed / "okpd2.parquet").exists():  # OKPD2 code → name (#154), make okpd2
        tables["okpd2"] = okpd2
    return tables


def load(processed: Path, url: str, schema: str = "data") -> dict[str, int]:
    """Load all tables into `schema`; returns row counts per table."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema) or schema == "public" or schema.startswith("pg_"):
        raise ValueError(f"недопустимая схема {schema!r}: нужна своя, не public (там очередь и pgvector)")
    manifest = json.loads((processed / "manifest.json").read_text(encoding="utf-8"))
    tables = _tables(processed, manifest)
    new = f"{schema}_new"
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres")
    con.execute(f"ATTACH {_lit(pg_dsn(url))} AS pg (TYPE postgres)")

    def pg(sql: str) -> None:
        con.execute(f"CALL postgres_execute('pg', {_lit(sql)})")

    pg(f"DROP SCHEMA IF EXISTS {new} CASCADE; CREATE SCHEMA {new}")
    counts: dict[str, int] = {}
    for name, path in tables.items():
        con.execute(f"CREATE TABLE pg.{new}.{name} AS SELECT * FROM read_parquet({_lit(path.as_posix())})")
        want = con.execute(f"SELECT count(*) FROM read_parquet({_lit(path.as_posix())})").fetchone()[0]  # type: ignore[index]
        got = con.execute(f"SELECT count(*) FROM pg.{new}.{name}").fetchone()[0]  # type: ignore[index]
        if got != want or (name in manifest["rows"] and got != manifest["rows"][name]):
            expected = manifest["rows"].get(name)
            raise RuntimeError(f"{name}: в Postgres {got} строк, в parquet {want}, в manifest {expected}")
        counts[name] = got
        log.info("%s: %d строк", name, got)

    pg(f"ALTER TABLE {new}.notices ADD CONSTRAINT notices_pkey PRIMARY KEY (lot_id)")
    pg(SUPPLIER_ITEMS.format(new=new))
    for idx, table, cols in INDEXES:
        pg(f"CREATE INDEX {idx} ON {new}.{table} {cols}")
    for name in tables:
        if name.startswith("suppliers_"):  # supplier stats as of the eval cutoff
            pg(f"CREATE INDEX {name}_inn ON {new}.{name} (inn)")
    pg(f"CREATE TABLE {new}.manifest (built_at timestamptz, body jsonb)")
    pg(f"INSERT INTO {new}.manifest VALUES ({_lit(manifest['built_at'])}, {_lit(json.dumps(manifest))})")
    for name in (*tables, "supplier_items"):
        pg(f"ANALYZE {new}.{name}")  # statistics survive the rename
    # DROP and RENAME run in one transaction (DuckDB's on the attached database): readers see the old or the new schema.
    pg(f"DROP SCHEMA IF EXISTS {schema} CASCADE; ALTER SCHEMA {new} RENAME TO {schema}")
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ingest parquet → Postgres (D-054)")
    default = Path(os.environ.get("DATA_PROCESSED_DIR") or ROOT / "data/processed")
    ap.add_argument("--processed", type=Path, default=default)
    ap.add_argument("--schema", default="data")
    args = ap.parse_args(argv)
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgres"):
        print("нужен DATABASE_URL=postgresql://… (docker compose up -d db)", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(json.dumps(load(args.processed, url, args.schema), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
