"""Embeddings of distinct normalized product names in Postgres (D-023). Live Postgres with pgvector:
TEST_DATABASE_URL, skipped otherwise. The model is replaced by a deterministic fake encoder."""

from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path

import numpy as np
import psycopg
import pytest
from psycopg import sql

from src.db.embed import DIM, embed, encode_file, ensure_hnsw, export_missing, import_file, nearest
from src.db.load import load, pg_dsn
from src.db.norm import norm_sql, normalize
from src.ingest.build import build

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ingest"
URL = os.environ.get("TEST_DATABASE_URL", "")
SCHEMA, VEC = "test_emb", "test_vec"

pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="TEST_DATABASE_URL не задан")


def fake_encode(texts: list[str]) -> np.ndarray:
    """Unit vectors seeded by sha256 of the text: equal texts → equal vectors, no model needed."""
    out = np.stack([
        np.random.default_rng(int.from_bytes(hashlib.sha256(t.encode()).digest()[:8])).standard_normal(DIM)
        for t in texts
    ]).astype(np.float32)
    return out / np.linalg.norm(out, axis=1, keepdims=True)


@pytest.fixture(scope="module")
def conn(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("processed")
    build(FIXTURE, out, as_of=None, cutoff=date(2025, 7, 1))
    load(out, URL, schema=SCHEMA)
    with psycopg.connect(pg_dsn(URL), autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {VEC} CASCADE")
        yield c
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}, {VEC} CASCADE")


@pytest.mark.parametrize("raw", ["  Бумага   ОФИСНАЯ\tА4 ", "Ёлка ёлочная", "Картридж HP", "x\n y",
                                 "a\u2009b\u3000c", "\u3000Ручка", "Ёж\x85"])
def test_python_and_postgres_normalization_agree(conn, raw: str) -> None:
    got = conn.execute(sql.SQL("SELECT {}").format(norm_sql(sql.SQL("%s::text"))), [raw]).fetchone()[0]
    assert got == normalize(raw)


def test_embed_is_resumable_and_covers_distinct_names(conn) -> None:
    norm = norm_sql(sql.Identifier("product_name"))
    distinct = conn.execute(sql.SQL("SELECT count(DISTINCT {}) FROM {} WHERE {} <> ''").format(
        norm, sql.Identifier(SCHEMA, "items"), norm)).fetchone()[0]
    first = embed(conn, fake_encode, data_schema=SCHEMA, vec_schema=VEC, batch=4, limit=5)
    assert first == {"total": distinct, "done": 5}
    second = embed(conn, fake_encode, data_schema=SCHEMA, vec_schema=VEC, batch=4)
    assert second == {"total": distinct, "done": distinct}
    assert embed(conn, fake_encode, data_schema=SCHEMA, vec_schema=VEC) == second  # nothing left


def test_hnsw_index_is_used_for_nearest(conn) -> None:
    embed(conn, fake_encode, data_schema=SCHEMA, vec_schema=VEC)
    ensure_hnsw(conn, VEC)
    name = conn.execute(f"SELECT product_name FROM {SCHEMA}.items LIMIT 1").fetchone()[0]
    q = fake_encode([normalize(name)])[0]
    conn.execute("SET enable_seqscan = off")  # tiny table: force the planner to show the index
    plan = "\n".join(r[0] for r in conn.execute(*nearest(VEC, q, 3, explain=True)).fetchall())
    conn.execute("RESET enable_seqscan")
    assert "Index Scan" in plan and "hnsw" in plan
    rows = conn.execute(*nearest(VEC, q, 3)).fetchall()
    assert rows[0][0] == normalize(name) and rows[0][1] == pytest.approx(1.0)


def test_export_encode_import_roundtrip(conn, tmp_path: Path) -> None:
    """GPU handoff: export texts without vectors → encode elsewhere (no Postgres) → import."""
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(VEC)))
    total = embed(conn, fake_encode, data_schema=SCHEMA, vec_schema=VEC, limit=2)["total"]
    texts, vecs = tmp_path / "texts.parquet", tmp_path / "vecs.parquet"
    assert export_missing(conn, texts, vec_schema=VEC) == total - 2
    assert encode_file(texts, vecs, fake_encode, chunk=3) == total - 2
    assert import_file(conn, vecs, vec_schema=VEC) == total - 2
    assert embed(conn, fake_encode, data_schema=SCHEMA, vec_schema=VEC, limit=0) == {"total": total, "done": total}
    name = conn.execute(sql.SQL("SELECT norm FROM {} ORDER BY norm DESC LIMIT 1")
                        .format(sql.Identifier(VEC, "names_e5_small"))).fetchone()[0]
    rows = conn.execute(*nearest(VEC, fake_encode([name])[0], 1)).fetchall()
    assert rows[0][0] == name and rows[0][1] == pytest.approx(1.0, abs=1e-3)  # float16 in transit


def test_import_rejects_vectors_of_another_model(conn, tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    bad = tmp_path / "other.parquet"
    pq.write_table(pa.table({"norm": ["x"], "emb": pa.FixedSizeListArray.from_arrays(
        np.zeros(DIM, dtype=np.float16), DIM)}).replace_schema_metadata({"model": "other", "prefix": "passage: "}), bad)
    with pytest.raises(ValueError, match="другой модели"):
        import_file(conn, bad, vec_schema=VEC)
