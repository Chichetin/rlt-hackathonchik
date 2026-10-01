"""parquet → Postgres loader (D-023) on the ingest fixture. Needs a live Postgres: TEST_DATABASE_URL
(e.g. postgresql://app:app@127.0.0.1:5432/app after `docker compose up -d db`); skipped otherwise."""

from __future__ import annotations

import json
import os
import shutil
from datetime import date
from pathlib import Path

import duckdb
import pytest

from src.db.load import load, pg_dsn
from src.ingest.build import build

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ingest"
URL = os.environ.get("TEST_DATABASE_URL", "")
SCHEMA = "test_load"  # never the app schema `data`

pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="TEST_DATABASE_URL не задан")


@pytest.fixture(scope="module")
def processed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("processed")
    build(FIXTURE, out, as_of=None, cutoff=date(2025, 7, 1))
    return out


@pytest.fixture(scope="module", autouse=True)
def _drop_test_schema():
    yield
    con = duckdb.connect()
    con.execute("LOAD postgres")
    con.execute(f"ATTACH '{pg_dsn(URL)}' AS pg (TYPE postgres)")
    con.execute(f"CALL postgres_execute('pg', 'DROP SCHEMA IF EXISTS {SCHEMA}, {SCHEMA}_new CASCADE')")


def _pg(sql: str) -> list[tuple]:
    con = duckdb.connect()
    con.execute("LOAD postgres")
    con.execute(f"ATTACH '{pg_dsn(URL)}' AS pg (TYPE postgres, READ_ONLY)")  # ATTACH takes no parameters
    return con.execute(f"SELECT * FROM postgres_query('pg', $${sql}$$)").fetchall()


def test_load_counts_match_parquet_and_rerun_is_idempotent(processed: Path) -> None:
    first = load(processed, URL, schema=SCHEMA)
    second = load(processed, URL, schema=SCHEMA)
    assert first == second == {
        "notices": 9, "items": 13, "participants": 15, "suppliers": first["suppliers"], "suppliers_2025_07_01":
        first["suppliers_2025_07_01"],
    }
    assert first["suppliers"] > 0
    for table, n in first.items():
        assert _pg(f"SELECT count(*) FROM {SCHEMA}.{table}") == [(n,)]
    # the staging schema of the swap is gone
    assert _pg(f"SELECT count(*) FROM pg_namespace WHERE nspname = '{SCHEMA}_new'") == [(0,)]


def test_indexes_for_filters_and_lookup(processed: Path) -> None:
    load(processed, URL, schema=SCHEMA)
    names = {r[0] for r in _pg(f"SELECT indexname FROM pg_indexes WHERE schemaname = '{SCHEMA}'")}
    for idx in ("notices_pkey", "notices_reqnum", "items_lot_id", "items_okpd2", "participants_lot_id",
                "participants_supplier_inn", "suppliers_inn", "suppliers_2025_07_01_inn"):
        assert idx in names, idx
    assert _pg(f"SELECT built_at IS NOT NULL FROM {SCHEMA}.manifest") == [(True,)]


def test_count_mismatch_keeps_previous_schema(processed: Path, tmp_path: Path) -> None:
    load(processed, URL, schema=SCHEMA)
    bad = tmp_path / "bad"
    shutil.copytree(processed, bad)
    manifest = json.loads((bad / "manifest.json").read_text(encoding="utf-8"))
    manifest["rows"]["items"] += 1
    (bad / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="items"):
        load(bad, URL, schema=SCHEMA)
    assert _pg(f"SELECT count(*) FROM {SCHEMA}.items") == [(13,)]


@pytest.mark.parametrize("schema", ["public", "pg_temp", "Data", "data; drop"])
def test_rejects_unsafe_schema(processed: Path, schema: str) -> None:
    with pytest.raises(ValueError):
        load(processed, URL, schema=schema)


def test_pg_dsn_drops_sqlalchemy_driver() -> None:
    assert pg_dsn("postgresql+psycopg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"
