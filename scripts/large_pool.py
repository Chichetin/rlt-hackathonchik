"""Companies outside the SME registry → the pool of «Новые»: ГИР БО, then ЕГРЮЛ, by INN (#219).

    uv run python scripts/large_pool.py collect [--top 100] [--only gap|candidates] [--rounds 3]
    uv run python scripts/large_pool.py build      # cache → <pool>/large.parquet (read by src/db/pool.py)
    uv run python scripts/large_pool.py coverage   # share of the organizers' wins the pool names, JSON

Two lists of legal entities that are NOT in the dump of the SME registry (`<pool>/companies.parquet`, #185):
- `gap` — the first `--top` winners of the organizers' data that are in neither the registry nor the open FNS
  datasets (D-137: the largest winners are there), by wins;
- `candidates` — entities of the open FNS datasets above the SME thresholds with an INN issued in Saint
  Petersburg or the Leningrad region that are not among the organizers' suppliers: «large and new».

`collect` asks ГИР БО (src/sources/bo_nalog.py) for every INN of the lists and, for an INN it has no row for,
the registry card of pb.nalog.ru (src/sources/pb_nalog.py; the `gap` list only). Every answer, an empty one
too, goes to the source cache (SOURCE_CACHE), so a rerun asks only what is missing: the cache is the
progress file. Pauses and the block after a refusal are the sources' own (src/sources/pacing.py; BO_MIN_INTERVAL_S,
PB_MIN_INTERVAL_S). A refusal (403/429, a protection page, a captcha) stops that source for the rest of the
run — nothing is bypassed; timeouts are retried on the next round.

`build` writes one row per INN that has a ГИР БО row or a registry card in the cache, with the cached page
itself (`url`, `fetched_at`, `body`): src/db/pool.py puts the page back into the cache of the product, so the
Evidence of the company is checked against the very text the source returned and the demo needs no network.
An INN neither source knows gets no row — nothing is guessed.

The organizers' data is read through the Store (D-136), the environment through src/settings.py (D-139).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import pool  # noqa: E402
from src.match.store import get_store  # noqa: E402
from src.settings import get_settings  # noqa: E402
from src.sources import bo_nalog, pb_nalog  # noqa: E402
from src.sources.cache import CachedPage, SourceCache  # noqa: E402

# ceiling of a medium enterprise: headcount — 209-ФЗ, income — Government decree No. 265 of 04.04.2016
SME_EMPLOYEES, SME_INCOME = 250, 2_000_000_000.0
HOME = ("78", "47")
TOP = 100


def _lit(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def connect(pool_dir: Path, fns: Path) -> duckdb.DuckDBPyConnection:
    """p — one row per (INN, lot) of the organizers' data; msp — INNs of the registry dump; fns — the open datasets."""
    con = duckdb.connect()
    con.register("part_in", get_store().participations(None))
    con.execute("CREATE TABLE p AS SELECT inn, lot_id, bool_or(is_winner) AS win FROM part_in GROUP BY inn, lot_id")
    con.execute(f"CREATE TABLE msp AS SELECT DISTINCT inn FROM read_parquet({_lit(pool_dir / 'companies.parquet')}) "
                "WHERE inn IS NOT NULL")  # a NULL would empty every NOT IN below
    con.execute(f"CREATE TABLE fns AS SELECT inn, employees, income FROM read_parquet({_lit(fns)}) "
                "WHERE inn IS NOT NULL")
    return con


def gap(con: duckdb.DuckDBPyConnection, top: int = TOP) -> list[tuple[str, int]]:
    """(INN, wins): legal entities with wins that are in neither the registry nor the datasets, largest first."""
    return con.execute("""
        SELECT inn, sum(win::INT)::BIGINT AS wins FROM p
        WHERE length(inn) = 10 AND inn NOT IN (SELECT inn FROM msp) AND inn NOT IN (SELECT inn FROM fns)
        GROUP BY inn HAVING sum(win::INT) > 0 ORDER BY wins DESC, inn LIMIT ?""", [top]).fetchall()


