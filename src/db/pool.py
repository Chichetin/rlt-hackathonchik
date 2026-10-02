"""Pool of new companies: the full FNS MSP registry dump → parquet → Postgres schema `pool` (#185, D-090).

    uv run python -m src.db.pool build    # data/external/rsmp/parts → data/external/rsmp/pool/*.parquet
    DATABASE_URL=postgresql://app:app@127.0.0.1:5432/app uv run python -m src.db.pool load   # → schema pool
    uv run python -m src.db.pool stats    # totals, new companies, coverage of organizers' suppliers
    make pool-load                        # all three steps (the dump parse first, resumable)

Tables (the same in parquet and in Postgres):
- `companies` — one record per INN (the latest `ДатаСост` when the dump repeats an INN), every column as in
  src/sources/msp_dump.py, ИП without locality, role by the main OKVED (`kind`, `kind_why`, src/sources/role.py),
  `fetched_at` of the dump download; sorted by INN;
- `okved` — (code, inn, main, region_code): the main and every additional OKVED of a company, one row per
  (inn, code); the candidate index by an OKVED prefix (human decision 01.10: an additional OKVED counts too);
- `company_class` — OKPD2 codes the company declared as produced (`СвПрод`, ~0.15 % of records).
- `large` — companies that are NOT in the registry dump (#219): a row per INN that ГИР БО or the registry card of
  pb.nalog.ru names, with the cached page of that source (scripts/large_pool.py build). Parquet only, also when
  the rest is read from Postgres: about a thousand rows next to 6.8 M. `large_candidates()` and `large_pages()`
  read it; the Evidence is built from the page by the same code as for a live answer (src/enrich/enrich.py).

Facts: every shown value of a company is an Evidence built from its row by `msp_dump.counterparty()` and
checked against the page `msp_dump.page()` that `counterparties()` puts into src/sources/cache.py. The
Evidence rows are derived, not stored: one per value, ~80 M rows would be a copy of `companies` (D-090).

Postgres: loaded into `<schema>_new`, counted against the manifest, indexed, then swapped in one
transaction like src/db/load.py. Schema `pool` is outside `data` and `vec`: `make db-load` does not drop
it and this loader drops nothing else. Without Postgres (`make demo`) the same lookups read the parquet.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from src.contracts import Counterparty, inn_is_valid
from src.db.load import pg_dsn
from src.regions import REGIONS
from src.settings import get_settings
from src.sources import bo_nalog, msp_dump, pb_nalog, role
from src.sources.cache import CachedPage, SourceCache

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
POOL = ROOT / "data" / "external" / "rsmp" / "pool"
TABLES = ("companies", "okved", "company_class")
LARGE = "large"  # companies outside the registry dump (#219): its own parquet, not a table of the dump
LARGE_SCHEMA = pa.schema([
    ("inn", pa.string()), ("ogrn", pa.string()), ("name", pa.string()), ("okved_main", pa.string()),
    ("region_code", pa.string()), ("status", pa.string()), ("source", pa.string()),  # 'bo' | 'pb'
    ("url", pa.string()), ("fetched_at", pa.float64()), ("body", pa.string()),  # the cached page of the source
    ("msp_checked_at", pa.float64()),  # download time of the registry dump that has no record of this INN
    # where the INN came from (scripts/large_pool.py): 'gap' — a winner of the organizers' data, picked by its
    # wins over the whole period (never offered as new: that would leak the future into a backtest);
    # 'candidates' — a large company that is not among the organizers' suppliers
    ("list", pa.string()),
])
# ГИР БО names a region by one upper-case word («САНКТ-ПЕТЕРБУРГ», «ЛЕНИНГРАДСКАЯ»); only the regions of
# src/regions.py get a code, any other company stays without one and is not offered for a region
_BO_REGION = {r.name.split()[0].upper(): r.code for r in REGIONS}
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


def large_row(inn: str, source: str, page: CachedPage, msp_checked_at: float | None = None,
              listed: str = "candidates") -> dict[str, Any] | None:
    """A row of `large` from the cached answer of ГИР БО ('bo') or the registry card ('pb'): only what the
    page says. None when the INN fails the checksum or the page does not name the company.
    `msp_checked_at`: the caller looked the INN up in the registry dump downloaded then and found no record."""
    if not inn_is_valid(inn) or source not in ("bo", "pb"):
        return None
    if source == "bo":
        f = bo_nalog.parse(page.body)
        name, ogrn, okved, status = f.get("shortName"), f.get("ogrn"), f.get("okved2"), f.get("statusCode")
        region = _BO_REGION.get((f.get("region") or "").upper())
    else:
        f = pb_nalog.parse(page.body)
        name = f.get("vyp.НаимЮЛСокр") or f.get("search.namec") or f.get("vyp.НаимЮЛПолн") or f.get("search.namep")
        ogrn = f.get("vyp.ОГРН") or f.get("search.ogrn")
        okved = f.get("vyp.КодОКВЭД") or f.get("search.okved2main")
        status = f.get("vyp.sulst_name_ex") or f.get("search.sulst_name_ex")
        region = (f.get("vyp.КПП") or "")[:2] or None
    if not name:
        return None
    return {"inn": inn, "ogrn": ogrn, "name": name, "okved_main": okved, "region_code": region, "status": status,
            "source": source, "url": page.url, "fetched_at": page.fetched_at, "body": page.body,
            "msp_checked_at": msp_checked_at, "list": listed}


def large_active(row: dict[str, Any]) -> bool:
    """The source calls the company active — the Python twin of LARGE_ACTIVE."""
    status = row["status"] or ""
    return status == "ACTIVE" if row["source"] == "bo" else status.lower().startswith("действующ")


LARGE_ACTIVE = "((source = 'bo' AND status = 'ACTIVE') OR (source = 'pb' AND status ILIKE 'действующ%'))"


def msp_absent_page(inn: str) -> str:
    """The page «the registry dump has no record of this INN», in the words of rmsp's empty answer."""
    return f"query.inn: {inn}\nanswer.inn_rows: 0"


