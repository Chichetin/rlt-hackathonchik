"""ФНС: ГИР БО (bo.nalog.gov.ru) — search by INN, the second fallback of enrich(). Owner: SOURCES.

One GET per INN (checked 01.10: HTTP 200 JSON, no captcha, `Accept: application/json`):
`/advanced-search/organizations/search?query=<INN>&page=0` → `content[]` with the short name, OGRN,
status, main OKVED2, region. Only legal entities that file accounting statements are there.

Cache (src/sources/cache.py): the answer as flat lines `row.<inn>.<field>: <value>`, values verbatim
(the `<strong>` highlighting around the INN removed), so src/enrich/evidence.py can check every value.
403 / 429 / non-JSON → Blocked: we stop talking to the host for BLOCK_COOLDOWN_S, never get around it.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx

from src.contracts import inn_is_valid
from src.sources.cache import CachedPage, SourceCache

BASE = "https://bo.nalog.gov.ru"
SOURCE = "ФНС: ГИР БО (бухгалтерская отчётность)"
UA = "Mozilla/5.0 (compatible; RLTHack-Enrich/0.1; hackathon, cached, paced)"
MIN_INTERVAL_S = float(os.environ.get("BO_MIN_INTERVAL_S", "1.0"))
TIMEOUT_S = float(os.environ.get("BO_TIMEOUT_S", "5.0"))
BLOCK_COOLDOWN_S = 900.0
FIELDS = ("shortName", "ogrn", "statusCode", "statusDate", "okved2", "region")

_lock = threading.Lock()
_last_request = 0.0
_blocked_until = 0.0


class Blocked(RuntimeError):
    """The host refuses us (403/429, protection page): stop, do not bypass."""


def search_url(inn: str) -> str:
    if not inn_is_valid(inn):
        raise ValueError(f"bo: INN {inn!r} fails the checksum")
    return f"{BASE}/advanced-search/organizations/search?query={inn}&page=0"


def blocked() -> bool:
    return time.monotonic() < _blocked_until


def _block(reason: str) -> Blocked:
    global _blocked_until
    _blocked_until = time.monotonic() + BLOCK_COOLDOWN_S
    return Blocked(f"bo.nalog.gov.ru: {reason} — останавливаемся, защиту не обходим")


def _text(v: Any) -> str:
    return " ".join(str(v).split())


def flatten(inn: str, answer: dict[str, Any]) -> str:
    """Rows of exactly this INN (the search is full-text), known fields only, non-empty values."""
    lines = [f"query.inn: {inn}"]
    n = 0
    for item in answer.get("content") or []:
        if not isinstance(item, dict):
            continue
        item_inn = _text(item.get("inn") or "").replace("<strong>", "").replace("</strong>", "")
        if item_inn != inn:
            continue
        n += 1
        lines += [f"row.{inn}.{k}: {_text(item[k])}" for k in FIELDS if item.get(k) not in (None, "")]
    lines.insert(1, f"answer.inn_rows: {n}")
    return "\n".join(lines)


def parse(text: str) -> dict[str, str]:
    """Inverse of flatten(): field → value of the first row (empty dict: not found)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.startswith("row."):
            out.setdefault(key.split(".", 2)[2], value)
    return out


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": UA, "Accept": "application/json"})


def fetch_raw(inn: str, client: httpx.Client | None = None) -> dict[str, Any]:
    global _last_request
    search_url(inn)
    if blocked():
        raise Blocked("bo.nalog.gov.ru: пауза после отказа источника ещё не истекла")
    own = client is None
    client = client or _client()
    try:
        with _lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                r = client.get(f"{BASE}/advanced-search/organizations/search", params={"query": inn, "page": "0"})
            finally:
                _last_request = time.monotonic()
        if r.status_code in (403, 429):
            raise _block(f"HTTP {r.status_code}")
        r.raise_for_status()
        try:
            answer = r.json()
        except ValueError as e:
            raise _block("ответ не JSON (страница защиты?)") from e
        if not isinstance(answer, dict) or not isinstance(answer.get("content"), list):
            raise _block("в ответе нет списка content (страница защиты?)")
        return answer
    finally:
        if own:
            client.close()


def fetch(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None,
          offline: bool = False) -> CachedPage | None:
    """Cached answer (an empty one too). offline → cache only (stale is fine), None if absent."""
    cache = cache or SourceCache()
    url = search_url(inn)
    if offline:
        return cache.get(url, allow_stale=True)
    return cache.fetch(url, lambda _: (200, flatten(inn, fetch_raw(inn, client))))
