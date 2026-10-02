"""Reviews of a supplier from 2GIS, on demand (#234, D-109): `GET /api/reviews/{inn}`.

Called when the user asks for them in the card — never for a results page, never in the background: one
supplier, at most a search and three reviews tabs of 2gis.ru (src/sources/gis2.py paces and caches them).
The company is what `enrich()` already knows (its name and corporate contacts, each only if the Evidence check
stands behind it: `strip_unverified`); the answer is `src/enrich/reviews.py`'s result with its own Evidence on
every fact. 2GIS being unreachable is `status: "unavailable"` in a 200: the card of the supplier works without it.
Reviews are not in the score and not in the rating.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from src.api import guard
from src.contracts import inn_is_valid
from src.enrich import enrich as enrich_mod
from src.enrich import reviews
from src.enrich.evidence import strip_unverified
from src.sources.cache import SourceCache
from src.sources.pb_nalog import CaptchaRequired

log = logging.getLogger(__name__)

router = APIRouter()
_slots = guard.LiveSlots(max(1, int(os.environ.get("REVIEWS_LIVE_MAX") or 2)))  # reads of 2gis.ru at once

_LIMITED: dict[int | str, dict[str, Any]] = {
    401: {"description": "нужен ключ API (X-API-Key), если на сервере задан API_KEY"},
    429: {"description": "2ГИС запрашивают другие пользователи; повторите через Retry-After секунд"},
}


@router.get("/api/reviews/{inn}", response_model=reviews.ReviewsResult, dependencies=[Depends(guard.require_api_key)],
            responses=_LIMITED, summary="Отзывы о поставщике из 2ГИС по требованию")
def api_reviews(inn: str) -> reviews.ReviewsResult:
    """Cards of 2GIS that carry the name of the company, how far each is trusted (`basis`, `verified`), and the
    newest reviews of the trusted ones: quote, date, link — every one with its Evidence."""
    if not inn_is_valid(inn):
        raise HTTPException(422, "ИНН не проходит контрольные цифры")
    try:
        try:
            cp = enrich_mod.enrich(inn, offline=True)  # the card the user is looking at is in the cache already
        except KeyError:
            with guard.live_slots.take():
                cp = enrich_mod.enrich(inn)
    except KeyError as e:
        raise HTTPException(404, "контрагент не найден") from e
    except (CaptchaRequired, TimeoutError, httpx.HTTPError) as e:
        raise HTTPException(503, "реестр ФНС сейчас недоступен, кеша по этому ИНН нет") from e
    if not getattr(enrich_mod, "IS_STUB", False):
        cp = strip_unverified(cp, SourceCache().page_text)
    with _slots.take():
        found = reviews.find_reviews(cp)
    return found.model_copy(update={"inn": inn})  # strip_unverified may drop the INN of `cp`; the asked one stays