def candidates(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Large entities of the home regions that are neither organizers' suppliers nor in the registry."""
    home = " OR ".join(f"inn LIKE '{p}%'" for p in HOME)
    return [r[0] for r in con.execute(f"""
        SELECT inn FROM fns WHERE (employees > {SME_EMPLOYEES} OR income > {SME_INCOME}) AND ({home})
          AND inn NOT IN (SELECT inn FROM p) AND inn NOT IN (SELECT inn FROM msp) ORDER BY inn""").fetchall()]


def miss_url(inn: str) -> str:
    """Where «the registry has no such INN» is remembered, so that a rerun does not ask again."""
    return f"{pb_nalog.card_url(inn)}&found=0"


def source_page(inn: str, cache: SourceCache) -> tuple[str, CachedPage] | None:
    """('bo' | 'pb', the cached page that names the company) or None: not asked yet or unknown to both."""
    bo = cache.get(bo_nalog.search_url(inn), allow_stale=True)
    if bo is not None and bo_nalog.parse(bo.body):
        return "bo", bo
    card = cache.get(pb_nalog.card_url(inn), allow_stale=True)
    if card is not None and pb_nalog.parse(card.body):
        return "pb", card
    return None


def collect(inns: list[str], cache: SourceCache, *, egrul: bool, rounds: int = 3) -> Counter[str]:
    """Ask what the cache lacks. Stops a source at its refusal; a timeout is retried on the next round."""
    st: Counter[str] = Counter()
    bo_on, pb_on = True, egrul
    for rnd in range(rounds):
        st = Counter()
        for n, inn in enumerate(inns, 1):
            page = cache.get(bo_nalog.search_url(inn), allow_stale=True)
            if page is None and bo_on:
                try:
                    page = bo_nalog.fetch(inn, cache)
                    st["bo_fetched"] += 1
                except bo_nalog.Blocked as e:
                    print(f"ГИР БО: отказ источника — останавливаемся: {e}", flush=True)
                    bo_on = False
                except httpx.HTTPError as e:
                    st[f"bo_error:{type(e).__name__}"] += 1
            if page is None:
                st["bo_not_asked"] += 1
                continue
            if bo_nalog.parse(page.body):
                st["bo_found"] += 1
                continue
            st["bo_no_row"] += 1
            if not egrul:
                continue
            if cache.get(pb_nalog.card_url(inn), allow_stale=True) is not None:
                st["pb_found"] += 1
            elif cache.get(miss_url(inn), allow_stale=True) is not None:
                st["pb_no_row"] += 1
            elif pb_on:
                try:
                    pb_nalog.fetch_card(inn, cache)
                    st["pb_found"] += 1
                    st["pb_fetched"] += 1
                except pb_nalog.NotFound:
                    cache.put(miss_url(inn), f"query.inn: {inn}\nanswer.inn_rows: 0", status=404)
                    st["pb_no_row"] += 1
                    st["pb_fetched"] += 1
                except pb_nalog.CaptchaRequired as e:
                    print(f"pb.nalog.ru: капча — останавливаемся: {e}", flush=True)
                    pb_on = False
                    st["pb_not_asked"] += 1
                except (TimeoutError, httpx.HTTPError, ValueError) as e:
                    st[f"pb_error:{type(e).__name__}"] += 1
            else:
                st["pb_not_asked"] += 1
            if n % 50 == 0:
                print(f"round {rnd}: {n}/{len(inns)} {dict(st)}", flush=True)
        print(f"round {rnd} done: {len(inns)} ИНН {dict(st)}", flush=True)
        if not any("error" in k for k in st) or not (bo_on or pb_on):
            break
    st["stopped_bo"], st["stopped_pb"] = int(not bo_on), int(egrul and not pb_on)
    return st


def build(inns: list[str], cache: SourceCache, out: Path) -> dict[str, int]:
    """Cached answers → `out` (pool.LARGE_SCHEMA); INNs without a ГИР БО row or a registry card are left out.
    `inns` are outside the registry dump by construction (gap(), candidates()): its download time, from the
    manifest of the pool, is the time of that check."""
    manifest = json.loads((out.parent / "manifest.json").read_text(encoding="utf-8"))
    checked = float(manifest["fetched_at"])
    rows = []
    for inn in inns:
        found = source_page(inn, cache)
        if found is None:
            continue
        source, page = found
        if (row := pool.large_row(inn, source, page, checked)) is not None:
            rows.append(row)
    rows.sort(key=lambda r: r["inn"])
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=pool.LARGE_SCHEMA), tmp, compression="zstd")
    tmp.replace(out)
    by = Counter(r["source"] for r in rows)
    return {"asked": len(inns), "rows": len(rows), "bo": by["bo"], "pb": by["pb"],
            "with_okved": sum(1 for r in rows if r["okved_main"]),
            "with_region": sum(1 for r in rows if r["region_code"])}


