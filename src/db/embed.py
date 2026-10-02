"""Embeddings of distinct normalized product names → Postgres + pgvector (D-054).

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
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql

from src.db.load import pg_dsn
from src.db.norm import norm_sql
from src.settings import get_settings

log = logging.getLogger(__name__)
MODEL = "intfloat/multilingual-e5-small"
DIM = 384
TABLE = "names_e5_small"  # one table per model: vectors of different models are not comparable
PASSAGE, QUERY = "passage: ", "query: "
MAX_SEQ = 128  # tokens; p99 of a normalized name is 276 characters


@dataclass(frozen=True)
class Spec:
    dim: int
    table: str  # one table per model: vectors of different models are not comparable
    query: str
    passage: str


# Models compared in #115 (src/eval/emb_bench.py); `embedding_model` in configs picks one (#98).
SPECS = {
    MODEL: Spec(DIM, TABLE, QUERY, PASSAGE),
    "deepvk/USER-base": Spec(768, "names_user_base", QUERY, PASSAGE),
    "intfloat/multilingual-e5-base": Spec(768, "names_e5_base", QUERY, PASSAGE),
    "cointegrated/rubert-tiny2": Spec(312, "names_rubert_tiny2", "", ""),
    # E3 (#270): USER-base fine-tuned on 100k name pairs of one OKPD2 code; weights are not in git — the folder
    # MODELS_DIR/user-base-okpd2 (as src/eval/emb_bench.py FINETUNED); vectors — vecs-user-base-okpd2.parquet (#485)
    "e3/user-base-okpd2": Spec(768, "names_e3_okpd2", QUERY, PASSAGE),
}
LOCAL = {"e3/user-base-okpd2"}  # fine-tuned models: loaded from MODELS_DIR/<last part of the name>


def spec(model: str | None) -> Spec:
    if (model or MODEL) not in SPECS:
        raise ValueError(f"embedding_model {model!r} не поддерживается (есть: {', '.join(SPECS)})")
    return SPECS[model or MODEL]

Encoder = Callable[[list[str]], np.ndarray]


def model_path(name: str) -> str:
    """HF id, or the local folder of a fine-tuned model (MODELS_DIR/<slug>, #485)."""
    return str(get_settings().models_dir / name.split("/")[-1].lower()) if name in LOCAL else name


def model_encoder(prefix: str = PASSAGE, name: str = MODEL) -> Encoder:
    # heavy (torch), optional extra: `make setup-ml`
    from sentence_transformers import SentenceTransformer, models  # pyright: ignore

    try:
        model = SentenceTransformer(model_path(name))  # CUDA if torch sees a GPU, else CPU
    except TypeError:
        # USER-base keeps Normalize at the model root: sentence-transformers 6 reads the HF config.json into it
        # and fails. The same pipeline by hand: Transformer -> mean pooling -> Normalize (#115).
        tr = models.Transformer(model_path(name), max_seq_length=MAX_SEQ)
        model = SentenceTransformer(modules=[tr, models.Pooling(tr.get_word_embedding_dimension(), "mean"),
                                             models.Normalize()])
    model.max_seq_length = MAX_SEQ
    bs = 256 if model.device.type == "cuda" else 32  # CPU: small batches are faster (measured, D-033)
    log.info("модель %s на %s, batch %d", name, model.device, bs)
    return lambda texts: model.encode([prefix + t for t in texts], batch_size=bs, normalize_embeddings=True,
                                      show_progress_bar=False)


def _vec(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.6g}" for x in v.tolist()) + "]"


def _rate(done: int, started: float) -> float:
    """Texts per second; the clock may not tick between two calls (Windows: ~15 ms resolution)."""
    return done / max(time.monotonic() - started, 1e-3)


