"""Document-to-lot aggregation keeps first-hit provenance and sorted, typed output (#522)."""

from pathlib import Path

import bm25s
import numpy as np
import pyarrow as pa
import pytest

from src.match.index import LotIndex


def _index(doc_lots: list[list[int]]) -> LotIndex:
    idx = LotIndex("test", Path("unused"), bm25s.BM25(), frozenset(), len(doc_lots), None)
    idx._cached = (np.asarray([lot for lots in doc_lots for lot in lots], dtype=np.int64),
                   np.asarray([0, *np.cumsum([len(lots) for lots in doc_lots])], dtype=np.int64),
                   pa.array([""] * len(doc_lots)))
    return idx


def _assert_first_hit(idx: LotIndex, doc_lots: list[list[int]], hits: list[tuple[int, float]]) -> None:
    expected: dict[int, tuple[float, int]] = {}
    for doc, score in hits:
        for lot in doc_lots[doc]:
            expected.setdefault(lot, (score, doc))
    ids = sorted(expected)
    actual = idx.lot_hits(hits)
    np.testing.assert_array_equal(actual.lot_id, np.asarray(ids, dtype=np.int64), strict=True)
    np.testing.assert_array_equal(actual.score, np.asarray([expected[lot][0] for lot in ids], dtype=float), strict=True)
    np.testing.assert_array_equal(actual.doc_id, np.asarray([expected[lot][1] for lot in ids], dtype=np.int32),
                                  strict=True)


@pytest.mark.parametrize("hits", [
    [],
    [(0, 2.0)],  # a document with no lots
    [(0, 4.0), (2, 3.0), (1, 3.0), (3, 1.0)],  # empty boundaries and equal-score docs in hit order
    [(1, 2.0), (1, 1.0), (0, 1.0), (2, 0.5)],  # repeated hit: first occurrence wins
])
def test_first_hit_provenance_and_empty_documents(hits):
    docs = [[], [9, 2, 2], [2, 7, 2**60], []]
    _assert_first_hit(_index(docs), docs, hits)


def test_random_overlaps_match_first_hit_oracle():
    rng = np.random.default_rng(522)
    for _ in range(100):
        docs = [rng.integers(1, 100, size=int(rng.integers(0, 40))).tolist() for _ in range(20)]
        chosen = rng.integers(0, len(docs), size=int(rng.integers(0, 60)))
        scores = np.sort(rng.integers(1, 10, size=len(chosen)).astype(float))[::-1]
        hits = [(int(doc), float(score)) for doc, score in zip(chosen, scores, strict=True)]
        _assert_first_hit(_index(docs), docs, hits)
