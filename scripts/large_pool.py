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
run — nothing is bypassed; so do MAX_ERRORS failures in a row (a dead host is not knocked on for every INN).
Other timeouts are retried on the next round.

`build` writes one row per INN that has a ГИР БО row or a registry card in the cache, with the cached page
itself (`url`, `fetched_at`, `body`) and the list it came from: src/db/pool.py puts the page back into the cache
of the product, so the Evidence of the company is checked against the very text the source returned and no
network is needed. An INN neither source knows gets no row — nothing is guessed; an INN the registry dump does
have gets none either (it is in the pool already, and «не МСП» would be false).

`coverage` is a description of the whole period, not a backtest: the `gap` INNs were TAKEN from the organizers'
data by their wins, so «the pool names the winner» for them means «we asked the registries who these INNs
are», not «an open search would have found them». Only `candidates` are offered as new companies
(pool.large_candidates): the number for К3 is `candidates_offerable`.

The organizers' data is read through the Store (D-136), the environment through src/settings.py (D-139).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.contracts import inn_is_valid  # noqa: E402
from src.db import pool  # noqa: E402
from src.match.store import get_store  # noqa: E402
from src.settings import get_settings  # noqa: E402
from src.sources import bo_nalog, pb_nalog  # noqa: E402
from src.sources.cache import CachedPage, SourceCache  # noqa: E402

# ceiling of a medium enterprise: headcount — 209-ФЗ, income — Government decree No. 265 of 04.04.2016
SME_EMPLOYEES, SME_INCOME = 250, 2_000_000_000.0
HOME = ("78", "47")
TOP = 100
MAX_ERRORS = 10  # failures of one source in a row that stop it for the rest of the run
REFUSED = (403, 429)


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
    rows = con.execute("""
        SELECT inn, sum(win::INT)::BIGINT AS wins FROM p
        WHERE length(inn) = 10 AND inn NOT IN (SELECT inn FROM msp) AND inn NOT IN (SELECT inn FROM fns)
        GROUP BY inn HAVING sum(win::INT) > 0 ORDER BY wins DESC, inn""").fetchall()
    return [(inn, wins) for inn, wins in rows if inn_is_valid(inn)][:top]


def candidates(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Large entities of the home regions that are neither organizers' suppliers nor in the registry."""
    home = " OR ".join(f"inn LIKE '{p}%'" for p in HOME)
    return [r[0] for r in con.execute(f"""
        SELECT inn FROM fns WHERE (employees > {SME_EMPLOYEES} OR income > {SME_INCOME}) AND ({home})
          AND inn NOT IN (SELECT inn FROM p) AND inn NOT IN (SELECT inn FROM msp) ORDER BY inn""").fetchall()
            if inn_is_valid(r[0])]  # a bad checksum would raise in the sources' URL builders on every run


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


def _refused(e: Exception) -> bool:
    """pb.nalog.ru says «go away»: 403/429, or an answer that is not its JSON (a protection page)."""
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in REFUSED
    return isinstance(e, ValueError)


def collect(inns: list[str], cache: SourceCache, *, egrul: bool, rounds: int = 3) -> Counter[str]:
    """Ask what the cache lacks. A source is stopped for the rest of the run by its refusal (403/429, a
    protection page, a captcha) or by MAX_ERRORS failures in a row; a lone timeout is retried on the next round."""
    st: Counter[str] = Counter()
    on = {"bo": True, "pb": egrul}
    failed = {"bo": 0, "pb": 0}

    def stop(source: str, why: object) -> None:
        print(f"{source}: останавливаемся до конца прогона — {why}", flush=True)
        on[source] = False

    def error(source: str, e: Exception) -> None:
        st[f"{source}_error:{type(e).__name__}"] += 1
        failed[source] += 1
        if failed[source] >= MAX_ERRORS:
            stop(source, f"{MAX_ERRORS} ошибок подряд, последняя: {type(e).__name__}")

    for rnd in range(rounds):
        st = Counter()
        for n, inn in enumerate(inns, 1):
            page = cache.get(bo_nalog.search_url(inn), allow_stale=True)
            if page is None and on["bo"]:
                try:
                    page = bo_nalog.fetch(inn, cache)
                    st["bo_fetched"] += 1
                    failed["bo"] = 0
                except bo_nalog.Blocked as e:
                    stop("bo", e)
                except httpx.HTTPError as e:
                    error("bo", e)
            if page is None:
                st["bo_not_asked"] += 1
            elif bo_nalog.parse(page.body):
                st["bo_found"] += 1
            else:
                st["bo_no_row"] += 1
                if egrul:
                    card = cache.get(pb_nalog.card_url(inn), allow_stale=True)
                    if card is not None and pb_nalog.parse(card.body):
                        st["pb_found"] += 1
                    elif cache.get(miss_url(inn), allow_stale=True) is not None:
                        st["pb_no_row"] += 1
                    elif not on["pb"]:
                        st["pb_not_asked"] += 1
                    else:
                        try:
                            pb_nalog.fetch_card(inn, cache)
                            st["pb_found"] += 1
                            st["pb_fetched"] += 1
                            failed["pb"] = 0
                        except pb_nalog.NotFound:
                            cache.put(miss_url(inn), f"query.inn: {inn}\nanswer.inn_rows: 0", status=404)
                            st["pb_no_row"] += 1
                            st["pb_fetched"] += 1
                            failed["pb"] = 0
                        except pb_nalog.CaptchaRequired as e:
                            stop("pb", e)
                            st["pb_not_asked"] += 1
                        except (TimeoutError, httpx.HTTPError, ValueError) as e:
                            if _refused(e):
                                pb_nalog.PACER.block()  # every other caller in this process leaves the host alone too
                                stop("pb", f"отказ источника: {type(e).__name__} {e}")
                                st["pb_not_asked"] += 1
                            else:
                                error("pb", e)
            if n % 50 == 0:
                print(f"round {rnd}: {n}/{len(inns)} {dict(st)}", flush=True)
        print(f"round {rnd} done: {len(inns)} ИНН {dict(st)}", flush=True)
        if not any("error" in k for k in st) or not any(on.values()):
            break
    st["stopped_bo"], st["stopped_pb"] = int(not on["bo"]), int(egrul and not on["pb"])
    return st


def build(lists: dict[str, list[str]], cache: SourceCache, out: Path, in_registry: set[str]) -> dict[str, int]:
    """Cached answers → `out` (pool.LARGE_SCHEMA). `lists`: name of the list ('gap' | 'candidates') → its INNs.
    INNs without a ГИР БО row or a registry card are left out, and so is an INN of `in_registry` (it has a
    record in the registry dump). For the rest «no record in the dump» was checked here, against the dump whose
    download time the manifest of the pool gives."""
    manifest = json.loads((out.parent / "manifest.json").read_text(encoding="utf-8"))
    checked = float(manifest["fetched_at"])
    rows: dict[str, dict] = {}
    for name, inns in lists.items():
        for inn in inns:
            if inn in rows or inn in in_registry or (found := source_page(inn, cache)) is None:
                continue
            if (row := pool.large_row(inn, found[0], found[1], checked, name)) is not None:
                rows[inn] = row
    table = [rows[inn] for inn in sorted(rows)]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pylist(table, schema=pool.LARGE_SCHEMA), tmp, compression="zstd")
    tmp.replace(out)
    by = Counter(r["source"] for r in table)
    return {"asked": len({i for inns in lists.values() for i in inns}), "rows": len(table), "bo": by["bo"],
            "pb": by["pb"], "with_okved": sum(1 for r in table if r["okved_main"]),
            "with_region": sum(1 for r in table if r["region_code"])}


