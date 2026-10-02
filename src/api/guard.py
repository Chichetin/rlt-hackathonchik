"""Limits of the endpoints that spend the registries' patience (F-10, #256, D-100).

`POST /api/enrich` feeds the worker, `GET /api/counterparty` calls pb.nalog.ru inside the request: an open flood
of either ends in a captcha, and the enrichment on the venue stops. Three limits (`.env`; the first two are read
at request time, the third at start):

- `API_KEY` — set: both endpoints need the header `X-API-Key` (401 otherwise); empty: open, as in the local demo.
- `ENRICH_QUEUE_MAX` — ceiling of the waiting enrichment tasks for a new INN (429 above it), default 2000.
- `COUNTERPARTY_LIVE_MAX` — requests of `/api/counterparty` served at once, default 2; the rest get 429
  (a card answered from the cache alone takes no slot: the endpoint decides before asking the registries).
"""

from __future__ import annotations

import hmac
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Header, HTTPException

from src.settings import COUNTERPARTY_LIVE_MAX_DEFAULT, ENRICH_QUEUE_MAX_DEFAULT, get_settings

QUEUE_MAX_DEFAULT = ENRICH_QUEUE_MAX_DEFAULT
LIVE_MAX_DEFAULT = COUNTERPARTY_LIVE_MAX_DEFAULT
RETRY_AFTER_S = "30"


def queue_max() -> int:
    return get_settings().enrich_queue_max


def busy(detail: str) -> HTTPException:
    """429 with a hint when to retry: the limits above are about waiting, not about a wrong request."""
    return HTTPException(429, detail, headers={"Retry-After": RETRY_AFTER_S})


def require_api_key(x_api_key: str | None = Header(None, description="ключ API, если на сервере задан API_KEY"),
                    ) -> None:
    expected = get_settings().api_key
    if not expected:
        return
    # bytes: compare_digest raises on a non-ASCII str, and a wrong key must be a 401, not a 500. Starlette decodes the
    # header as latin-1, so latin-1 gives back the bytes the client sent: a UTF-8 key matches its UTF-8 setting
    if x_api_key is None or not hmac.compare_digest(x_api_key.encode("latin-1", "replace"), expected.encode()):
        raise HTTPException(401, "нужен ключ API в заголовке X-API-Key")


class LiveSlots:
    """A non-blocking cap on the requests running at once: a full cap answers 429 instead of queueing threads."""

    def __init__(self, size: int) -> None:
        self._sem = threading.BoundedSemaphore(size)

    @contextmanager
    def take(self) -> Iterator[None]:
        if not self._sem.acquire(blocking=False):
            raise busy("обогащение занято другими запросами, повторите позже")
        try:
            yield
        finally:
            self._sem.release()


live_slots = LiveSlots(get_settings().counterparty_live_max)