def ensure_table(conn: psycopg.Connection, data_schema: str = "data", vec_schema: str = "vec",
                 model: str | None = None) -> None:
    """Create the model's table in `vec_schema` if needed and add the new distinct texts of `data_schema.items`."""
    sp = spec(model)
    t = sql.Identifier(vec_schema, sp.table)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(vec_schema)))
    conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} (norm text PRIMARY KEY, emb vector({}))").format(t, sp.dim))
    norm = norm_sql(sql.Identifier("product_name"))
    conn.execute(sql.SQL("INSERT INTO {} (norm) SELECT DISTINCT {} FROM {} WHERE {} <> '' "  # '' never passes keyset
                         "ON CONFLICT DO NOTHING").format(t, norm, sql.Identifier(data_schema, "items"), norm))


def embed(conn: psycopg.Connection, encode: Encoder, data_schema: str = "data", vec_schema: str = "vec",
          batch: int = 1024, limit: int | None = None) -> dict[str, int]:
    """Add new texts of `data_schema.items`, encode up to `limit` missing ones; returns totals."""
    t = sql.Identifier(vec_schema, TABLE)
    ensure_table(conn, data_schema, vec_schema)
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
        rate = _rate(done, started)
        log.info("%d/%d (%.0f текстов/с, осталось ≈ %.0f мин)", done, todo, rate, (todo - done) / rate / 60)
    left = conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE emb IS NULL").format(t)).fetchone()[0]  # type: ignore[index]
    return {"total": total, "done": total - left}


def ensure_hnsw(conn: psycopg.Connection, vec_schema: str = "vec", model: str | None = None) -> None:
    table = spec(model).table
    conn.execute("SET maintenance_work_mem = '1GB'")
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING hnsw (emb vector_cosine_ops)")
                 .format(sql.Identifier(f"{table}_hnsw"), sql.Identifier(vec_schema, table)))


def nearest(vec_schema: str, q: np.ndarray, k: int, explain: bool = False,
            table: str = TABLE) -> tuple[sql.Composed, list]:
    """(query, params): k nearest texts by cosine, with similarity; the HNSW index serves ORDER BY <=>."""
    v = _vec(q)
    query = sql.SQL("{}SELECT norm, 1 - (emb <=> %s::vector) AS sim FROM {} "
                    "ORDER BY emb <=> %s::vector LIMIT %s").format(sql.SQL("EXPLAIN " if explain else ""),
                                                                   sql.Identifier(vec_schema, table))
    return query, [v, v, k]


def export_missing(conn: psycopg.Connection, path: Path, vec_schema: str = "vec", model: str | None = None,
                   data_schema: str = "data") -> int:
    ensure_table(conn, data_schema, vec_schema, model)  # a new model's table is filled from data.items first
    rows = conn.execute(sql.SQL("SELECT norm FROM {} WHERE emb IS NULL ORDER BY norm")
                        .format(sql.Identifier(vec_schema, spec(model).table))).fetchall()
    pq.write_table(pa.table({"norm": [r[0] for r in rows]}), path)
    return len(rows)


def encode_file(src: Path, dst: Path, encode: Encoder, chunk: int = 20_000, model: str | None = None) -> int:
    """texts.parquet → vecs.parquet (norm, emb float16[dim of the model]); no database needed."""
    texts = pq.read_table(src, columns=["norm"]).column("norm").to_pylist()
    sp = spec(model)
    schema = pa.schema([("norm", pa.string()), ("emb", pa.list_(pa.float16(), sp.dim))],
                       metadata={"model": model or MODEL, "prefix": sp.passage})
    started = time.monotonic()
    with pq.ParquetWriter(dst, schema) as w:
        for i in range(0, len(texts), chunk):
            part = texts[i:i + chunk]
            emb = np.asarray(encode(part), dtype=np.float16).reshape(-1)
            arr = pa.FixedSizeListArray.from_arrays(emb, sp.dim)
            w.write_table(pa.table({"norm": part, "emb": arr}, schema=schema))
            done = i + len(part)
            rate = _rate(done, started)
            left = (len(texts) - done) / rate / 60
            log.info("%d/%d (%.0f текстов/с, осталось ≈ %.1f мин)", done, len(texts), rate, left)
    return len(texts)


