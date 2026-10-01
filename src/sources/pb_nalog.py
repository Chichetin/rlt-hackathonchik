"""ФНС «Прозрачный бизнес» (pb.nalog.ru): registry card by INN. Owner: SOURCES.

Protocol (research/SOURCES.md, checked 01.10): 4 POSTs per INN —
search-proc.json (search) → search-proc.json get-response (row with token) →
company-proc.json get-request → company-proc.json get-response (card `vyp`).

What goes to the cache (src/sources/cache.py):
- `card_url(inn)` → the card as flat `key: value` lines (search row + `vyp` scalars). Values are
  copied verbatim, only JSON escaping is removed, so `ООО "БРАСС"` occurs literally and
  src/enrich/evidence.py can check every extracted value against this text (D-028);
- `raw_url(inn)` → the raw JSON answers, for audit.

`captchaRequired: true` or HTTP 400 with a captcha error → CaptchaRequired: we stop, never solve captchas (research/SOURCES.md).
Requests are paced (MIN_INTERVAL_S between any two requests to the host).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import httpx

from src.sources.cache import CachedPage, SourceCache

BASE = "https://pb.nalog.ru"
SOURCE = "ФНС «Прозрачный бизнес»"
UA = "Mozilla/5.0 (compatible; RLTHack-Enrich/0.1; hackathon, cached, paced)"
MIN_INTERVAL_S = float(os.environ.get("PB_MIN_INTERVAL_S", "1.0"))
TIMEOUT_S = 20.0
POLL_TRIES = 5

_lock = threading.Lock()
_last_request = 0.0


class CaptchaRequired(RuntimeError):
    pass


class NotFound(KeyError):
    pass


def card_url(inn: str) -> str:
    # [ПРОВЕРИТЬ] the public search page with the INN; the card page itself needs a session token
    return f"{BASE}/search.html#queryAll={inn}"


def raw_url(inn: str) -> str:
    return f"{BASE}/company-proc.json?inn={inn}#raw"


def _post(client: httpx.Client, path: str, data: dict[str, str]) -> httpx.Response:
    global _last_request
    with _lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            r = client.post(BASE + path, data=data)
        finally:
            _last_request = time.monotonic()
    # Seen live 01.10: the captcha demand is HTTP 400 `{"ERRORS": {"pbSearchCaptcha": [...]}}`,
    # not `captchaRequired` in a 200 body. Without this the worker would count it as a failed
    # task and keep knocking.
    if r.status_code == 400 and "captcha" in r.text.lower():
        raise CaptchaRequired("pb.nalog.ru требует капчу (HTTP 400) — останавливаемся (не решаем)")
    return r


def _json(r: httpx.Response) -> dict[str, Any]:
    r.raise_for_status()
    j = r.json()
    if j.get("captchaRequired"):
        raise CaptchaRequired("pb.nalog.ru требует капчу — останавливаемся (не решаем)")
    return j


def _scalar(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def flatten(row: dict[str, Any], vyp: dict[str, Any]) -> str:
    """Card as text: one `key: value` line per scalar; lists/dicts of people are skipped."""
    lines = [f"search.{k}: {_scalar(v)}" for k, v in row.items() if not isinstance(v, (list, dict))]
    lines += [f"vyp.{k}: {_scalar(v)}" for k, v in vyp.items() if not isinstance(v, (list, dict))]
    return "\n".join(lines)


def fetch_raw(inn: str, client: httpx.Client | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Live fetch: (search row, card). NotFound if the registry has no such INN."""
    own = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT_S,
        headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/"},
    )
    try:
        is_ul = len(inn) == 10
        form = {"mode": "search-ul" if is_ul else "search-ip", "queryAll": "", "queryUl": inn,
                "queryIp": inn, "page": "1", "pageSize": "10"}
        sid = _json(_post(client, "/search-proc.json", form))["id"]
        res = _json(_post(client, "/search-proc.json", {"id": sid, "method": "get-response"}))
        rows = [r for r in (res.get("ul" if is_ul else "ip") or {}).get("data", []) if r.get("inn") == inn]
        if not rows:
            raise NotFound(inn)
        row = rows[0]
        token = row["token"]
        rid = _json(_post(client, "/company-proc.json", {"token": token, "method": "get-request"}))["id"]
        for attempt in range(POLL_TRIES):
            r = _post(client, "/company-proc.json", {"token": token, "id": rid, "method": "get-response"})
            if r.status_code == 200:
                card = _json(r)
                return row, card.get("vyp") or {}
            time.sleep(1.0 + attempt)
        raise TimeoutError(f"pb.nalog.ru: карточка {inn} не готова после {POLL_TRIES} попыток")
    finally:
        if own:
            client.close()


def fetch_card(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None) -> CachedPage:
    """Cached card text (fresh cache → cache; else live; live failed → stale cache if any)."""
    cache = cache or SourceCache()
    url = card_url(inn)

    def live(_: str) -> tuple[int, str]:
        row, vyp = fetch_raw(inn, client)
        cache.put(raw_url(inn), json.dumps({"search": row, "vyp": vyp}, ensure_ascii=False))
        return 200, flatten(row, vyp)

    return cache.fetch(url, live)


def parse(text: str) -> dict[str, str]:
    """Inverse of flatten(): `search.x` / `vyp.x` → value (first occurrence wins)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        k, sep, v = line.partition(": ")
        if sep and k not in out:
            out[k] = v
    return out
