"""Read path on Postgres = read path on parquet (#49, D-024): the same rows, the same search() answer.

Tests marked needs_pg load the ingest fixture into a schema of a live Postgres (TEST_DATABASE_URL, e.g.
postgresql://app:app@127.0.0.1:5432/app after `docker compose up -d db`); skipped otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq_io
import pytest

from src.api.lots import LotDataUnavailable, LotNotFound, find_lot
from src.contracts import Query
from src.match import index as index_mod
from src.match import store as store_mod
from src.match.search import search

CUTOFF = date(2025, 7, 1)
URL = os.environ.get("TEST_DATABASE_URL", "")
SCHEMA = "test_store"  # never the app schema `data`
needs_pg = pytest.mark.skipif(not URL.startswith("postgres"), reason="TEST_DATABASE_URL не задан")

QUERIES = [
    Query(query_id="a", text="Бумага офисная А4"),
    Query(query_id="b", text="бумага", as_of=CUTOFF),
    Query(query_id="c", text="бумага офисная а4", okpd2=["17.12.14.110"]),
    Query(query_id="d", text="бумага", okpd2=["17.12.99.000"], as_of=CUTOFF),
    Query(query_id="e", text="картридж", region="Ленинградская область"),
    Query(query_id="f", text="ремонт"),
    Query(query_id="g", text="вода питьевая", as_of=CUTOFF),
    Query(query_id="h", text="бумага офисная", okpd2=["17.12"]),
]
# Texts and codes where Postgres' en_US collation and bytewise order disagree (case, '-', '(', '.'):
# without COLLATE "C" the BM25 doc numbering, ties and the deepest OKPD2 code would differ.
TRICKY = [(1, "бумага офисная", "17.121"), (1, "Бумага офисная", "17.12.14.1"), (3, "бумага-офисная", "17.12.141"),
          (3, "БУМАГА ОФИСНАЯ", "17.12.14.110"), (3, "бумага (офисная)", "17.12.2"), (2, "Бумага офисная", "17.12.2")]


def _tricky(src: Path, out: Path) -> Path:
    shutil.copytree(src, out, dirs_exist_ok=True)
    values = ", ".join(f"({lot}, '{name}', '{code}')" for lot, name, code in TRICKY)
    duckdb.sql(f"""
        COPY (SELECT * FROM read_parquet('{(src / "items.parquet").as_posix()}')
              UNION ALL
              SELECT lot_id, product_name, okpd2_code, substr(okpd2_code, 1, 2),
                     CASE WHEN length(okpd2_code) >= 5 THEN substr(okpd2_code, 1, 5) END,
                     CASE WHEN length(okpd2_code) >= 8 THEN substr(okpd2_code, 1, 8) END
              FROM (VALUES {values}) v(lot_id, product_name, okpd2_code))
        TO '{(out / "items.parquet").as_posix()}' (FORMAT parquet)""")
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    manifest["rows"]["items"] += len(TRICKY)
    manifest["built_at"] = "2026-01-01T00:00:00+00:00"  # another ingest: another index key
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return out


@pytest.fixture(scope="module", params=["fixture", "tricky"])
def loaded(request, fixture_processed: Path, tmp_path_factory: pytest.TempPathFactory):
    """The ingest fixture (and its copy with collation traps) in parquet and in schema SCHEMA."""
    from src.db.load import load

    processed = fixture_processed
    if request.param == "tricky":
        processed = _tricky(fixture_processed, tmp_path_factory.mktemp("tricky"))
    load(processed, URL, schema=SCHEMA)
    yield processed
    import psycopg

    from src.db.load import pg_dsn

    with psycopg.connect(pg_dsn(URL), autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}, {SCHEMA}_new CASCADE")  # type: ignore[arg-type]


@pytest.fixture
def use(search_data, loaded, monkeypatch):
    """use('parquet' | 'postgres'): the store search() and /api/lot read, with a cold index cache."""

    def switch(mode: str) -> None:
        monkeypatch.setenv("DATA_PROCESSED_DIR", str(loaded))
        monkeypatch.setenv("DATA_STORE", mode)
        monkeypatch.setenv("DATABASE_URL", URL)
        monkeypatch.setenv("DATA_SCHEMA", SCHEMA)
        store_mod.clear_stores()
        index_mod.clear_memory_cache()

    yield switch
    store_mod.clear_stores()


@needs_pg
def test_store_rows_are_identical(loaded):
    pq, pg = store_mod.ParquetStore(loaded), store_mod.PgStore(URL, SCHEMA)
    assert pg.manifest() == pq.manifest() and pg.built_at() == pq.built_at()
    for as_of in (None, CUTOFF):
        assert pg.index_rows(as_of) == pq.index_rows(as_of)
        idx = index_mod.get_index(pq, as_of)
        lots = idx.lot_hits(idx.retrieve(["бумаг"], 50))
        assert len(lots) and pg.candidates(lots, as_of, 200) == pq.candidates(lots, as_of, 200)
        inns = [r[0] for r in pq.candidates(lots, as_of, 200)]
        for codes in (["17.12.14.110"], ["17.12.99.000", "26.20"], ["17.12"], ["17"], ["99"]):
            assert pg.class_history(inns, codes, as_of) == pq.class_history(inns, codes, as_of)
        assert pg.supplier_rows(inns + ["0000000000"], as_of) == pq.supplier_rows(inns + ["0000000000"], as_of)
        for cust in ("7801000051", "7701000040", "0000000000"):
            assert pg.customer_lots(inns, cust, as_of) == pq.customer_lots(inns, cust, as_of)
        assert pq.customer_lots(inns, "7801000051", as_of)  # not vacuous
    assert pg.lot_codes([1, 2, 3, 6]) == pq.lot_codes([1, 2, 3, 6])
    assert pg.lot_codes([]) == pq.lot_codes([]) == []


@needs_pg
def test_search_on_postgres_equals_search_on_parquet(loaded, use):
    use("parquet")
    on_parquet = [search(q, k=200) for q in QUERIES]
    use("postgres")
    assert store_mod.get_store().name == "postgres"
    on_pg = [search(q, k=200) for q in QUERIES]
    assert any(on_parquet)
    for q, a, b in zip(QUERIES, on_parquet, on_pg, strict=True):
        assert [m.model_dump() for m in b] == [m.model_dump() for m in a], q.query_id


@needs_pg
def test_lot_lookup_on_postgres(loaded, use):
    use("parquet")
    by_parquet = find_lot("6")
    use("postgres")
    assert find_lot("6") == by_parquet
    with pytest.raises(LotNotFound):
        find_lot("424242")
    with pytest.raises(LotNotFound):  # longer than bigint: compared as reqnum only, not a crash
        find_lot("9" * 30)
    store_mod.clear_stores()


@needs_pg
def test_postgres_index_is_shared_with_parquet(loaded, use, tmp_path):
    use("parquet")
    built = index_mod.get_index(None, CUTOFF)
    use("postgres")
    assert index_mod.get_index(None, CUTOFF).path == built.path  # same ingest -> same key, no rebuild


def test_auto_falls_back_to_parquet_and_retries_postgres_later(search_data, monkeypatch):
    monkeypatch.setenv("DATA_STORE", "auto")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///queue.sqlite")
    store_mod.clear_stores()
    assert store_mod.get_store().name == "parquet"

    tries = []

    def down(self):
        tries.append(1)
        raise store_mod.StoreUnavailable("Postgres: схема data не загружена — `make db-load`")

    monkeypatch.setattr(store_mod.PgStore, "manifest", down)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app@127.0.0.1:1/app")
    store_mod.clear_stores()
    assert store_mod.get_store().name == "parquet"
    assert search(Query(query_id="q", text="бумага"))
    assert len(tries) == 1  # the fallback is kept: no reconnect per request
    monkeypatch.setattr(store_mod, "RETRY_PG_S", 0.0)
    store_mod.clear_stores()
    store_mod.get_store()
    store_mod.get_store()
    assert len(tries) == 3  # expired fallback: Postgres is tried again
    store_mod.clear_stores()


def test_strict_postgres_without_db_is_unavailable(search_data, monkeypatch):
    monkeypatch.setenv("DATA_STORE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app@127.0.0.1:1/app")  # nothing listens on port 1
    store_mod.clear_stores()
    with pytest.raises(store_mod.StoreUnavailable, match="db-load"):  # a real refused connection
        store_mod.get_store()
    monkeypatch.setattr(store_mod.PgStore, "manifest", lambda self: (_ for _ in ()).throw(
        store_mod.StoreUnavailable("`make db-load`")))
    with pytest.raises(FileNotFoundError):  # the API answers 503
        search(Query(query_id="q", text="бумага"))
    with pytest.raises(LotDataUnavailable):
        find_lot("6")
    monkeypatch.setenv("DATA_STORE", "duckdb")
    with pytest.raises(ValueError, match="DATA_STORE"):
        store_mod.get_store()
    store_mod.clear_stores()


def test_lot_hits_keep_the_best_document_per_lot(search_data):
    idx = index_mod.get_index(search_data, None)
    hits = idx.retrieve(["бумаг", "офисн", "a4"], 50)
    lots = idx.lot_hits(hits)
    assert len(set(lots.lot_id.tolist())) == len(lots)
    best = {}
    for d, s in hits:  # brute force: every (doc, lot) pair
        for lot in idx._maps()[0][idx._maps()[1][d]:idx._maps()[1][d + 1]]:
            best.setdefault(int(lot), (s, d))
    assert {int(lot): (float(s), int(d)) for lot, s, d in zip(lots.lot_id, lots.score, lots.doc_id, strict=True)} \
        == best
    assert lots.top(2) == sorted(((k, v[0]) for k, v in best.items()), key=lambda x: (-x[1], x[0]))[:2]
    assert len(idx.lot_hits([])) == 0


@needs_pg
def test_database_down_after_start_is_503_and_auto_falls_back(loaded, use, monkeypatch):
    """Postgres chosen at start, then the container stops: 503 (not 500), and the next request uses parquet."""
    import psycopg
    from fastapi.testclient import TestClient

    from src.api import app as app_mod

    use("auto")
    store = store_mod.get_store()
    assert store.name == "postgres" and find_lot("6").query_id == "lot-6"
    monkeypatch.setattr(app_mod, "_engine", None)
    monkeypatch.setenv("DATABASE_URL", URL)

    def refused(*a, **kw):
        raise psycopg.OperationalError("connection refused")

    store._local.conn.close()  # type: ignore[attr-defined]
    monkeypatch.setattr(psycopg, "connect", refused)
    client = TestClient(app_mod.app)
    r = client.get("/api/lot/6")
    assert r.status_code == 503 and "Postgres" in r.json()["detail"]
    assert store_mod.get_store().name == "parquet"  # forgotten, auto re-checked and fell back
    assert client.get("/api/lot/6").json()["query_id"] == "lot-6"
    assert client.post("/api/search", json={"query_id": "q", "text": "бумага"}).json()["history"]


# --- class_history from the precomputed supplier_items (D-073): the same answer as the original SQL ---

WIDE_CODES = ["17.12.14.110", "17.12.14.119", "17.12.14.1", "17.12.141", "17.121", "17.12.19.000", "17.99.99.000",
              "17.12", "17.1", "17", "26.20.40.110", "26.20.40.119", "26.20", "11.07", "11.07.11.130", "33.12.1",
              "33.12.18.000", "62.01.11.000", None]  # levels 4..0, short codes, bytewise-vs-locale traps, NULL
WIDE_SUPPLIERS = [f"78010000{n:02d}" for n in range(14)]
NULL_ONLY = "7801000099"  # takes part only in lots whose positions have no OKPD2 code


def _levels(code: str | None) -> tuple[str | None, ...]:
    if code is None:
        return (None, None, None)
    return (code[:2], code[:5] if len(code) >= 5 else None, code[:8] if len(code) >= 8 else None)


def _wide(src: Path, out: Path) -> Path:
    """The ingest fixture with random participants and items: several positions per lot (one code twice, NULL codes),
    several candidates per lot, a pair with both is_winner values, lots on both sides of CUTOFF."""
    rnd = random.Random(136)
    shutil.copytree(src, out, dirs_exist_ok=True)
    items, parts = [], []
    for lot in range(1, 81):
        day = date(2024, 1, 1) + timedelta(days=rnd.randrange(0, 700))
        codes = [rnd.choice(WIDE_CODES) for _ in range(rnd.randint(1, 5))]
        codes += codes[:1] if rnd.random() < 0.3 else []  # the same code in two positions of the lot
        items += [(lot, f"p{lot}-{n}", c, *_levels(c)) for n, c in enumerate(codes)]
        for inn in rnd.sample(WIDE_SUPPLIERS, rnd.randint(1, 4)):
            win = rnd.random() < 0.4
            parts += [(lot, inn, win, day)]
            if rnd.random() < 0.1:
                parts += [(lot, inn, not win, day)]
    items += [(81, "p81", None, *_levels(None)), (82, "p82-0", None, *_levels(None)),
              (82, "p82-1", None, *_levels(None))]
    parts += [(81, NULL_ONLY, True, date(2024, 5, 1)), (82, NULL_ONLY, False, date(2025, 5, 1)),
              (82, WIDE_SUPPLIERS[0], True, date(2025, 5, 1))]
    pq_io.write_table(pa.table({
        "lot_id": pa.array([i[0] for i in items], pa.int64()), "product_name": [i[1] for i in items],
        "okpd2_code": [i[2] for i in items], "okpd2_l2": [i[3] for i in items], "okpd2_l4": [i[4] for i in items],
        "okpd2_l6": [i[5] for i in items]}), out / "items.parquet")
    pq_io.write_table(pa.table({
        "lot_id": pa.array([x[0] for x in parts], pa.int64()), "supplier_inn": [x[1] for x in parts],
        "supplier_kpp": pa.array([None] * len(parts), pa.string()), "is_winner": [x[2] for x in parts],
        "inn_valid": [True] * len(parts), "publish_date": pa.array([x[3] for x in parts], pa.date32()),
        "channel": ["ЭМ"] * len(parts), "customer_inn": ["7801000051"] * len(parts),
        "start_price": pa.array([1.0] * len(parts), pa.float64())}), out / "participants.parquet")
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    manifest["rows"].update(items=len(items), participants=len(parts))
    manifest["built_at"] = "2026-01-02T00:00:00+00:00"
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def wide(fixture_processed: Path, tmp_path_factory: pytest.TempPathFactory):
    import psycopg

    from src.db.load import load, pg_dsn

    processed = _wide(fixture_processed, tmp_path_factory.mktemp("wide"))
    schema = f"{SCHEMA}_wide"
    load(processed, URL, schema=schema)
    yield processed, schema
    with psycopg.connect(pg_dsn(URL), autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {schema}, {schema}_new CASCADE")  # type: ignore[arg-type]


def _wide_queries():
    rnd = random.Random(7)
    real = [c for c in WIDE_CODES if c]
    sets = [[c] for c in real] + [rnd.sample(real, rnd.randint(2, 3)) for _ in range(40)]
    return sets + [["99.99.99.999"], []]


@needs_pg
def test_class_history_from_supplier_items_equals_the_original_sql(wide, monkeypatch):
    processed, schema = wide
    pq, pg = store_mod.ParquetStore(processed), store_mod.PgStore(URL, schema)
    used = []
    fetch = store_mod.PgStore._fetch
    monkeypatch.setattr(store_mod.PgStore, "_fetch", lambda self, tpl, *a, **kw: (used.append(tpl),
                                                                                  fetch(self, tpl, *a, **kw))[1])
    old = store_mod.PgStore(URL, schema)  # the same schema read the old way: join of every lot to items
    monkeypatch.setattr(old, "_has_supplier_items", lambda: False)
    inns = [*WIDE_SUPPLIERS, NULL_ONLY, "0000000000"]
    checked = deep = 0
    for as_of in (None, CUTOFF, date(2024, 6, 1)):
        for codes in _wide_queries():
            want = pq.class_history(inns, codes, as_of)
            assert pg.class_history(inns, codes, as_of) == want, (codes, as_of)
            assert old.class_history(inns, codes, as_of) == want, (codes, as_of)
            checked += 1
            deep += any(v[0] >= 2 and v[2] > v[3] > 0 for v in want.values())  # lots and wins both non-trivial
    assert checked == 3 * len(_wide_queries()) and deep > 20  # not vacuous
    assert store_mod._CLASS_HISTORY_PG in used and store_mod._CLASS_HISTORY in used
    assert pg.class_history([], ["17.12"], None) == {}
    assert pg.class_history([NULL_ONLY], ["17.12"], None) == {NULL_ONLY: (0, None, 0, 0)}  # code NULL, not ''


@needs_pg
def test_loader_builds_supplier_items_per_lot_and_code(wide):
    import psycopg

    from src.db.load import pg_dsn

    processed, schema = wide
    want = duckdb.sql(f"""
        SELECT count(*) FROM (SELECT DISTINCT p.supplier_inn, p.lot_id, p.is_winner, i.okpd2_code
        FROM read_parquet('{(processed / "participants.parquet").as_posix()}') p
        JOIN read_parquet('{(processed / "items.parquet").as_posix()}') i USING (lot_id))""").fetchall()[0][0]
    with psycopg.connect(pg_dsn(URL), autocommit=True) as c:
        got = c.execute(f"SELECT count(*) FROM {schema}.supplier_items").fetchone()[0]  # type: ignore[arg-type]
        names = {r[0] for r in c.execute("SELECT indexname FROM pg_indexes WHERE schemaname = %s", (schema,))}
    assert got == want > 100 and "supplier_items_inn" in names


@needs_pg
def test_class_history_without_supplier_items_still_answers_and_says_so(wide, caplog):
    """A schema loaded before D-073 has no supplier_items: the old query, a warning, no crash."""
    import psycopg

    from src.db.load import pg_dsn

    processed, schema = wide
    want = store_mod.ParquetStore(processed).class_history(WIDE_SUPPLIERS, ["17.12.14.110"], None)
    assert want
    with psycopg.connect(pg_dsn(URL), autocommit=True) as c:
        c.execute(f"ALTER TABLE {schema}.supplier_items RENAME TO supplier_items_off")  # type: ignore[arg-type]
        try:
            with caplog.at_level(logging.WARNING, logger="src.match.store"):
                assert store_mod.PgStore(URL, schema).class_history(WIDE_SUPPLIERS, ["17.12.14.110"], None) == want
            assert "supplier_items" in caplog.text and "db-load" in caplog.text
        finally:
            c.execute(f"ALTER TABLE {schema}.supplier_items_off RENAME TO supplier_items")  # type: ignore[arg-type]
    caplog.clear()
    assert store_mod.PgStore(URL, schema).class_history(WIDE_SUPPLIERS, ["17.12.14.110"], None) == want
    assert not caplog.text
