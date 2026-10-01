"""Guards of the two endpoints that reach the registries (F-10, #256): API key, queue ceiling, live-call limit.

    API_KEY             set → POST /api/enrich and GET /api/counterparty need `X-API-Key` (401 otherwise);
                        unset → open, as in the local demo. Compared in constant time.
    ENRICH_QUEUE_MAX    pending enrichment tasks one explicit request may add to (default 2000); beyond → 429
    COUNTERPARTY_LIVE_MAX  simultaneous live (network) enrichments in /api/counterparty (default 2); beyond → 429

Read from the environment at request time, so a changed `.env` needs a restart and a test needs no reload.
"""

from __future__ import annotations

import hmac
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Header, HTTPException

QUEUE_MAX_DEFAULT = 2000
LIVE_MAX_DEFAULT = 2
RETRY_QUEUE_S = 60
RETRY_LIVE_S = 5

_live_lock = threading.Lock()
_live_active = 0


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "")))
    except ValueError:
        return default


def queue_max() -> int:
    return _int_env("ENRICH_QUEUE_MAX", QUEUE_MAX_DEFAULT)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("API_KEY", "")
    if not expected:
        return
    if x_api_key is None or not hmac.compare_digest(x_api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "нужен заголовок X-API-Key", headers={"WWW-Authenticate": "ApiKey"})


def too_many(detail: str, retry_after_s: int) -> HTTPException:
    return HTTPException(429, detail, headers={"Retry-After": str(retry_after_s)})


@contextmanager
def live_slot() -> Iterator[None]:
    """One of COUNTERPARTY_LIVE_MAX simultaneous live enrichments; no free slot → 429 at once, no waiting."""
    global _live_active
    limit = _int_env("COUNTERPARTY_LIVE_MAX", LIVE_MAX_DEFAULT)
    with _live_lock:
        if _live_active >= limit:
            raise too_many("слишком много живых обращений к реестрам, повторите позже", RETRY_LIVE_S)
        _live_active += 1
    try:
        yield
    finally:
        with _live_lock:
            _live_active -= 1
