"""Supplier reviews for the card: a rating from a recorded page, never a score input. Owner: SOURCES (#106).

Feedback of checkpoint 1: reviews are shown next to the supplier or linked; they do NOT change
coefficients (src/match does not read them; tests/unit/test_reviews.py pins score and order).

What is honest here (research/SOURCES.md «Отзывы», D-066):
- no live scraping of review sites: robots.txt of rusprofile disallows /search, flamp answers 403,
  otzovik disallows search; a captcha means stop. The UI gives search links the USER opens (not
  facts, no Evidence); they are built in src/ui/src/reviews.ts from the name and the INN;
- a rating is shown only when it is read from a page that is in the source cache (recorded by a person
  or `seed`) and the value literally occurs there: lines `rating: <n>` and `reviews_count: <n>` are the
  Evidence values, so src/enrich/evidence.py checks them like any other fact. Nothing cached — nothing shown.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import quote

from src.contracts import Evidence
from src.sources.cache import SourceCache

SOURCE = "Отзывы: запись страницы с рейтингом"
_RATING = re.compile(r"^rating:\s*(\d(?:[.,]\d)?)\s*$", re.M)
_COUNT = re.compile(r"^reviews_count:\s*(\d{1,6})\s*$", re.M)


def page_url(inn: str) -> str:
    """Where a recorded review page of this INN is looked up in the cache (2GIS search by INN)."""
    return f"https://2gis.ru/spb/search/{quote(inn)}"


def parse(text: str) -> dict[str, str]:
    """Whole lines `rating: 4.7` / `reviews_count: 12` -> {"rating": "rating: 4.7", ...} (the page lines)."""
    out: dict[str, str] = {}
    m = _RATING.search(text)
    if m and 0 <= float(m.group(1).replace(",", ".")) <= 5:
        out["rating"] = f"rating: {m.group(1)}"
    m = _COUNT.search(text)
    if m:
        out["reviews"] = f"reviews_count: {m.group(1)}"
    return out


def from_cache(inn: str, cache: SourceCache) -> list[Evidence]:
    """Rating and review count of the INN from the cached page; [] when nothing is cached. Never the network."""
    page = cache.get(page_url(inn), allow_stale=True)
    if page is None:
        return []
    at = datetime.fromtimestamp(page.fetched_at, tz=UTC)
    f = parse(page.body)
    out = []
    if "rating" in f:
        out.append(Evidence(source=SOURCE, url=page.url, fetched_at=at, field="rating", value=f["rating"],
                            fact=f"Рейтинг в отзывах: {f['rating'].split(': ')[1]} из 5 (не влияет на оценку подбора)"))
    if "reviews" in f:
        out.append(Evidence(source=SOURCE, url=page.url, fetched_at=at, field="reviews", value=f["reviews"],
                            fact=f"Число отзывов: {f['reviews'].split(': ')[1]} (не влияет на оценку подбора)"))
    return out
