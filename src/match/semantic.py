"""Meaning similarity of the query and a candidate's best position: feature `sem` of the ranker (#268, E1).

`sem` = cosine of the query embedding and the embedding of the candidate's best position (`Candidate.best_text`
— the document of the BM25 index as_of that brought the supplier). Vectors of normalized product names come
from a parquet file (`norm`: string, `emb`: fixed_size_list<float16>, L2-normalized, made with the passage prefix
— src/eval/emb_bench.py, src/db/embed.py), named by the env `SEM_VECTORS`; the query is encoded by the model
of the file's metadata with its query prefix (src/db/embed.py SPECS).

The index has two kinds of documents: position names (in the table) and lot subjects (not in it, ~30% of the
candidates' best documents) — a text without a vector is encoded here by the same model with the passage prefix
of the file and kept in memory.

No leak: the table only answers for the names the caller passes; search() passes `best_text` of candidates,
which come from the index of `as_of` (history strictly before it). A name of the table that is not a candidate's
best position never enters the feature.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from src.db.norm import normalize

log = logging.getLogger(__name__)
ENV = "SEM_VECTORS"
QUERY_CACHE = 4096  # encoded queries kept in memory (an eval or a training run repeats none, the product a few)
Encoder = Callable[[list[str]], np.ndarray]
TEXT_CACHE = 300_000  # encoded texts without a vector in the table (float16, 768 dims: ~460 MB at most)


class Vectors:
    """Normalized name -> L2-normalized vector, all in memory (USER-base: 1.03M x 768 float16 ≈ 1.6 GB)."""

    def __init__(self, norms: list[str], emb: np.ndarray, model: str, prefix: str, path: str = "") -> None:
        if emb.ndim != 2 or len(norms) != emb.shape[0]:
            raise ValueError(f"векторы: {len(norms)} имён и матрица {emb.shape}")
        self.row = {t: i for i, t in enumerate(norms)}
        self.extra: dict[str, np.ndarray] = {}  # texts encoded here (not in the table), normalized
        self._extra_lock = threading.Lock()
        self.emb = emb
        self.model = model
        self.prefix = prefix
        self.path = path

    @classmethod
    def read(cls, path: Path) -> Vectors:
        import pyarrow.parquet as pq

        f = pq.ParquetFile(path)
        meta = {k.decode(): v.decode() for k, v in (f.schema_arrow.metadata or {}).items()}
        if "model" not in meta:
            raise ValueError(f"{path}: в метаданных нет модели векторов (model)")
        dim = f.schema_arrow.field("emb").type.list_size
        values = np.empty((f.metadata.num_rows, dim), dtype=np.float16)  # filled by row group: no second copy
        norms: list[str] = []
        for g in range(f.num_row_groups):
            t = f.read_row_group(g, columns=["norm", "emb"])
            emb = t.column("emb").combine_chunks()
            values[len(norms):len(norms) + len(t)] = emb.values.to_numpy(zero_copy_only=False).reshape(-1, dim)
            norms.extend(t.column("norm").to_pylist())
        log.info("векторы %s: %d имён, модель %s", path, len(norms), meta["model"])
        return cls(norms, values, meta["model"], meta.get("prefix", ""), str(path))

    def similarity(self, query_vec: np.ndarray, texts: list[str], encode: Encoder | None = None) -> list[float]:
        """Cosine of the query vector and each text's vector; a text without one is encoded by `encode`
        (prefix included by the caller), NaN without `encode`."""
        q = np.asarray(query_vec, dtype=np.float32)
        norms = [normalize(t) for t in texts]
        rows = [self.row.get(n, -1) for n in norms]
        sims = [float("nan")] * len(texts)
        found = [i for i, r in enumerate(rows) if r >= 0]
        if found:
            for i, v in zip(found, self.emb[[rows[i] for i in found]].astype(np.float32) @ q, strict=True):
                sims[i] = float(v)
        if encode is not None:
            missing = [i for i, r in enumerate(rows) if r < 0 and norms[i]]
            with self._extra_lock:
                new = sorted({norms[i] for i in missing} - set(self.extra))
                if new:
                    if len(self.extra) + len(new) > TEXT_CACHE:
                        self.extra.clear()
                    for n, v in zip(new, np.asarray(encode(new), dtype=np.float16), strict=True):
                        self.extra[n] = v
                for i in missing:
                    sims[i] = float(self.extra[norms[i]].astype(np.float32) @ q)
        return sims


_lock = threading.Lock()
_vectors: dict[str, Vectors] = {}
_encoders: dict[str, Encoder] = {}
_queries: dict[tuple[str, str], np.ndarray] = {}


def source() -> Path | None:
    raw = os.environ.get(ENV, "").strip()
    return Path(raw) if raw else None


def available() -> bool:
    p = source()
    return p is not None and p.is_file()


def vectors() -> Vectors:
    """The table of `SEM_VECTORS`, read once per process."""
    p = source()
    if p is None or not p.is_file():
        raise FileNotFoundError(f"признак sem: нет файла векторов имён ({ENV}={os.environ.get(ENV, '')!r})")
    with _lock:
        if str(p) not in _vectors:
            _vectors[str(p)] = Vectors.read(p)
        return _vectors[str(p)]


def _encoder(model: str) -> Encoder:
    """The model without a prefix: callers add `query: ` / `passage: ` themselves (one model in memory)."""
    from src.db.embed import model_encoder

    with _lock:
        if model not in _encoders:
            _encoders[model] = model_encoder(prefix="", name=model)
        return _encoders[model]


def _passages(vecs: Vectors) -> Encoder:
    enc = _encoder(vecs.model)
    return lambda texts: enc([vecs.prefix + t for t in texts])


def _query_prefix(vecs: Vectors) -> str:
    from src.db.embed import spec

    try:
        return spec(vecs.model).query
    except ValueError:  # a model outside SPECS: e5-style prefixes go together
        return "query: " if vecs.prefix == "passage: " else ""


def query_vector(text: str, vecs: Vectors) -> np.ndarray:
    key = (vecs.model, normalize(text))
    with _lock:
        hit = _queries.get(key)
    if hit is not None:
        return hit
    v = np.asarray(_encoder(vecs.model)([_query_prefix(vecs) + key[1]])[0], dtype=np.float32)
    with _lock:
        if len(_queries) >= QUERY_CACHE:
            _queries.clear()
        _queries[key] = v
    return v


def similarity(query_text: str, texts: list[str]) -> list[float]:
    """`sem` of each candidate: cosine of the query and its best position (texts from the index as_of)."""
    vecs = vectors()
    return vecs.similarity(query_vector(query_text, vecs), texts, _passages(vecs))


def coverage() -> dict | None:
    """Model, file and rows of the table this process used (eval/results/<id>.json), None if not loaded."""
    with _lock:
        if not _vectors:
            return None
        v = next(iter(_vectors.values()))
        return {"feature": "sem", "model": v.model, "file": v.path, "rows": len(v.row), "encoded_here": len(v.extra)}
