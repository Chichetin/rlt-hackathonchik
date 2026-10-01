"""#106: reviews in the card — rating only from a cached page, fabricated one dropped, score and order untouched."""

from pathlib import Path

import pytest

from src.enrich.enrich import enrich
from src.enrich.evidence import check_counterparty, strip_unverified
from src.sources import pb_nalog, reviews
from src.sources.cache import SourceCache
from tests.unit.test_api_enrich import CACHED, FIX, UNCACHED, Q, env  # noqa: F401 (env is a fixture)

REV = Path(__file__).resolve().parents[1] / "fixtures" / "reviews" / f"{CACHED}.txt"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "s.sqlite"))
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    c = SourceCache()
    c.put(pb_nalog.card_url(CACHED), (FIX / f"{CACHED}.txt").read_text(encoding="utf-8"))
    return c


def test_parse_takes_whole_lines_and_rejects_out_of_range():
    assert reviews.parse("rating: 4,5\nreviews_count: 7") == {"rating": "rating: 4,5", "reviews": "reviews_count: 7"}
    assert reviews.parse("rating: 9.9") == {}
    assert reviews.parse("my rating: 4.5") == {}  # not a whole line


def test_nothing_cached_nothing_shown(cache):
    cp = enrich(CACHED, cache)
    assert not [e for e in cp.evidence if e.field in ("rating", "reviews")]


def test_cached_rating_passes_evidence(cache):
    cache.put(reviews.page_url(CACHED), REV.read_text(encoding="utf-8"))
    cp = enrich(CACHED, cache)
    got = {e.field: e for e in cp.evidence if e.field in ("rating", "reviews")}
    assert got["rating"].value == "rating: 4.7" and got["reviews"].value == "reviews_count: 12"
    assert got["rating"].url == reviews.page_url(CACHED) and "не влияет" in got["rating"].fact
    assert check_counterparty(cp, cache.page_text) == []


def test_fabricated_rating_is_dropped(cache):
    cache.put(reviews.page_url(CACHED), REV.read_text(encoding="utf-8"))
    cp = enrich(CACHED, cache)
    real = next(e for e in cp.evidence if e.field == "rating")
    fake = real.model_copy(update={"value": "rating: 5.0", "fact": "Рейтинг в отзывах: 5.0 из 5"})
    no_page = real.model_copy(update={"url": "https://2gis.ru/spb/search/other"})
    bad = cp.model_copy(update={"evidence": [*cp.evidence, fake, no_page]})
    assert check_counterparty(bad, cache.page_text)
    shown = strip_unverified(bad, cache.page_text)
    assert fake not in shown.evidence and no_page not in shown.evidence and real in shown.evidence


def test_reviews_do_not_change_score_or_order(env):  # noqa: F811
    client, put, calls = env
    put()
    before = client.post("/api/search", json=Q).json()
    SourceCache().put(reviews.page_url(CACHED), REV.read_text(encoding="utf-8"))
    after = client.post("/api/search", json=Q).json()
    card = next(m["counterparty"] for m in after["history"] if m["counterparty"]["inn"] == CACHED)
    assert any(e["field"] == "rating" for e in card["evidence"])  # shown...
    strip = lambda r: [(m["counterparty"]["inn"], m["score"], m["components"]) for m in r["history"]]  # noqa: E731
    assert strip(before) == strip(after) and strip(after)  # ...but score, components and order are the same
    assert calls == []


def test_stale_page_is_still_used_offline(cache):
    cache.put(reviews.page_url(CACHED), REV.read_text(encoding="utf-8"), fetched_at=1.0)  # 1970: past any TTL
    assert [e.field for e in reviews.from_cache(CACHED, cache)] == ["rating", "reviews"]
