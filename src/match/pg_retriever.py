"""retriever `pg_hybrid` (#39, D-034): BM25 (src/match/index.py) + pgvector kNN, fused by RRF over doc_id.

The vector half: the query `query: normalize(text)` (`embedding_model` of the config, default e5-small;
src/db/embed.py SPECS, #98) → KNN nearest normalized product names in that model's table (HNSW)
→ doc_ids of the SAME BM25 index (same as_of): a name
that is not in that index (only in lots after as_of) is dropped, so no leak. One name may map to
several doc_ids (raw texts differing in case/whitespace); `notices.subject` documents have no vectors
and come only from BM25. The fused list feeds the existing `_retrieve` and scoring of search.py.

Without Postgres, the model or a complete vector table the retriever raises VectorsUnavailable:
search() falls back to BM25 with a warning in the API and fails in eval (search.STRICT_RETRIEVER).
"""

from __future__ import annotations

import logging
import threading
import time

import pyarrow.parquet as pq

from src.db.embed import MODEL, Encoder, model_encoder, nearest, spec
from src.db.norm import normalize
from src.match.index import LotIndex
from src.settings import get_settings

log = logging.getLogger(__name__)
RRF_K = 60
KNN = 1000  # nearest names per query; hnsw.ef_search allows at most 1000
VEC_SCHEMA = "vec"


class VectorsUnavailable(RuntimeError):
    pass


_lock = threading.Lock()
_maps: dict[str, dict[str, list[int]]] = {}  # LotIndex.key -> normalized text -> doc_ids (~1 GB each, keep 2)
_state: dict = {}  # conn, down_until/down_why (negative cache); per model: encoders, coverage
_encoders: dict[str, Encoder] = {}
_coverage: dict[str, dict] = {}  # model -> checked table
RETRY_S = 30  # unavailable vectors: fall back without re-checking the table on every request


def norm_map(idx: LotIndex) -> dict[str, list[int]]:
    """normalized document text → doc_ids of this index (cached per index key, i.e. per as_of)."""
    with _lock:
        if idx.key not in _maps:
            t = pq.read_table(idx.path / "texts.parquet", columns=["doc_id", "text"])
            m: dict[str, list[int]] = {}
            for d, text in zip(t.column("doc_id").to_pylist(), t.column("text").to_pylist(), strict=True):
                m.setdefault(normalize(text), []).append(d)
            if len(_maps) >= 2:
                _maps.pop(next(iter(_maps)))
            _maps[idx.key] = m
        return _maps[idx.key]


def _down(why: str) -> VectorsUnavailable:
    """Forget the connection and remember the failure for RETRY_S (the next check reconnects)."""
    if conn := _state.pop("conn", None):
        conn.close()
    _coverage.clear()
    _state["down_until"], _state["down_why"] = time.monotonic() + RETRY_S, why
    return VectorsUnavailable(why)


def _conn(model: str | None = None):
    """Connection with the model's table checked once (complete and indexed)."""
    name, sp = model or MODEL, spec(model)
    if "conn" in _state and name in _coverage:
        return _state["conn"]
    if time.monotonic() < _state.get("down_until", 0.0):
        raise VectorsUnavailable(_state["down_why"])
    url = get_settings().database_url
    if not url.startswith("postgres"):
        raise _down("DATABASE_URL не указывает на Postgres")
    import psycopg
    from psycopg import sql

    from src.db.load import pg_dsn

    try:
        if "conn" not in _state:
            _state["conn"] = psycopg.connect(pg_dsn(url), autocommit=True, connect_timeout=3)
            _state["conn"].execute(sql.SQL("SET hnsw.ef_search = {}").format(KNN))
        conn = _state["conn"]
        total, missing = conn.execute(sql.SQL("SELECT count(*), count(*) FILTER (WHERE emb IS NULL) FROM {}")
                                      .format(sql.Identifier(VEC_SCHEMA, sp.table))).fetchone()  # type: ignore[misc]
        indexed = conn.execute("SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
                               [VEC_SCHEMA, f"{sp.table}_hnsw"]).fetchone()
    except psycopg.Error as e:  # no server, no table: the hybrid is not available
        raise _down(f"векторы {name} недоступны: {e}") from e
    if total == 0 or missing:
        raise _down(f"векторы {name} заполнены не полностью: {total - missing} из {total} (src.db.embed --import)")
    if not indexed:
        raise _down(f"нет индекса HNSW {VEC_SCHEMA}.{sp.table}_hnsw (src.db.embed --index)")
    _state.pop("down_until", None)
    _coverage[name] = {"model": name, "table": f"{VEC_SCHEMA}.{sp.table}", "rows": total}
    return conn


def _encoder(model: str | None = None) -> Encoder:
    name = model or MODEL
    if name not in _encoders:
        try:
            _encoders[name] = model_encoder(prefix=spec(model).query, name=name)
        except Exception as e:  # no torch / no weights offline: remembered for RETRY_S, not reloaded per query
            raise _down(f"модель {name} недоступна: {e}") from e
    return _encoders[name]


def warm(model: str | None = None) -> dict:
    """Connect, check coverage and load the query model once (FastAPI lifespan, Q-24)."""
    with _lock:
        _conn(model)
        _encoder(model)(["прогрев"])
        return dict(_coverage[model or MODEL])


def coverage(model: str | None = None) -> dict | None:
    return dict(_coverage[model or MODEL]) if (model or MODEL) in _coverage else None


def vector_docs(idx: LotIndex, text: str, k: int = KNN, model: str | None = None) -> list[int]:
    """doc_ids of this index nearest to the query by meaning, best first."""
    import psycopg

    with _lock:
        conn = _conn(model)
        q = _encoder(model)([normalize(text)])[0]
        try:
            rows = conn.execute(*nearest(VEC_SCHEMA, q, k, table=spec(model).table)).fetchall()
        except psycopg.Error as e:  # db restarted / connection lost: fall back now, reconnect later
            raise _down(f"векторный поиск прервался: {e}") from e
    m = norm_map(idx)
    out: list[int] = []
    for norm, _sim in rows:
        out.extend(m.get(norm, ()))
    log.info("pg_hybrid: %d соседей, %d в индексе as_of", len(rows), sum(1 for r in rows if r[0] in m))
    return list(dict.fromkeys(out))


def rrf(bm25_hits: list[tuple[int, float]], vec: list[int], top: int) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: score = Σ 1 / (RRF_K + rank) over both lists (rank from 1)."""
    score: dict[int, float] = {}
    for rank, (d, _s) in enumerate(bm25_hits, start=1):
        score[d] = score.get(d, 0.0) + 1.0 / (RRF_K + rank)
    for rank, d in enumerate(vec, start=1):
        score[d] = score.get(d, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(score.items(), key=lambda x: (-x[1], x[0]))[:top]


def hybrid(idx: LotIndex, text: str, bm25_hits: list[tuple[int, float]],
           top: int, model: str | None = None) -> tuple[list[tuple[int, float]], bool]:
    """(fused hits, whether the vector half contributed any document of this index)."""
    vec = vector_docs(idx, text, model=model)
    return rrf(bm25_hits, vec, top), bool(vec)
