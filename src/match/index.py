"""BM25 index over lot positions before as_of (methodology step 2, D-017). Owner: MATCH.

Document = one distinct text among ТРУ.product_name and Извещения.subject of the lots that have
participants published strictly before `as_of` (all lots when as_of is None) — no leak (D-018).
Each document maps to the lots where the text occurs (docs.parquet: doc_id, lot_id).

The index is built lazily on the first query and cached on disk in INDEX_DIR (default
`indexes/`, outside git) under a key of the ingest manifest (built_at, row counts, path), as_of
and the tokenizer version; a rebuilt ingest gives a new key. In-process the loaded index is
kept in memory, so a query never rebuilds it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import bm25s
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from src.match.text import TOKENIZER_VERSION, tokenize

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("manifest.json", "notices.parquet", "participants.parquet", "items.parquet", "suppliers.parquet")


def _dir(env: str, default: str) -> Path:
    d = Path(os.environ.get(env) or ROOT / default)
    return d if d.is_absolute() else ROOT / d


def processed_dir() -> Path:
    """DATA_PROCESSED_DIR (as src/api/lots.py), default data/processed."""
    return _dir("DATA_PROCESSED_DIR", "data/processed")


def index_root() -> Path:
    return _dir("INDEX_DIR", "indexes")


def check_processed(processed: Path) -> dict:
    """Manifest of the ingest output, or FileNotFoundError with the command that builds it."""
    missing = [f for f in REQUIRED if not (processed / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"нет данных для поиска в {processed}: {', '.join(missing)} — соберите "
            "`uv run python -m src.ingest.build` (DATA_RAW_DIR → DATA_PROCESSED_DIR)"
        )
    return json.loads((processed / "manifest.json").read_text(encoding="utf-8"))


def pq_path(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def index_key(processed: Path, manifest: dict, as_of: date | None) -> str:
    raw = json.dumps(
        [str(processed.resolve()), manifest.get("built_at"), manifest.get("rows"),
         as_of.isoformat() if as_of else None, TOKENIZER_VERSION],
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class LotIndex:
    key: str
    path: Path
    retriever: bm25s.BM25
    vocab: frozenset[str]
    n_docs: int
    data_end: date | None  # latest publish_date in the indexed history

    @property
    def docs_parquet(self) -> str:
        return pq_path(self.path / "docs.parquet")

    def retrieve(self, tokens: list[str], top: int) -> list[tuple[int, float]]:
        """(doc_id, bm25 score) with score > 0, best first; unknown tokens are ignored."""
        known = [t for t in tokens if t in self.vocab]
        if not known or self.n_docs == 0:
            return []
        docs, scores = self.retriever.retrieve([known], k=min(top, self.n_docs), show_progress=False)
        return [(int(d), float(s)) for d, s in zip(docs[0], scores[0], strict=True) if s > 0]


_cache: dict[str, LotIndex] = {}
_lock = threading.Lock()


def _build(processed: Path, as_of: date | None, out: Path) -> None:
    cond = "publish_date < $as_of" if as_of else "TRUE"
    params = {"as_of": as_of} if as_of else {}
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            WITH lots AS (
                SELECT DISTINCT lot_id FROM read_parquet({pq_path(processed / "participants.parquet")})
                WHERE {cond}
            ),
            texts AS (
                SELECT i.lot_id, i.product_name AS t
                FROM read_parquet({pq_path(processed / "items.parquet")}) i JOIN lots USING (lot_id)
                WHERE i.product_name IS NOT NULL
                UNION
                SELECT n.lot_id, n.subject AS t
                FROM read_parquet({pq_path(processed / "notices.parquet")}) n JOIN lots USING (lot_id)
                WHERE n.subject IS NOT NULL
            )
            SELECT t, list(DISTINCT lot_id ORDER BY lot_id) FROM texts GROUP BY t ORDER BY t
            """,
            params,
        ).fetchall()
        data_end = con.execute(
            f"SELECT max(publish_date) FROM read_parquet({pq_path(processed / 'participants.parquet')}) WHERE {cond}",
            params,
        ).fetchone()
    finally:
        con.close()
    corpus: list[list[str]] = []
    texts: list[str] = []
    doc_ids: list[int] = []
    lot_ids: list[int] = []
    for text, lots in rows:
        tokens = tokenize(text)
        if not tokens:
            continue
        d = len(corpus)
        corpus.append(tokens)
        texts.append(text)
        doc_ids.extend([d] * len(lots))
        lot_ids.extend(lots)
    out.mkdir(parents=True)
    retriever = bm25s.BM25()
    if corpus:
        retriever.index(corpus, show_progress=False)
        retriever.save(str(out / "bm25"), show_progress=False)
    pq.write_table(pa.table({"doc_id": pa.array(doc_ids, pa.int32()), "lot_id": pa.array(lot_ids, pa.int64())}),
                   out / "docs.parquet")
    pq.write_table(pa.table({"doc_id": pa.array(range(len(texts)), pa.int32()), "text": texts}),
                   out / "texts.parquet")
    end = data_end[0] if data_end else None
    (out / "meta.json").write_text(
        json.dumps({"n_docs": len(corpus), "n_pairs": len(lot_ids), "as_of": as_of.isoformat() if as_of else None,
                    "data_end": end.isoformat() if end else None, "tokenizer": TOKENIZER_VERSION},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load(key: str, path: Path) -> LotIndex:
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    n_docs = int(meta["n_docs"])
    if n_docs:
        retriever = bm25s.BM25.load(str(path / "bm25"), show_progress=False)
        vocab = frozenset(retriever.vocab_dict)
    else:
        retriever, vocab = bm25s.BM25(), frozenset()
    end = meta.get("data_end")
    return LotIndex(key, path, retriever, vocab, n_docs, date.fromisoformat(end) if end else None)


def get_index(processed: Path, as_of: date | None) -> LotIndex:
    """Cached index for (ingest manifest, as_of): memory, then disk, else built once."""
    manifest = check_processed(processed)
    key = index_key(processed, manifest, as_of)
    with _lock:
        if key in _cache:
            return _cache[key]
        path = index_root() / f"bm25-{key}"
        if not (path / "meta.json").exists():
            log.info("building BM25 index %s (as_of=%s) from %s", key, as_of, processed)
            tmp = path.with_name(path.name + f".tmp{os.getpid()}")
            shutil.rmtree(tmp, ignore_errors=True)
            _build(processed, as_of, tmp)
            shutil.rmtree(path, ignore_errors=True)
            tmp.rename(path)
        idx = _load(key, path)
        if len(_cache) >= 4:
            _cache.pop(next(iter(_cache)))
        _cache[key] = idx
        return idx


def clear_memory_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = ["LotIndex", "check_processed", "clear_memory_cache", "get_index", "index_root", "processed_dir"]
