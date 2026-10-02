"""BM25 index over lot positions before as_of (methodology step 2, D-017). Owner: MATCH.

Document = one distinct text among ТРУ.product_name and Извещения.subject of the lots that have
participants published strictly before `as_of` (all lots when as_of is None) — no leak (D-018).
Each document maps to the lots where the text occurs (docs.parquet: doc_id, lot_id).

Texts come from the data store (src/match/store.py: Postgres or ingest parquet — the same rows in the
same bytewise order). The index is built lazily on the first query and cached on disk in INDEX_DIR
(default `indexes/`, outside git) under a key of the ingest manifest (built_at, row counts), as_of
and the tokenizer version: a rebuilt ingest gives a new key, and Postgres loaded from that ingest
shares the index with parquet. In-process the loaded index is kept in memory, so a query never
rebuilds it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import bm25s
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from src.match.store import LotHits, Store, get_store
from src.match.store import processed_dir as processed_dir
from src.match.text import tokenize, tokenizer_version

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def index_root() -> Path:
    d = Path(os.environ.get("INDEX_DIR") or ROOT / "indexes")
    return d if d.is_absolute() else ROOT / d


def pq_path(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def index_key(manifest: dict, as_of: date | None) -> str:
    """Store-independent: Postgres loaded from an ingest carries its manifest (src/db/load.py)."""
    raw = json.dumps(
        [manifest.get("built_at"), manifest.get("rows"), as_of.isoformat() if as_of else None, tokenizer_version()],
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
    as_of: date | None = None
    sidecars: dict = field(default_factory=dict, repr=False, compare=False)  # derived data loaded once (search.py)
    _cached: tuple[np.ndarray, np.ndarray, pa.Array] | None = field(default=None, repr=False, compare=False)

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

    def _maps(self) -> tuple[np.ndarray, np.ndarray, pa.Array]:
        """(lot_id by doc, offsets: lots of doc d = [off[d], off[d+1]), texts) — read once per index."""
        if self._cached is None:
            docs = pq.read_table(self.path / "docs.parquet")  # written sorted by doc_id (_build)
            lots = docs.column("lot_id").to_numpy()
            offsets = np.searchsorted(docs.column("doc_id").to_numpy(), np.arange(self.n_docs + 1))
            texts = pq.read_table(self.path / "texts.parquet", columns=["text"]).column("text").combine_chunks()
            self._cached = (lots, offsets, texts)
        return self._cached

    def lot_hits(self, hits: list[tuple[int, float]]) -> LotHits:
        """Documents -> their lots; a lot keeps its best document (the first one in `hits`: score desc)."""
        lots_by_doc, off, _ = self._maps()
        parts = [(lots_by_doc[off[d]:off[d + 1]], s, d, r) for r, (d, s) in enumerate(hits)]
        if not parts:
            return LotHits(np.empty(0, np.int64), np.empty(0), np.empty(0, np.int32))
        lot = np.concatenate([p[0] for p in parts]).astype(np.int64)
        score = np.concatenate([np.full(len(p[0]), p[1]) for p in parts])
        doc = np.concatenate([np.full(len(p[0]), p[2], np.int32) for p in parts])
        rank = np.concatenate([np.full(len(p[0]), p[3]) for p in parts])
        order = np.lexsort((rank, lot))  # by lot, then by rank: the first row of a lot is its best document
        pick = order[np.unique(lot[order], return_index=True)[1]]
        return LotHits(lot[pick], score[pick], doc[pick])

    def text(self, doc_id: int) -> str:
        return self._maps()[2][doc_id].as_py()


_cache: dict[str, LotIndex] = {}
_lock = threading.Lock()


def _build(store: Store, as_of: date | None, out: Path) -> None:
    rows, end = store.index_rows(as_of)
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
    (out / "meta.json").write_text(
        json.dumps({"n_docs": len(corpus), "n_pairs": len(lot_ids), "as_of": as_of.isoformat() if as_of else None,
                    "data_end": end.isoformat() if end else None, "tokenizer": tokenizer_version()},
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
    as_of = meta.get("as_of")
    return LotIndex(key, path, retriever, vocab, n_docs, date.fromisoformat(end) if end else None,
                    date.fromisoformat(as_of) if as_of else None)


def get_index(store: Store | None, as_of: date | None) -> LotIndex:
    """Cached index for (ingest manifest, as_of): memory, then disk, else built once from the store.

    `store`: a Store or None — the store of the environment (organizers' data only through Store, #293)."""
    if store is None:
        store = get_store()
    key = index_key(store.manifest(), as_of)
    with _lock:
        if key in _cache:
            return _cache[key]
        path = index_root() / f"bm25-{key}"
        if not (path / "meta.json").exists():
            log.info("building BM25 index %s (as_of=%s) from %r", key, as_of, store)
            tmp = path.with_name(path.name + f".tmp{os.getpid()}")
            shutil.rmtree(tmp, ignore_errors=True)
            _build(store, as_of, tmp)
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


def main(argv: list[str] | None = None) -> int:
    """Prebuild the product index (as_of=None) and the eval one (split cutoff), each with its position
    codes (#83): no wait on the first query, with or without an OKPD2 code.

        uv run python -m src.match.index
    """
    import sys
    import time

    import duckdb

    from src.ingest.split_config import split_config
    from src.match import speller
    from src.match.search import prebuild_doc_codes

    for as_of in (None, date.fromisoformat(str(split_config()["cutoff"]))):
        t0 = time.time()
        try:
            store = get_store()
            idx = get_index(store, as_of)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        try:
            codes = prebuild_doc_codes(store, idx)
            where = ("рядом с индексом" if codes.parent == idx.path
                     else f"ВНЕ индекса ({codes}): контейнер его не увидит")
        except (OSError, ValueError, duckdb.Error) as e:  # search falls back to the lot vote (D-045)
            where = f"НЕ построены ({e}) — запрос без кода определит класс по лотам"
        try:  # the query speller's dictionary (#282): next to the index, read-only for the API in make up
            words = f"словарь запроса {len(speller.for_index(idx.path).freq)} словоформ"
        except (OSError, ValueError) as e:
            words = f"словарь запроса НЕ построен ({e}) — построится при первом запросе"
        print(f"as_of={as_of}: {idx.n_docs} документов из {store.name}, {idx.path}; коды позиций {where}; {words} "
              f"({time.time() - t0:.1f} с)")
    return 0


__all__ = ["LotHits", "LotIndex", "clear_memory_cache", "get_index", "index_root", "processed_dir"]


if __name__ == "__main__":
    raise SystemExit(main())
