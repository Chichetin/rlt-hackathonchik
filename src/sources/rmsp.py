"""ФНС: единый реестр субъектов МСП (rmsp.nalog.ru) — extended search by OKVED + region. Owner: SOURCES.

Discovery channel v0 (#16, research/SOURCES.md «(б) discovery v0», D-036): one POST per
(OKVED prefix, region, subject type) returns the first page of the registry (10 rows, registry order).
`/api/search` reads only page 1; `make predict` (#229) asks pages 2..N with the form's own `page` field
([ПРОВЕРИТЬ] on the live site that the order is stable between pages). No bulk walk of the registry.

What goes to the cache (src/sources/cache.py):
- `search_url(...)` → the answer as flat lines `row.<inn>.<field>: <value>` (values verbatim, only
  JSON escaping removed), so src/enrich/evidence.py can check every extracted value against this text;
- `raw_url(...)` → the cleaned JSON rows, for audit.
Session tokens and row ids are not stored. For ИП rows only registry facts are kept: phone, email
and locality are dropped before anything is cached (152-ФЗ, research/SOURCES.md «Правила»).

403 / 429 / a non-JSON page / `captchaRequired` → Blocked: we stop talking to the host for
BLOCK_COOLDOWN_S and never try to get around it. A timeout or a dead connection pauses the host
for DOWN_COOLDOWN_S: a hanging source is not asked again by every next search.
Requests are paced (MIN_INTERVAL_S). A caller may pass a `deadline` (time.monotonic()): pacing,
connect and read together do not outlive it (connect and read get half of the remaining time each).

`python -m src.sources.rmsp seed <dir>` loads recorded answers (tests/fixtures/rmsp) into the cache:
the e2e demo and an offline demo run on them without network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.contracts import inn_is_valid
from src.sources.cache import CachedPage, SourceCache

BASE = "https://rmsp.nalog.ru"
SOURCE = "ФНС: единый реестр субъектов МСП"
UA = "Mozilla/5.0 (compatible; RLTHack-Discovery/0.1; hackathon, cached, paced)"
MIN_INTERVAL_S = float(os.environ.get("RMSP_MIN_INTERVAL_S", "1.0"))
TIMEOUT_S = float(os.environ.get("RMSP_TIMEOUT_S", "3.0"))
QUICK_TIMEOUT_S = float(os.environ.get("RMSP_QUICK_TIMEOUT_S", "10.0"))  # enrich is not on the 4 s search budget
BLOCK_COOLDOWN_S = 900.0
DOWN_COOLDOWN_S = float(os.environ.get("RMSP_DOWN_COOLDOWN_S", "90"))
MIN_ATTEMPT_S = 0.2  # less time than this before the deadline — do not start a request

NP_TYPES = ("UL", "IP")
# Registry facts kept per row, in this order. Everything else (token, id, flags) is not stored.
UL_FIELDS = ("inn", "ogrn", "nptype", "name_ex", "category", "is_active", "regioncode", "okved1", "okved1name",
             "dtregistry", "dtregistryout", "od2_sschr", "phone", "email")
IP_FIELDS = ("inn", "ogrn", "nptype", "name_ex", "category", "is_active", "regioncode", "okved1", "okved1name",
             "dtregistry", "dtregistryout")
META_FIELDS = ("rowCount", "dtQueryEnd")

_OKVED = re.compile(r"\d{2}(\.\d{1,2}){0,2}", re.ASCII)
_REGION = re.compile(r"\d{2}", re.ASCII)

_lock = threading.Lock()
_last_request = 0.0
_blocked_until = 0.0


class Blocked(RuntimeError):
    """The host refuses us (403/429, captcha, protection page): stop, do not bypass."""


class Paused(Blocked):
    """No request was made: the pause after a refusal or a timeout is still on, or no time is left."""


@dataclass(frozen=True)
class Answer:
    meta: dict[str, str]  # query.okved1 / query.region / query.npType, answer.rowCount, answer.dtQueryEnd
    rows: list[dict[str, str]]


def _check_args(okved: str, region: str, np_type: str) -> None:
    if not _OKVED.fullmatch(okved) or not _REGION.fullmatch(region) or np_type not in NP_TYPES:
        raise ValueError(f"rmsp: bad search arguments okved={okved!r} region={region!r} npType={np_type!r}")


def search_url(okved: str, region: str, np_type: str = "UL", page: int = 1) -> str:
    # Cache key and Evidence url. [ПРОВЕРИТЬ] the public form does not prefill itself from the query
    # string: the user repeats the search with the same three fields. Page 1 keeps the old key (cache).
    _check_args(okved, region, np_type)
    if page < 1:
        raise ValueError(f"rmsp: bad page {page}")
    tail = f"&page={page}" if page > 1 else ""
    return f"{BASE}/search.html?mode=extended&region={region}&npType={np_type}&okved1={okved}{tail}"


def raw_url(okved: str, region: str, np_type: str = "UL", page: int = 1) -> str:
    return search_url(okved, region, np_type, page) + "#raw"


def _scalar(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return " ".join(str(v).split())


def clean_rows(answer: dict[str, Any]) -> list[dict[str, str]]:
    """Rows as {field: text}: known fields only, non-empty values, checksum-valid INN, UL/IP only."""
    out = []
    for row in answer.get("data") or []:
        if not isinstance(row, dict):
            continue
        np_type, inn = row.get("nptype"), str(row.get("inn") or "")
        if np_type not in NP_TYPES or not inn_is_valid(inn) or len(inn) != (10 if np_type == "UL" else 12):
            continue
        fields = UL_FIELDS if np_type == "UL" else IP_FIELDS
        out.append({k: _scalar(row[k]) for k in fields if row.get(k) is not None and _scalar(row[k]) != ""})
    return out


def flatten(okved: str, region: str, np_type: str, answer: dict[str, Any]) -> str:
    lines = [f"query.okved1: {okved}", f"query.region: {region}", f"query.npType: {np_type}"]
    lines += [f"answer.{k}: {_scalar(answer[k])}" for k in META_FIELDS if answer.get(k) is not None]
    for row in clean_rows(answer):
        lines += [f"row.{row['inn']}.{k}: {v}" for k, v in row.items()]
    return "\n".join(lines)


def parse(text: str) -> Answer:
    """Inverse of flatten(): rows keep the registry order."""
    meta: dict[str, str] = {}
    rows: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            continue
        if key.startswith("row."):
            _, inn, field = key.split(".", 2)
            rows.setdefault(inn, {})[field] = value
        else:
            meta[key] = value
    return Answer(meta=meta, rows=list(rows.values()))


def blocked() -> bool:
    """True while the host is paused: after a refusal (15 min) or after a timeout (DOWN_COOLDOWN_S)."""
    return time.monotonic() < _blocked_until


def _pause(seconds: float) -> None:
    global _blocked_until
    _blocked_until = max(_blocked_until, time.monotonic() + seconds)


def _block(reason: str) -> Blocked:
    _pause(BLOCK_COOLDOWN_S)
    return Blocked(f"rmsp.nalog.ru: {reason} — останавливаемся, защиту не обходим")


def _post(client: httpx.Client, data: dict[str, str], deadline: float | None,
          timeout: float | None = None) -> httpx.Response:
    global _last_request
    with _lock:
        wait = max(0.0, MIN_INTERVAL_S - (time.monotonic() - _last_request))
        limit = timeout or TIMEOUT_S
        if deadline is not None:
            limit = min(limit, deadline - time.monotonic() - wait)
            if limit < MIN_ATTEMPT_S:
                raise Paused("rmsp.nalog.ru: на запрос не осталось времени")
        if wait > 0:
            time.sleep(wait)
        try:
            # httpx timeouts are per phase: connect and read share the limit, so both ≤ limit in total
            return client.post(BASE + "/search-proc.json", data=data, timeout=limit / 2)
        except httpx.TransportError:
            _pause(DOWN_COOLDOWN_S)  # timeout / no connection: leave the host alone for a while
            raise
        finally:
            _last_request = time.monotonic()


def _checked(r: httpx.Response) -> dict[str, Any]:
    """The JSON answer, or Blocked on 403/429/captcha/non-JSON, ValueError when `data` is not a list."""
    if r.status_code in (403, 429):
        raise _block(f"HTTP {r.status_code}")
    r.raise_for_status()
    try:
        answer = r.json()
    except ValueError as e:
        raise _block("ответ не JSON (страница защиты?)") from e
    if not isinstance(answer, dict) or answer.get("captchaRequired"):
        raise _block("требуется капча")
    if not isinstance(answer.get("data"), list):
        raise ValueError(f"rmsp.nalog.ru: в ответе нет списка data (ключи: {sorted(answer)[:8]})")
    return answer


def _client(timeout: float | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=timeout or TIMEOUT_S,
        headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/search.html"},
    )


def fetch_raw(okved: str, region: str, np_type: str = "UL", client: httpx.Client | None = None,
              deadline: float | None = None, page: int = 1) -> dict[str, Any]:
    """One live POST: one page (the first by default) of the extended search. Blocked on 403/429/captcha/non-JSON,
    Paused when the host is on pause or the deadline leaves no time (no request is made)."""
    _check_args(okved, region, np_type)
    if blocked():
        raise Paused("rmsp.nalog.ru: пауза после отказа или таймаута источника ещё не истекла")
    own = client is None
    client = client or _client()
    try:
        return _checked(_post(client, {"mode": "extended", "page": str(page), "region": region, "npType": np_type,
                                       "okved1": okved}, deadline))
    finally:
        if own:
            client.close()


def fetch_search(okved: str, region: str, np_type: str = "UL", cache: SourceCache | None = None,
                 client: httpx.Client | None = None, offline: bool = False,
                 deadline: float | None = None, page: int = 1) -> CachedPage | None:
    """Cached answer text of one page. offline → cache only (stale is fine), None when not cached.

    Live: fresh cache → cache; else one POST; POST failed → stale cache if any, else the error.
    An empty answer is cached too, so the same question is not asked again within the TTL.
    """
    cache = cache or SourceCache()
    url = search_url(okved, region, np_type, page)
    if offline:
        return cache.get(url, allow_stale=True)

    def live(_: str) -> tuple[int, str]:
        answer = fetch_raw(okved, region, np_type, client, deadline, page)
        cache.put(raw_url(okved, region, np_type, page), json.dumps(
            {"rowCount": answer.get("rowCount"), "dtQueryEnd": answer.get("dtQueryEnd"), "data": clean_rows(answer)},
            ensure_ascii=False))
        return 200, flatten(okved, region, np_type, answer)

    return cache.fetch(url, live)


# --- quick search by INN (#58): the fallback of enrich() while pb.nalog.ru asks for a captcha --------------

def quick_url(inn: str) -> str:
    # Cache key and Evidence url; [ПРОВЕРИТЬ] the public form is filled by the user with the same INN.
    if not inn_is_valid(inn):
        raise ValueError(f"rmsp: INN {inn!r} fails the checksum")
    return f"{BASE}/search.html?mode=quick&query={inn}"


def quick_flatten(inn: str, answer: dict[str, Any]) -> str:
    """Answer of the quick search as flat lines. Only rows of exactly this INN are kept; their count is
    written as `answer.inn_rows` so that «в реестре МСП не найден» is a fact that is on the page too."""
    rows = [r for r in clean_rows(answer) if r.get("inn") == inn]
    lines = [f"query.inn: {inn}", "query.mode: quick"]
    lines += [f"answer.{k}: {_scalar(answer[k])}" for k in ("dtQueryEnd",) if answer.get(k) is not None]
    lines.append(f"answer.inn_rows: {len(rows)}")
    for row in rows:
        lines += [f"row.{inn}.{k}: {v}" for k, v in row.items()]
    return "\n".join(lines)


def fetch_quick_raw(inn: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """One live POST: quick search by INN (checked 01.10: HTTP 200 JSON, no captcha)."""
    quick_url(inn)
    if blocked():
        raise Blocked("rmsp.nalog.ru: пауза после отказа источника ещё не истекла")
    own = client is None
    client = client or _client(QUICK_TIMEOUT_S)
    try:
        return _checked(_post(client, {"mode": "quick", "page": "1", "query": inn, "pageSize": "10"}, None,
                              QUICK_TIMEOUT_S))
    finally:
        if own:
            client.close()


def fetch_quick(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None,
                offline: bool = False) -> CachedPage | None:
    """Cached quick answer (an empty one too). offline → cache only (stale is fine), None if absent."""
    cache = cache or SourceCache()
    url = quick_url(inn)
    if offline:
        return cache.get(url, allow_stale=True)
    return cache.fetch(url, lambda _: (200, quick_flatten(inn, fetch_quick_raw(inn, client))))


def seed(directory: Path, cache: SourceCache | None = None) -> int:
    """Recorded answers `<npType>_<okved>_<region>.json` → cache (fetched_at = file's `recorded_at`)."""
    cache = cache or SourceCache()
    n = 0
    for path in sorted(directory.glob("*.json")):
        np_type, okved, region = path.stem.split("_")
        answer = json.loads(path.read_text(encoding="utf-8"))
        cache.put(search_url(okved, region, np_type), flatten(okved, region, np_type, answer),
                  fetched_at=float(answer["recorded_at"]))
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="rmsp.nalog.ru: загрузить записанные ответы в кеш источников")
    ap.add_argument("command", choices=("seed",))
    ap.add_argument("directory", type=Path)
    args = ap.parse_args(argv)
    print(f"rmsp: в кеш загружено ответов: {seed(args.directory)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
