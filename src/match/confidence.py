"""Weak-match warning of /api/search (#281; idea — art7235/RLT_hack, «уверенность подбора»).

product_fit is normalized by the best candidate, so the first row always looks like a perfect match.
This is the absolute view: how strongly the best similar lot matches the query words.

Measured (research/EXPERIMENTS.md W-281, eval/results/W-281-short.json and W-281-full.json, 400 dev cases):
a query cut to 3 words that gets the warning has recall@10 0.172 against 0.400 for the rest; on full lot names
the warning fires in 4 of 400 cases. The threshold was chosen on dev queries before that run (a preliminary,
unsaved look), so it is not an independent check. «Few similar lots» did not look like a sign of a bad answer
(preliminary, no saved result: narrow queries seemed to have fewer candidates and a higher hit rate), so the warning
is only about a weak match of a short query.
"""

from __future__ import annotations

MAX_TOKENS = 4  # longer queries: BM25 per token tells nothing
MIN_BM25_PER_TOKEN = 2.0
WEAK_TEXT = ("Слабое совпадение с названиями прошлых закупок — проверьте название предмета закупки "
             "или укажите код ОКПД2: рекомендации могут быть неточными.")
WEAK_TEXT_WITH_CODE = ("Слабое совпадение с названиями прошлых закупок — проверьте название предмета закупки: "
                       "рекомендации могут быть неточными.")


def weak_match(signals: dict | None, has_code: bool = False) -> str | None:
    """The sentence for the user when the match is weak, else None. `signals` — src.match.search.fit_signals();
    `has_code` — the query already has an OKPD2 code, so the advice to give one is dropped."""
    if not signals or signals["ladder"]:  # nothing searched, or found not by name (the search note says so)
        return None
    if signals.get("retriever", "bm25") != "bm25":  # pg_hybrid keeps RRF scores, not BM25: the threshold is BM25's
        return None
    tokens = signals["tokens"]
    if 0 < tokens <= MAX_TOKENS and signals["top_bm25"] / tokens < MIN_BM25_PER_TOKEN:
        return WEAK_TEXT_WITH_CODE if has_code else WEAK_TEXT
    return None
