"""Cross-encoder reranking of the retrieved positions (#269, E4): only the order of the top-N changes."""

from src.eval.ce_bench import rank_scores, rerank, rerank_ties

HITS = [(10, 9.0), (11, 7.0), (12, 5.0), (13, 3.0), (14, 1.0)]
CE = {10: -2.0, 11: 4.0, 12: 1.0, 13: 8.0, 14: 9.0}


def docs(hits):
    return [d for d, _ in hits]


def test_rank_scores_keep_order_and_decrease():
    got = rank_scores([7, 3, 5])
    assert docs(got) == [7, 3, 5]
    assert [s for _, s in got] == [1.0, 0.5, 1 / 3]


def test_top_n_sorted_by_ce_tail_kept():
    got = rerank(HITS, CE, 3)
    assert docs(got) == [11, 12, 10, 13, 14]  # 13 and 14 score higher but stay outside the top-3
    assert [s for _, s in got] == [s for _, s in rank_scores(docs(got))]
    assert sorted(docs(got)) == sorted(docs(HITS))  # the document set never changes


def test_n_beyond_list_sorts_everything():
    assert docs(rerank(HITS, CE, 200)) == [14, 13, 11, 12, 10]


def test_ties_keep_retriever_order():
    flat = dict.fromkeys(docs(HITS), 0.5)
    assert docs(rerank(HITS, flat, 5)) == docs(HITS)


def test_fuse_is_between_retriever_and_ce():
    # retriever ranks 10→0 … 14→4; ce ranks 14→0, 13→1, 11→2, 12→3, 10→4.
    # RRF rank sums: 11 = 1+2 (best), 10 = 0+4 and 14 = 4+0 tie (retriever order), 13 = 3+1, 12 = 2+3 (worst)
    assert docs(rerank(HITS, CE, 5, fuse=True)) == [11, 10, 14, 13, 12]
    assert docs(rerank(HITS, CE, 1, fuse=True)) == docs(HITS)  # n=1: nothing to reorder


def test_empty_hits():
    assert rerank([], {}, 100) == []
    assert rerank([], {}, 100, fuse=True) == []


def test_ties_groups_move_together_and_share_a_score():
    hits = [(1, 5.0), (2, 5.0), (3, 4.0), (4, 3.0), (5, 3.0), (6, 2.0), (7, 2.0)]
    ce = {1: 0.0, 2: 1.0, 3: -1.0, 4: 0.5, 5: 3.0, 6: 9.0, 7: 9.0}
    got = rerank_ties(hits, ce, 5)
    # groups in the top-5: {1,2} max 1.0, {3} -1.0, {4,5} max 3.0 -> {4,5}, {1,2}, {3}; tail {6,7} stays last
    assert docs(got) == [4, 5, 1, 2, 3, 6, 7]
    sc = dict(got)
    assert sc[4] == sc[5] > sc[1] == sc[2] > sc[3] > sc[6] == sc[7]


def test_ties_without_ties_is_ce_order():
    assert docs(rerank_ties(HITS, CE, 3)) == docs(rerank(HITS, CE, 3))
    assert rerank_ties([], {}, 10) == []