def coverage(con: duckdb.DuckDBPyConnection, large: Path) -> dict[str, float | int]:
    """What the non-SME table adds to the pool. Wins: of the `gap` list only — INNs taken from the organizers'
    data (see the module docstring). New companies: the `candidates` list; `candidates_offerable` — those
    large_candidates() can return for a region of src/regions.py (active, with an OKVED and a region code)."""
    con.execute(f"""CREATE OR REPLACE TABLE large AS
        SELECT inn, source, okved_main, list, ({pool.LARGE_ACTIVE} AND okved_main IS NOT NULL
                                               AND region_code IS NOT NULL) AS offerable
        FROM read_parquet({_lit(large)})""")
    size = con.execute("""
        SELECT count(*), count(*) FILTER (source = 'bo'), count(*) FILTER (source = 'pb'), count(okved_main),
               count(*) FILTER (list = 'gap'), count(*) FILTER (list = 'candidates'),
               count(*) FILTER (list = 'candidates' AND offerable),
               count(*) FILTER (list = 'candidates' AND inn IN (SELECT inn FROM p)) FROM large""").fetchone()
    row = con.execute("""
        WITH w AS (SELECT inn, count(*) AS parts, sum(win::INT) AS wins FROM p GROUP BY inn)
        SELECT sum(wins)::BIGINT, sum(wins) FILTER (inn IN (SELECT inn FROM msp))::BIGINT,
               coalesce(sum(wins) FILTER (inn IN (SELECT inn FROM large)), 0)::BIGINT,
               count(*) FILTER (wins > 0 AND inn NOT IN (SELECT inn FROM msp)),
               count(*) FILTER (wins > 0 AND inn IN (SELECT inn FROM large)),
               sum(parts)::BIGINT, coalesce(sum(parts) FILTER (inn IN (SELECT inn FROM large)), 0)::BIGINT
        FROM w""").fetchone()
    assert size is not None and row is not None
    wins, msp_wins, large_wins, out_winners, large_winners, parts, large_parts = (int(v or 0) for v in row)
    outside = wins - msp_wins
    return {"large_rows": size[0], "large_from_bo": size[1], "large_from_pb": size[2], "large_with_okved": size[3],
            "gap_rows": size[4], "candidates_rows": size[5], "candidates_offerable": size[6],
            "candidates_among_organizers_suppliers": size[7],  # 0 by construction: they are «new»
            "wins": wins, "wins_in_sme_registry": msp_wins, "wins_outside_sme_registry": outside,
            "wins_in_large": large_wins, "winners_outside_sme_registry": out_winners,
            "winners_in_large": large_winners, "participations": parts, "participations_in_large": large_parts,
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
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        built = datetime.fromtimestamp(large.stat().st_mtime, UTC).isoformat(timespec="seconds")
        # the table grows while `collect` goes on: the numbers are those of this snapshot
        result: dict = {"commit": commit.stdout.strip() or None, "large_built_at": built, "top": args.top,
                        **coverage(con, large)}
        if args.out:
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    cache = SourceCache(ttl_s=30 * 24 * 3600)
    top = [inn for inn, _ in gap(con, args.top)]
    rest = candidates(con)
    if args.command == "build":
        asked = sorted({*top, *rest})
        con.execute("CREATE TABLE asked AS SELECT unnest(?) AS inn", [asked])
        in_registry = {r[0] for r in con.execute("SELECT inn FROM asked WHERE inn IN (SELECT inn FROM msp)").fetchall()}
        result = build({"gap": top, "candidates": rest}, cache, large, in_registry)
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