def import_file(conn: psycopg.Connection, path: Path, data_schema: str = "data", vec_schema: str = "vec",
                batch_size: int = 20_000, model: str | None = None) -> int:
    """Fill rows still without a vector from vecs.parquet; returns the number of rows updated.
    Works on a fresh database too: the table is created from `data_schema.items` first."""
    meta = pq.read_schema(path).metadata or {}
    got = {k.decode(): v.decode() for k, v in meta.items() if k in (b"model", b"prefix")}
    name, sp = model or MODEL, spec(model)
    if got != {"model": name, "prefix": sp.passage}:
        raise ValueError(f"{path}: векторы другой модели или префикса {got}, нужно {name!r} и {sp.passage!r}")
    ensure_table(conn, data_schema, vec_schema, model)
    conn.execute("DROP TABLE IF EXISTS import_vecs")  # its dim is the model's
    conn.execute(sql.SQL("CREATE TEMP TABLE import_vecs (norm text, emb vector({}))").format(sp.dim))
    update = sql.SQL("UPDATE {} t SET emb = i.emb FROM import_vecs i WHERE t.norm = i.norm AND t.emb IS NULL").format(
        sql.Identifier(vec_schema, sp.table))
    pf = pq.ParquetFile(path)
    updated, seen, started = 0, 0, time.monotonic()
    with conn.cursor() as cur:
        # Batch by batch: one 1M-row UPDATE took the 7 GB laptop's Docker VM down (#48). Each batch is committed, so
        # a rerun keeps what is done: it reads the whole file again but changes only rows with emb IS NULL.
        for batch in pf.iter_batches(batch_size=batch_size):
            cur.execute("TRUNCATE import_vecs")
            emb = batch.column("emb").flatten().to_numpy().reshape(-1, sp.dim).astype(np.float32)
            with cur.copy("COPY import_vecs (norm, emb) FROM STDIN") as cp:
                for norm, v in zip(batch.column("norm").to_pylist(), emb, strict=True):
                    cp.write_row((norm, _vec(v)))
            cur.execute(update)
            if not conn.autocommit:
                conn.commit()
            updated, seen = updated + cur.rowcount, seen + batch.num_rows
            log.info("импорт %d/%d (обновлено %d, пропущено %d: уже с вектором или нет в таблице; %.0f строк/с)",
                     seen, pf.metadata.num_rows, updated, seen - updated, _rate(seen, started))
    return updated


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="embeddings of product names → pgvector (D-054)")
    ap.add_argument("--limit", type=int, help="encode at most N texts (subsample first)")
    ap.add_argument("--batch", type=int, default=1024)
    ap.add_argument("--index", action="store_true", help="build the HNSW index after the fill")
    ap.add_argument("--export", type=Path, help="write texts without a vector to this parquet and exit")
    ap.add_argument("--encode", type=Path, help="encode this texts parquet (no database) into --out")
    ap.add_argument("--out", type=Path, default=Path("vecs.parquet"))
    ap.add_argument("--import", dest="import_", type=Path, help="fill missing vectors from this parquet")
    ap.add_argument("--model", default=MODEL, choices=sorted(SPECS),
                    help="model of --export / --encode / --import / --index (its own table)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # HF Hub HEAD requests on model load
    if args.encode:
        enc = model_encoder(prefix=spec(args.model).passage, name=args.model)
        print({"encoded": encode_file(args.encode, args.out, enc, model=args.model), "out": str(args.out)})
        return 0
    url = get_settings().database_url
    if not url.startswith("postgres"):
        print("нужен DATABASE_URL=postgresql://… (docker compose up -d db; make db-load)", file=sys.stderr)
        return 2
    with psycopg.connect(pg_dsn(url), autocommit=True) as conn:
        if args.export:
            print({"exported": export_missing(conn, args.export, model=args.model), "path": str(args.export)})
            return 0
        if args.import_:
            print({"imported": import_file(conn, args.import_, model=args.model)})
            if args.index:
                ensure_hnsw(conn, model=args.model)
            return 0
        if args.model != MODEL:
            print(f"{args.model}: в базе кодируется только {MODEL}; другая модель — --export → --encode → --import",
                  file=sys.stderr)
            return 2
        print(embed(conn, model_encoder(), batch=args.batch, limit=args.limit))
        if args.index:
            ensure_hnsw(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
