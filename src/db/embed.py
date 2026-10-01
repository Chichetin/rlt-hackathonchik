"""Embeddings of distinct normalized product names → Postgres + pgvector (D-023).

    DATABASE_URL=postgresql://app:app@127.0.0.1:5432/app uv run python -m src.db.embed [--limit N]   # make db-embed

Key = normalized text (src/db/norm.py), not an item row: 2.55M positions are ~1.04M distinct texts.
Table `vec.<TABLE>` lives outside schema `data`, so `make db-load` does not drop the vectors and
reloads only add new texts. Resumable: only rows with a NULL vector are encoded, in keyset order,
with progress and ETA in the log. The HNSW index (cosine) is built after the fill (`--index`).

GPU elsewhere (no Postgres there): `--export texts.parquet` here → `--encode texts.parquet --out
vecs.parquet` on the GPU machine → `--import vecs.parquet` here (fills only rows still without a vector).
Model — e5 with prefixes `passage: ` (here) and `query: ` (search), research/METHODOLOGY.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql

from src.db.load import pg_dsn
from src.db.norm import norm_sql

log = logging.getLogger(__name__)
MODEL = "intfloat/multilingual-e5-small"
DIM = 384
TABLE = "names_e5_small"  # one table per model: vectors of different models are not comparable
PASSAGE, QUERY = "passage: ", "query: "
MAX_SEQ = 128  # tokens; p99 of a normalized name is 276 characters

Encoder = Callable[[list[str]], np.ndarray]


def model_encoder(prefix: str = PASSAGE, name: str = MODEL) -> Encoder:
    from sentence_transformers import SentenceTransformer  # heavy (torch): `make setup-ml`

    model = SentenceTransformer(name)  # CUDA if torch sees a GPU, else CPU
    model.max_seq_length = MAX_SEQ
    bs = 256 if model.device.type == "cuda" else 32  # CPU: small batches are faster (measured, D-033)
    log.info("модель %s на %s, batch %d", name, model.device, bs)
    return lambda texts: model.encode([prefix + t for t in texts], batch_size=bs, normalize_embeddings=True,
                                      show_progress_bar=False)


def _vec(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.6g}" for x in v.tolist()) + "]"


def embed(conn: psycopg.Connection, encode: Encoder, data_schema: str = "data", vec_schema: str = "vec",
          batch: int = 1024, limit: int | None = None) -> dict[str, int]:
    """Add new texts of `data_schema.items`, encode up to `limit` missing ones; returns totals."""
    t = sql.Identifier(vec_schema, TABLE)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(vec_schema)))
    conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} (norm text PRIMARY KEY, emb vector({}))").format(t, DIM))
    conn.execute(sql.SQL("INSERT INTO {} (norm) SELECT DISTINCT {} FROM {} WHERE product_name IS NOT NULL "
                         "ON CONFLICT DO NOTHING").format(t, norm_sql(sql.Identifier("product_name")),
                                                          sql.Identifier(data_schema, "items")))
    total, missing = conn.execute(sql.SQL("SELECT count(*), count(*) FILTER (WHERE emb IS NULL) FROM {}")
                                  .format(t)).fetchone()  # type: ignore[misc]
    todo = missing if limit is None else min(limit, missing)
    log.info("%s.%s: %d текстов, без вектора %d, в этом запуске %d", vec_schema, TABLE, total, missing, todo)
    done, last, started = 0, "", time.monotonic()
    while done < todo:
        rows = conn.execute(sql.SQL("SELECT norm FROM {} WHERE emb IS NULL AND norm > %s ORDER BY norm LIMIT %s")
                            .format(t), [last, min(batch, todo - done)]).fetchall()
        if not rows:
            break
        texts = [r[0] for r in rows]
        vecs = encode(texts)
        with conn.cursor() as cur:
            cur.executemany(sql.SQL("UPDATE {} SET emb = %s::vector WHERE norm = %s").format(t),
                            [(_vec(v), x) for v, x in zip(vecs, texts, strict=True)])
        done, last = done + len(texts), texts[-1]
        rate = done / (time.monotonic() - started)
        log.info("%d/%d (%.0f текстов/с, осталось ≈ %.0f мин)", done, todo, rate, (todo - done) / rate / 60)
    left = conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE emb IS NULL").format(t)).fetchone()[0]  # type: ignore[index]
    return {"total": total, "done": total - left}


def ensure_hnsw(conn: psycopg.Connection, vec_schema: str = "vec") -> None:
    conn.execute("SET maintenance_work_mem = '1GB'")
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING hnsw (emb vector_cosine_ops)")
                 .format(sql.Identifier(f"{TABLE}_hnsw"), sql.Identifier(vec_schema, TABLE)))


def nearest(vec_schema: str, q: np.ndarray, k: int, explain: bool = False) -> tuple[sql.Composed, list]:
    """(query, params): k nearest texts by cosine, with similarity; the HNSW index serves ORDER BY <=>."""
    v = _vec(q)
    query = sql.SQL("{}SELECT norm, 1 - (emb <=> %s::vector) AS sim FROM {} "
                    "ORDER BY emb <=> %s::vector LIMIT %s").format(sql.SQL("EXPLAIN " if explain else ""),
                                                                   sql.Identifier(vec_schema, TABLE))
    return query, [v, v, k]


def export_missing(conn: psycopg.Connection, path: Path, vec_schema: str = "vec") -> int:
    rows = conn.execute(sql.SQL("SELECT norm FROM {} WHERE emb IS NULL ORDER BY norm")
                        .format(sql.Identifier(vec_schema, TABLE))).fetchall()
    pq.write_table(pa.table({"norm": [r[0] for r in rows]}), path)
    return len(rows)


def encode_file(src: Path, dst: Path, encode: Encoder, chunk: int = 20_000) -> int:
    """texts.parquet → vecs.parquet (norm, emb float16[DIM]); no database needed."""
    texts = pq.read_table(src, columns=["norm"]).column("norm").to_pylist()
    schema = pa.schema([("norm", pa.string()), ("emb", pa.list_(pa.float16(), DIM))])
    started = time.monotonic()
    with pq.ParquetWriter(dst, schema) as w:
        for i in range(0, len(texts), chunk):
            part = texts[i:i + chunk]
            emb = np.asarray(encode(part), dtype=np.float16).reshape(-1)
            w.write_table(pa.table({"norm": part, "emb": pa.FixedSizeListArray.from_arrays(emb, DIM)}, schema=schema))
            done = i + len(part)
            rate = done / (time.monotonic() - started)
            left = (len(texts) - done) / rate / 60
            log.info("%d/%d (%.0f текстов/с, осталось ≈ %.1f мин)", done, len(texts), rate, left)
    return len(texts)


def import_file(conn: psycopg.Connection, path: Path, vec_schema: str = "vec") -> int:
    """Fill rows still without a vector from vecs.parquet; returns the number of rows updated."""
    conn.execute(sql.SQL("CREATE TEMP TABLE IF NOT EXISTS import_vecs (norm text, emb vector({}))").format(DIM))
    conn.execute("TRUNCATE import_vecs")
    with conn.cursor() as cur:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000):
            emb = batch.column("emb").flatten().to_numpy().reshape(-1, DIM).astype(np.float32)
            with cur.copy("COPY import_vecs (norm, emb) FROM STDIN") as cp:
                for norm, v in zip(batch.column("norm").to_pylist(), emb, strict=True):
                    cp.write_row((norm, _vec(v)))
        cur.execute(sql.SQL("UPDATE {} t SET emb = i.emb FROM import_vecs i WHERE t.norm = i.norm AND t.emb IS NULL")
                    .format(sql.Identifier(vec_schema, TABLE)))
        return cur.rowcount


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="embeddings of product names → pgvector (D-023)")
    ap.add_argument("--limit", type=int, help="encode at most N texts (subsample first)")
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--index", action="store_true", help="build the HNSW index after the fill")
    ap.add_argument("--export", type=Path, help="write texts without a vector to this parquet and exit")
    ap.add_argument("--encode", type=Path, help="encode this texts parquet (no database) into --out")
    ap.add_argument("--out", type=Path, default=Path("vecs.parquet"))
    ap.add_argument("--import", dest="import_", type=Path, help="fill missing vectors from this parquet")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # HF Hub HEAD requests on model load
    if args.encode:
        print({"encoded": encode_file(args.encode, args.out, model_encoder()), "out": str(args.out)})
        return 0
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgres"):
        print("нужен DATABASE_URL=postgresql://… (docker compose up -d db; make db-load)", file=sys.stderr)
        return 2
    with psycopg.connect(pg_dsn(url), autocommit=True) as conn:
        if args.export:
            print({"exported": export_missing(conn, args.export), "path": str(args.export)})
            return 0
        if args.import_:
            print({"imported": import_file(conn, args.import_)})
            if args.index:
                ensure_hnsw(conn)
            return 0
        print(embed(conn, model_encoder(), batch=args.batch, limit=args.limit))
        if args.index:
            ensure_hnsw(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
