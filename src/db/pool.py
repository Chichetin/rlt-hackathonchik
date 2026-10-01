"""Pool of new companies: the full FNS MSP registry dump → parquet → Postgres schema `pool` (#185, D-084).

    uv run python -m src.db.pool build    # data/external/rsmp/parts → data/external/rsmp/pool/*.parquet
    DATABASE_URL=postgresql://app:app@127.0.0.1:5432/app uv run python -m src.db.pool load   # → schema pool
    uv run python -m src.db.pool stats    # totals, new companies, coverage of organizers' suppliers
    make pool-load                        # all three steps (the dump parse first, resumable)

Tables (the same in parquet and in Postgres):
- `companies` — one record per INN (the latest `ДатаСост` when the dump repeats an INN), every column as in
  src/sources/msp_dump.py, ИП without locality, role by the main OKVED (`kind`, `kind_why`, src/enrich/role.py),
  `fetched_at` of the dump download; sorted by INN;
- `okved` — (code, inn, main, region_code): the main and every additional OKVED of a company, one row per
  (inn, code); the candidate index by an OKVED prefix (human decision 01.10: an additional OKVED counts too);
- `company_class` — OKPD2 codes the company declared as produced (`СвПрод`, ~0.15 % of records).

Facts: every shown value of a company is an Evidence built from its row by `msp_dump.counterparty()` and
checked against the page `msp_dump.page()` that `counterparties()` puts into src/sources/cache.py. The
Evidence rows are derived, not stored: one per value, ~80 M rows would be a copy of `companies` (D-084).

Postgres: loaded into `<schema>_new`, counted against the manifest, indexed, then swapped in one
transaction like src/db/load.py. Schema `pool` is outside `data` and `vec`: `make db-load` does not drop
it and this loader drops nothing else. Without Postgres (`make demo`) the same lookups read the parquet.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from src.contracts import Counterparty
from src.db.load import pg_dsn
from src.enrich import role
from src.sources import msp_dump
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
POOL = ROOT / "data" / "external" / "rsmp" / "pool"
TABLES = ("companies", "okved", "company_class")
FOREIGN = {"public", "data", "vec"}  # schemas of other loaders: never written or dropped here
_PREFIX = re.compile(r"\d{2}(?:\.\d{1,2}){0,2}", re.ASCII)
_INN = re.compile(r"\d{10}|\d{12}", re.ASCII)

INDEXES = [
    ("companies_region", "companies", "(region_code)"),
    ("companies_okved_main", "companies", "(okved_main text_pattern_ops)"),
    ("okved_code", "okved", "(code text_pattern_ops, region_code) INCLUDE (inn, main)"),  # index-only scan
    ("okved_inn", "okved", "(inn)"),
    ("company_class_inn", "company_class", "(inn)"),
    ("company_class_okpd2", "company_class", "(okpd2 text_pattern_ops)"),
]


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _copy(con: duckdb.DuckDBPyConnection, query: str, path: Path) -> int:
    """Write a query to parquet atomically: a table either exists whole or keeps its previous version."""
    tmp = path.with_suffix(".parquet.tmp")
    con.execute(f"COPY ({query}) TO {_lit(tmp.as_posix())} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)")
    tmp.replace(path)
    return con.execute(f"SELECT count(*) FROM read_parquet({_lit(path.as_posix())})").fetchone()[0]  # type: ignore[index]


def build(parts: Path, out: Path, *, fetched_at: float, memory: str = "2GB", threads: int = 4) -> dict[str, int]:
    """Parts of the dump → the three pool tables + manifest.json in `out`; a rerun rewrites them."""
    src = _lit((parts / "*.parquet").as_posix())
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    # sorting 6.8 M rows with lists: bounded memory and threads, spill to disk (4 GB × all cores was OOM-killed)
    con.execute(f"SET memory_limit = {_lit(memory)}; SET threads = {int(threads)}; "
                "SET preserve_insertion_order = false; "
                f"SET temp_directory = {_lit((out / '.tmp').as_posix())}")
    codes = [r[0] for r in con.execute(f"SELECT DISTINCT okved_main FROM read_parquet({src}) "
                                       "WHERE okved_main IS NOT NULL").fetchall()]
    roles = {c: k for c in codes if (k := role.classify(c))}
    kinds = pa.table({"code": list(roles), "kind": [k for k, _ in roles.values()],
                      "why": [w for _, w in roles.values()]})
    con.register("kinds", kinds)
    records = con.execute(f"SELECT count(*) FROM read_parquet({src})").fetchone()[0]  # type: ignore[index]
    # an INN repeated in the dump (393 of 6.8 M): keep its latest state; only those rows go through the window
    con.execute(f"""CREATE TEMP TABLE stale AS SELECT src_file, doc_id FROM read_parquet({src})
                    WHERE inn IN (SELECT inn FROM read_parquet({src}) GROUP BY inn HAVING count(*) > 1)
                    QUALIFY row_number() OVER (PARTITION BY inn
                                               ORDER BY state_date DESC, src_file DESC, doc_id DESC) > 1""")
    companies = (out / "companies.parquet").as_posix()
    rows = {
        "companies": _copy(con, f"""
            SELECT p.* REPLACE (CASE WHEN p.entity = 'ЮЛ' THEN p.city END AS city),
                   k.kind, k.why AS kind_why, {fetched_at!r}::DOUBLE AS fetched_at
            FROM read_parquet({src}) p
            ANTI JOIN stale s ON s.src_file = p.src_file AND s.doc_id = p.doc_id
            LEFT JOIN kinds k ON k.code = p.okved_main
            ORDER BY p.inn""", out / "companies.parquet"),
        "okved": _copy(con, f"""
            SELECT code, inn, main, region_code FROM (
                SELECT inn, okved_main AS code, true AS main, region_code FROM read_parquet({_lit(companies)})
                UNION ALL  -- one INN per row after the dedup above: a repeat can only be inside one record
                SELECT inn, unnest(list_distinct(list_filter(okved_extra, x -> x IS DISTINCT FROM okved_main)))
                       AS code, false AS main, region_code FROM read_parquet({_lit(companies)})
            ) WHERE code IS NOT NULL AND code <> ''""", out / "okved.parquet"),  # 79 M rows: a sort does not fit 2 GB
        "company_class": _copy(con, f"""
            SELECT DISTINCT inn, left(code, 2) AS okpd2_class, code AS okpd2, name, 'СвПрод' AS method FROM (
                SELECT inn, unnest(products) AS code, unnest(product_names) AS name FROM read_parquet({_lit(companies)})
            ) WHERE code <> '' ORDER BY inn, okpd2""", out / "company_class.parquet"),
    }
    manifest = {"built_at": datetime.now(UTC).isoformat(timespec="seconds"), "dataset": msp_dump.DATASET_URL,
                "as_of": msp_dump.AS_OF, "fetched_at": fetched_at, "source_records": records, "rows": rows}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("pool: %s", rows)
    return rows


def _check_schema(schema: str) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema) or schema in FOREIGN or schema.startswith("pg_"):
        raise ValueError(f"недопустимая схема {schema!r}: нужна своя "
                         "(data — make db-load, vec — векторы, public — очередь)")


def load(pool_dir: Path, url: str, schema: str = "pool") -> dict[str, int]:
    """Parquet tables of `pool_dir` → `schema` (swap of `<schema>_new`); returns row counts."""
    _check_schema(schema)
    manifest = json.loads((pool_dir / "manifest.json").read_text(encoding="utf-8"))
    new = f"{schema}_new"
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres")
    con.execute(f"ATTACH {_lit(pg_dsn(url))} AS pg (TYPE postgres)")

    def pg(sql: str) -> None:
        con.execute(f"CALL postgres_execute('pg', {_lit(sql)})")

    pg(f"DROP SCHEMA IF EXISTS {new} CASCADE; CREATE SCHEMA {new}")
    counts: dict[str, int] = {}
    for name in TABLES:
        path = _lit((pool_dir / f"{name}.parquet").as_posix())
        started = time.monotonic()
        con.execute(f"CREATE TABLE pg.{new}.{name} AS SELECT * FROM read_parquet({path})")
        got = con.execute(f"SELECT count(*) FROM pg.{new}.{name}").fetchone()[0]  # type: ignore[index]
        if got != manifest["rows"][name]:
            raise RuntimeError(f"{name}: в Postgres {got} строк, в manifest {manifest['rows'][name]}")
        counts[name] = got
        log.info("%s: %d строк за %.0f с", name, got, time.monotonic() - started)
    pg(f"ALTER TABLE {new}.companies ADD CONSTRAINT companies_pkey PRIMARY KEY (inn)")
    for idx, table, cols in INDEXES:
        started = time.monotonic()
        pg(f"CREATE INDEX {idx} ON {new}.{table} {cols}")
        log.info("индекс %s за %.0f с", idx, time.monotonic() - started)
    pg(f"CREATE TABLE {new}.manifest (built_at timestamptz, body jsonb)")
    pg(f"INSERT INTO {new}.manifest VALUES ({_lit(manifest['built_at'])}, {_lit(json.dumps(manifest))})")
    for name in TABLES:
        pg(f"ANALYZE {new}.{name}")
    pg(f"DROP SCHEMA IF EXISTS {schema} CASCADE; ALTER SCHEMA {new} RENAME TO {schema}")
    import psycopg
    from psycopg import sql

    with psycopg.connect(pg_dsn(url), autocommit=True) as conn:  # VACUUM runs outside a transaction
        # visibility map: candidates() never touch the heap
        conn.execute(sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(schema, "okved")))
    return counts


def _pg_rows(url: str, query: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    try:
        with psycopg.connect(pg_dsn(url)) as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(query, params).fetchall()  # type: ignore[arg-type]
    except psycopg.errors.UndefinedTable:
        log.warning("пул в Postgres не загружен (make pool-load) — кандидатов из реестра МСП нет")
        return []


def _parquet(pool_dir: Path, name: str) -> str | None:
    path = pool_dir / f"{name}.parquet"
    if not path.exists():
        log.warning("нет %s (make pool-load) — кандидатов из реестра МСП нет", path)
        return None
    return _lit(path.as_posix())


def lookup(inns: Iterable[str], *, url: str | None = None, schema: str = "pool",
           pool_dir: Path = POOL) -> dict[str, dict[str, Any]]:
    """Company rows by INN (Postgres when `url` is given, else parquet); INNs not in the pool are absent."""
    want = sorted({i for i in inns if _INN.fullmatch(i)})
    if not want:
        return {}
    if url:
        _check_schema(schema)
        rows = _pg_rows(url, f"SELECT * FROM {schema}.companies WHERE inn = ANY(%s)", [want])
    else:
        if (path := _parquet(pool_dir, "companies")) is None:
            return {}
        # a literal IN list (INNs are checked digits): DuckDB prunes row groups of the INN-sorted file
        cur = duckdb.connect().execute(f"SELECT * FROM read_parquet({path}) WHERE inn IN ({','.join(map(_lit, want))})")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    return {r["inn"]: r for r in rows}


def candidates(prefixes: Sequence[str], *, regions: Sequence[str] | None = None, limit: int | None = None,
               url: str | None = None, schema: str = "pool", pool_dir: Path = POOL) -> list[dict[str, Any]]:
    """Companies with a main or additional OKVED under one of `prefixes`, one row per INN — its best match
    (main before additional, then the longest code); main first, then by INN. Ranking is not done here."""
    for p in prefixes:
        if not _PREFIX.fullmatch(p):
            raise ValueError(f"не префикс ОКВЭД: {p!r}")
    for r in regions or ():
        if not re.fullmatch(r"\d{2}", r, re.ASCII):
            raise ValueError(f"не код региона: {r!r}")
    if not prefixes:
        return []
    like = " OR ".join(f"code LIKE {_lit(p + '%')}" for p in prefixes)  # checked: digits and dots only
    where = f"({like})" + (f" AND region_code IN ({','.join(map(_lit, regions))})" if regions else "")
    tail = " ORDER BY main DESC, inn" + (f" LIMIT {int(limit)}" if limit is not None else "")
    best = ("SELECT inn, code, main FROM (SELECT inn, code, main, row_number() OVER "
            "(PARTITION BY inn ORDER BY main DESC, length(code) DESC, code) AS rn FROM {t} WHERE {w}) b WHERE rn = 1")
    if url:
        _check_schema(schema)
        return _pg_rows(url, best.format(t=f"{schema}.okved", w=where.replace("%", "%%")) + tail, [])
    if (path := _parquet(pool_dir, "okved")) is None:
        return []
    cur = duckdb.connect().execute(best.format(t=f"read_parquet({path})", w=where) + tail)
    return [{"inn": i, "code": c, "main": m} for i, c, m in cur.fetchall()]


def counterparties(inns: Iterable[str], cache: SourceCache, *, url: str | None = None, schema: str = "pool",
                   pool_dir: Path = POOL) -> dict[str, Counterparty]:
    """Counterparties of the pool with every fact evidenced; their pages go to the source cache, so
    src/enrich/evidence.py checks them like any other source. Bad-checksum INNs give no company."""
    out: dict[str, Counterparty] = {}
    for inn, row in lookup(inns, url=url, schema=schema, pool_dir=pool_dir).items():
        cp = msp_dump.counterparty(row, datetime.fromtimestamp(row["fetched_at"], UTC))
        if cp is None:
            continue
        body, page_url = msp_dump.page(row), msp_dump.page_url(inn)
        if cache.page_text(page_url) != body:
            cache.put(page_url, body, fetched_at=row["fetched_at"])
        out[inn] = cp
    return out


def stats(pool_dir: Path, suppliers: Path) -> dict[str, Any]:
    """Pool totals, companies absent from organizers' data, and coverage of organizers' suppliers."""
    c, s = _lit((pool_dir / "companies.parquet").as_posix()), _lit(suppliers.as_posix())
    con = duckdb.connect()
    cur = con.execute(f"""
        SELECT count(*) AS companies, count(*) FILTER (WHERE entity = 'ЮЛ') AS ul,
               count(*) FILTER (WHERE region_code = '78') AS spb, count(*) FILTER (WHERE region_code = '47') AS lo,
               count(*) FILTER (WHERE NOT inn_valid) AS bad_inn,
               count(*) FILTER (WHERE okved_main IS NULL) AS no_main_okved,
               count(*) FILTER (WHERE len(okved_extra) > 0) AS with_okved_extra,
               count(*) FILTER (WHERE len(products) > 0) AS with_products,
               count(*) FILTER (WHERE len(licences) > 0) AS with_licences,
               count(*) FILTER (WHERE kind = 'manufacturer') AS manufacturers,
               count(*) FILTER (WHERE kind = 'distributor') AS distributors,
               count(*) FILTER (WHERE kind = 'dealer') AS dealers,
               count(*) FILTER (WHERE inn NOT IN (SELECT inn FROM read_parquet({s}) WHERE inn IS NOT NULL))
                   AS not_in_suppliers,
               count(*) FILTER (WHERE region_code IN ('78', '47')
                                AND inn NOT IN (SELECT inn FROM read_parquet({s}) WHERE inn IS NOT NULL))
                   AS not_in_suppliers_spb_lo
        FROM read_parquet({c})""")
    res: dict[str, Any] = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))  # type: ignore[arg-type]
    cur = con.execute(f"""
        SELECT count(*) AS suppliers_valid_inn, count(p.inn) AS suppliers_in_pool,
               sum(lots_participated) AS participations,
               sum(lots_participated) FILTER (WHERE p.inn IS NOT NULL) AS participations_in_pool,
               sum(lots_won) AS wins, sum(lots_won) FILTER (WHERE p.inn IS NOT NULL) AS wins_in_pool
        FROM read_parquet({s}) x LEFT JOIN (SELECT inn FROM read_parquet({c})) p ON p.inn = x.inn
        WHERE x.inn_valid""")
    res |= {k: int(v or 0) for k, v in zip([d[0] for d in cur.description], cur.fetchone(), strict=True)}  # type: ignore[arg-type]
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MSP registry dump → pool (parquet, Postgres schema pool), #185")
    ap.add_argument("command", choices=("build", "load", "stats"))
    ap.add_argument("--parts", type=Path, default=msp_dump.OUT)
    ap.add_argument("--pool", type=Path, default=POOL)
    ap.add_argument("--schema", default="pool")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.command == "build":
        if not msp_dump.ZIP.exists():  # its mtime is when the facts were fetched
            print(f"нет {msp_dump.ZIP}: сначала python -m src.sources.msp_dump", file=sys.stderr)
            return 2
        result: dict[str, Any] = build(args.parts, args.pool, fetched_at=msp_dump.ZIP.stat().st_mtime)
    elif args.command == "load":
        url = os.environ.get("DATABASE_URL", "")
        if not url.startswith("postgres"):
            print("нужен DATABASE_URL=postgresql://… (docker compose up -d db)", file=sys.stderr)
            return 2
        result = load(args.pool, url, args.schema)
    else:
        result = stats(args.pool, ROOT / "data" / "processed" / "suppliers.parquet")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
