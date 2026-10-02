"""#281: the weak-match warning — a short query whose best match is weak (idea of art7235/RLT_hack)."""

from __future__ import annotations

from src.contracts import Query
from src.eval.run import slice_names
from src.match import confidence
from src.match.search import fit_signals, search


def _sig(**kw):
    return {"tokens": 2, "top_bm25": 10.0, "ladder": False, "retriever": "bm25", **kw}


def test_weak_only_for_short_query_with_low_bm25_per_token():
    assert confidence.weak_match(_sig(top_bm25=3.0))  # 1.5 per token: weak
    assert confidence.weak_match(_sig(top_bm25=4.0)) is None  # exactly the threshold is fine
    assert confidence.weak_match(_sig(tokens=confidence.MAX_TOKENS, top_bm25=confidence.MAX_TOKENS * 1.0))
    assert confidence.weak_match(_sig(tokens=confidence.MAX_TOKENS + 1, top_bm25=0.1)) is None  # long: no signal
    assert confidence.weak_match(_sig(tokens=0, top_bm25=0.0)) is None  # no words: nothing to divide by


def test_no_warning_without_signals_or_when_found_not_by_name():
    assert confidence.weak_match(None) is None  # nothing was searched
    assert confidence.weak_match(_sig(top_bm25=0.0, ladder=True)) is None  # the search note already says why


def test_search_records_what_it_found(search_data):
    assert search(Query(query_id="q", text="бумага", as_of=None), k=5)
    sig = fit_signals()
    assert sig and sig["tokens"] == 1 and sig["ladder"] is False and sig["top_bm25"] > 0
    assert sig["retriever"] == "bm25"


def test_signals_do_not_leak_into_the_next_search(search_data):
    """The eval loop runs every case in one context: a query that finds nothing must not inherit the previous
    query's signals (review #281)."""
    search(Query(query_id="a", text="бумага", as_of=None), k=5)
    assert fit_signals() is not None
    search(Query(query_id="b", text="zzzzqq qqqzzz", as_of=None), k=5)
    assert fit_signals() is None


def test_no_warning_off_bm25_and_no_code_advice_when_the_code_is_given():
    assert confidence.weak_match(_sig(top_bm25=0.02, retriever="pg_hybrid")) is None  # RRF scores, not BM25
    assert confidence.weak_match(_sig(top_bm25=1.0)) == confidence.WEAK_TEXT
    assert confidence.weak_match(_sig(top_bm25=1.0), has_code=True) == confidence.WEAK_TEXT_WITH_CODE
    assert "ОКПД2" not in confidence.WEAK_TEXT_WITH_CODE


def test_api_header_only_when_weak(search_data, tmp_path, monkeypatch):
    from urllib.parse import unquote

    from fastapi.testclient import TestClient

    from src.api import app as app_mod

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    monkeypatch.setattr(app_mod, "_engine", None)
    client = TestClient(app_mod.app)
    body = {"query_id": "q", "text": "бумага"}
    monkeypatch.setattr(confidence, "MIN_BM25_PER_TOKEN", 0.0)
    assert "X-Weak-Match" not in client.post("/api/search", json=body).headers
    monkeypatch.setattr(confidence, "MIN_BM25_PER_TOKEN", 1e9)
    r = client.post("/api/search", json=body)
    assert unquote(r.headers["X-Weak-Match"]) == confidence.WEAK_TEXT


def test_eval_slice_by_weak_match():
    assert "weak=yes" in slice_names({"channel": "ЭМ", "weak": True})
    assert "weak=no" in slice_names({"channel": "ЭМ", "weak": False})
    assert not [n for n in slice_names({"channel": "ЭМ"}) if n.startswith("weak")]  # an older result has no field


def test_first_words_cuts_the_text_and_keeps_the_case():
    from datetime import date

    from src.eval.run import first_words
    from src.eval.split import EvalCase

    c = EvalCase(query=Query(query_id="q", text="бумага офисная белая А4 500 листов", okpd2=["17.12"]),
                 relevant_inns=["7801000012"], date=date(2025, 7, 10), grades={"7801000012": 2})
    (short,) = first_words([c], 3)
    assert short.query.text == "бумага офисная белая" and short.query.okpd2 == ["17.12"]
    assert short.relevant_inns == c.relevant_inns and short.grades == c.grades
    assert c.query.text.endswith("листов")  # the input is not changed
