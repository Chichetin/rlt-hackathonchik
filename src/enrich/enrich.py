"""enrich(inn) -> Counterparty with Evidence for every fact. Owner: SOURCES.

# STUB — until SOURCES wires one reliable registry source (status, OKVED, address, MSP),
research/SOURCES.md. Every call logs a warning. Real implementation must go through
src/sources/cache.py (demo works on cache) and pass src/enrich/evidence.py checks.
"""

from __future__ import annotations

import logging

from src.contracts import Counterparty, Query, inn_is_valid
from src.match.search import search

log = logging.getLogger(__name__)


def enrich(inn: str) -> Counterparty:
    log.warning("STUB enrich() called for inn=%s — synthetic data, not a result", inn)
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    for m in search(Query(query_id="stub-enrich", text=inn)):
        if m.counterparty.inn == inn:
            return m.counterparty
    raise KeyError(inn)


IS_STUB = True