def coverage(con: duckdb.DuckDBPyConnection, large: Path) -> dict[str, float | int]:
    """Wins and winners of the organizers' data named by the pool: the registry dump, then the non-SME table."""
    con.execute(f"CREATE OR REPLACE TABLE large AS SELECT inn, source, okved_main FROM read_parquet({_lit(large)})")
    size = con.execute("SELECT count(*), count(*) FILTER (source = 'bo'), count(*) FILTER (source = 'pb'), "
                       "count(okved_main) FROM large").fetchone()
    assert size is not None
    row = con.execute("""
        WITH w AS (SELECT inn, count(*) AS parts, sum(win::INT) AS wins FROM p GROUP BY inn)
        SELECT sum(wins)::BIGINT, sum(wins) FILTER (inn IN (SELECT inn FROM msp))::BIGINT,
               coalesce(sum(wins) FILTER (inn IN (SELECT inn FROM large)), 0)::BIGINT,
               count(*) FILTER (wins > 0 AND inn NOT IN (SELECT inn FROM msp)),
               count(*) FILTER (wins > 0 AND inn IN (SELECT inn FROM large)),
               sum(parts)::BIGINT, coalesce(sum(parts) FILTER (inn IN (SELECT inn FROM large)), 0)::BIGINT,
               (SELECT count(*) FROM large WHERE inn NOT IN (SELECT inn FROM p))
        FROM w""").fetchone()
    assert row is not None
    wins, msp_wins, large_wins, out_winners, large_winners, parts, large_parts, new = (int(v or 0) for v in row)
    outside = wins - msp_wins
    return {"large_rows": size[0], "large_from_bo": size[1], "large_from_pb": size[2], "large_with_okved": size[3],
            "wins": wins, "wins_in_sme_registry": msp_wins, "wins_outside_sme_registry": outside,
            "wins_in_large": large_wins, "winners_outside_sme_registry": out_winners,
            "winners_in_large": large_winners, "participations": parts, "participations_in_large": large_parts,
            "large_not_among_organizers_suppliers": new,
            "share_of_wins_outside_registry_now_in_pool": round(large_wins / outside, 4) if outside else 0.0,
            "share_of_all_wins_in_large": round(large_wins / wins, 4) if wins else 0.0,
            "share_of_all_wins_in_pool": round((msp_wins + large_wins) / wins, 4) if wins else 0.0,
            "share_of_all_wins_in_pool_before": round(msp_wins / wins, 4) if wins else 0.0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("collect", "build", "coverage"))
    ap.add_argument("--pool", type=Path, default=get_settings().pool_dir or pool.POOL)
    ap.add_argument("--fns", type=Path, default=get_settings().fns_opendata_dir / "fns_inns.parquet",
                    help="parsed open FNS datasets (scripts/fns_winners.py writes it)")
    ap.add_argument("--top", type=int, default=TOP, help="how many INNs of the gap list")
    ap.add_argument("--only", choices=("gap", "candidates"), default=None)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", type=Path, default=None, help="coverage: also write the JSON here")
    args = ap.parse_args(argv)
    for path in (args.pool / "companies.parquet", args.fns):
        if not path.exists():
            print(f"нет {path}: make pool-load / scripts/fns_winners.py", file=sys.stderr)
            return 2
    con = connect(args.pool, args.fns)
    large = args.pool / f"{pool.LARGE}.parquet"
    if args.command == "coverage":
        if not large.exists():
            print(f"нет {large}: сначала scripts/large_pool.py build", file=sys.stderr)
            return 2
        result: dict = {"top": args.top, **coverage(con, large)}
        if args.out:
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    cache = SourceCache(ttl_s=30 * 24 * 3600)
    top = [inn for inn, _ in gap(con, args.top)]
    rest = candidates(con)
    if args.command == "build":
        result = build(sorted({*top, *rest}), cache, large)
    else:
        result = {}
        if args.only != "candidates":
            result["gap"] = dict(collect(top, cache, egrul=True, rounds=args.rounds))
        if args.only != "gap":
            result["candidates"] = dict(collect(rest, cache, egrul=False, rounds=args.rounds))
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
