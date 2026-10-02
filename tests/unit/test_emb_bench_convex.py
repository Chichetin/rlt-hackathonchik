"""Convex fusion of BM25 and vector hits (#313, E5)."""

from src.eval.emb_bench import convex

BM25 = [(10, 9.0), (11, 6.0), (12, 3.0), (13, 1.0)]
VEC = [(13, 0.9), (20, 0.8), (11, 0.7), (21, 0.5)]


def test_alpha_zero_is_bm25_order():
    # vector-only documents come after the BM25 ones (norm 0 ties with the lowest BM25 hit, doc_id order)
    assert [d for d, _ in convex(BM25, VEC, 0.0)] == [10, 11, 12, 13, 20, 21]


def test_alpha_one_is_vector_order():
    got = [d for d, _ in convex(BM25, VEC, 1.0)]
    assert got[:3] == [13, 20, 11]  # the rest (vector norm 0) tie by doc_id


def test_ties_by_doc_id_and_top():
    hits = [(5, 1.0), (3, 1.0)]  # equal scores -> both 1.0 -> doc_id order
    assert [d for d, _ in convex(hits, [], 0.5)] == [3, 5]
    assert len(convex(BM25, VEC, 0.5, top=2)) == 2


def test_empty_branch_keeps_other():
    assert [d for d, _ in convex(BM25, [], 0.7)][:3] == [10, 11, 12]
    assert convex([], [], 0.5) == []
