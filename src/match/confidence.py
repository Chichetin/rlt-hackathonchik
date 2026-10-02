"""Weak-match warning of /api/search (#281; idea — art7235/RLT_hack, «уверенность подбора»).

product_fit is normalized by the best candidate, so the first row always looks like a perfect match.
This is the absolute view: how strongly the best similar lot matches the query words.

What the data says (dev, 2 000 cases cut to ≤ 4 words and in full, hit = any participant of the lot in the
top-10; research/EXPERIMENTS.md, W-281): a SHORT query whose best BM25 per query token is below 2.0 finds
a participant in ~19 % of cases against ~58 % for the rest. For a long query the same signal does not
discriminate (AUC ≈ 0.5), and «few similar lots» is not a sign of a bad answer at all: a narrow query has
FEWER candidates and a HIGHER hit rate. So the warning is only the first case, and says so.
"""

from __future__ import annotations

MAX_TOKENS = 4  # longer queries: BM25 per token tells nothing
MIN_BM25_PER_TOKEN = 2.0
WEAK_TEXT = ("Слабое совпадение с названиями прошлых закупок — проверьте название предмета закупки "
             "или укажите код ОКПД2: рекомендации могут быть неточными.")


def weak_match(signals: dict | None) -> str | None:
    """The sentence for the user when the match is weak, else None. `signals` — src.match.search.fit_signals()."""
    if not signals or signals["ladder"]:  # nothing searched, or found not by name (the search note says so)
        return None
    tokens = signals["tokens"]
    if 0 < tokens <= MAX_TOKENS and signals["top_bm25"] / tokens < MIN_BM25_PER_TOKEN:
        return WEAK_TEXT
    return None