def _large(pool_dir: Path, where: str) -> list[dict[str, Any]]:
    path = pool_dir / f"{LARGE}.parquet"
    if not path.exists():  # the table is optional: without it the pool is the registry dump alone
        return []
    cur = duckdb.connect().execute(f"SELECT * FROM read_parquet({_lit(path.as_posix())}) WHERE {where} ORDER BY inn")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def large_candidates(prefixes: Sequence[str], *, regions: Sequence[str] | None = None,
                     pool_dir: Path = POOL) -> list[dict[str, Any]]:
    """Companies outside the registry dump whose main OKVED is under one of `prefixes` — the rows of
    `candidates()` (inn, code, main; the sources give the main code only), ordered by INN. Only the list
    'candidates' (see LARGE_SCHEMA) and only companies the source calls active (ГИР БО `ACTIVE`, the card
    «Действующая…»): one in liquidation is not offered as new."""
    for p in prefixes:
        if not _PREFIX.fullmatch(p):
            raise ValueError(f"не префикс ОКВЭД: {p!r}")
    for r in regions or ():
        if not re.fullmatch(r"\d{2}", r, re.ASCII):
            raise ValueError(f"не код региона: {r!r}")
    if not prefixes:
        return []
    like = " OR ".join(f"okved_main LIKE {_lit(p + '%')}" for p in prefixes)  # checked: digits and dots only
    where = f"({like}) AND list = 'candidates' AND {LARGE_ACTIVE}" + (
        f" AND region_code IN ({','.join(map(_lit, regions))})" if regions else "")
    return [{"inn": r["inn"], "code": r["okved_main"], "main": True} for r in _large(pool_dir, where)]


def large_pages(inns: Iterable[str], cache: SourceCache, *, pool_dir: Path = POOL) -> dict[str, dict[str, Any]]:
    """Rows of `large` by INN; the page of each goes to the source cache, so the Evidence built from
    `row["body"]` is checked like a live answer and no request is needed. A newer answer of the source in the
    cache wins: the whole row is read from it again (an answer that no longer names the company — no row).
    «No record in the registry dump» is a page too (as rmsp's empty answer): our render of the lookup made by
    scripts/large_pool.py build. It is never written over a record of the dump under the same address — then
    the row comes back without `msp_checked_at`, and nobody calls the company «не МСП»."""
    want = sorted({i for i in inns if _INN.fullmatch(i)})
    if not want:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in _large(pool_dir, f"inn IN ({','.join(map(_lit, want))})"):
        inn = row["inn"]
        hit = cache.get(row["url"], allow_stale=True)
        if hit is None or hit.fetched_at < row["fetched_at"]:
            cache.put(row["url"], row["body"], fetched_at=row["fetched_at"])
        elif hit.body != row["body"]:
            fresh = large_row(inn, row["source"], hit, row["msp_checked_at"], row["list"])
            if fresh is None:
                continue
            row = fresh
        if row["msp_checked_at"] is not None:
            absent, body = msp_dump.page_url(inn), msp_absent_page(inn)
            if cache.page_text(absent) is None:
                cache.put(absent, body, fetched_at=row["msp_checked_at"])
            elif cache.page_text(absent) != body:
                row = {**row, "msp_checked_at": None}
        out[inn] = row
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
        url = get_settings().database_url
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
