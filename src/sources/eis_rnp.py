"""ЕИС, реестр недобросовестных поставщиков (РНП) by INN → Counterparty.in_rnp. Owner: SOURCES.

`GET zakupki.gov.ru/epz/dishonestsupplier/search/results.html?searchString=<ИНН>` (research/SOURCES.md).
The search also matches INNs of founders and managers, so only entries whose «ИНН (аналог ИНН)»
equals the INN count. An entry with an «Исключено» date is history, not a current listing.

Answer (in_rnp, evidence):
- an own entry without exclusion → True, Evidence = its registry number;
- own entries, all excluded → False, Evidence «был в РНП, исключён»;
- «Поиск не дал результатов» or no own entry on a complete page → False;
- results do not fit one page and no own current entry is on it → None (not checked fully).

robots.txt: `Crawl-delay: 60` → at most one request per MIN_INTERVAL_S (60 s), blocking; for the
final shortlist only. Pages go to SourceCache as is (Evidence values literally occur there);
a cached answer older than MAX_AGE_S is refetched. `ENRICH_OFFLINE=1` — cache only.
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from src.contracts import Evidence, inn_is_valid
from src.sources.cache import CachedPage, SourceCache
from src.sources.tls import gov_ssl_context

BASE = "https://zakupki.gov.ru"
SOURCE = "ЕИС, реестр недобросовестных поставщиков"
UA = "Mozilla/5.0 (compatible; RLTHack-Enrich/0.1; hackathon, cached, paced)"
MIN_INTERVAL_S = float(os.environ.get("RNP_MIN_INTERVAL_S", "60"))  # robots.txt Crawl-delay
MAX_AGE_S = float(os.environ.get("RNP_MAX_AGE_S", str(24 * 3600)))
TIMEOUT_S = 40.0
NO_RESULTS = "Поиск не дал результатов"

_lock = threading.Lock()
_last_request = 0.0

_ENTRY = re.compile(r"search-registry-entry-block.*?(?=search-registry-entry-block|$)", re.S)
_BLOCK = re.compile(r"(?:registry-entry__body-title|data-block__title)\"?>\s*(.*?)\s*</div>\s*"
                    r"<div class=\"(?:registry-entry__body-value|data-block__value)\">\s*(.*?)\s*</div>", re.S)
_NUMBER = re.compile(r"№\s*(\d+)")
_TOTAL = re.compile(r"Результаты поиска\s+(\d+)\s+запис\w*")


class Blocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    number: str  # «№ 24009739» as on the page
    name: str
    inn: str
    included: str | None
    excluded: str | None


@dataclass(frozen=True)
class RnpResult:
    in_rnp: bool | None
    evidence: list[Evidence]


def search_url(inn: str) -> str:
    # no strictEqual=true: 01.10 it answered «Поиск не дал результатов» for an INN with 28 entries
    q = urlencode({"searchString": inn, "recordsPerPage": "_50"})
    return f"{BASE}/epz/dishonestsupplier/search/results.html?{q}"


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse(body: str) -> tuple[list[Entry], int | None, str | None]:
    """Entries on the page, N and the text «Результаты поиска N записей» (None if not shown)."""
    entries = []
    for block in _ENTRY.findall(body):
        fields = {_text(k): _text(v) for k, v in _BLOCK.findall(block)}
        number = _NUMBER.search(_text(block))
        if not number or "ИНН (аналог ИНН)" not in fields:
            continue
        name = fields.get("Наименование (ФИО) недобросовестного поставщика", "")
        entries.append(Entry(number=f"№ {number[1]}", name=name,
                             inn=fields["ИНН (аналог ИНН)"], included=fields.get("Включено"),
                             excluded=fields.get("Исключено")))
    total = _TOTAL.search(_text(body))
    return entries, (int(total[1]) if total else None), (total[0] if total else None)


def _get(client: httpx.Client, url: str) -> tuple[int, str]:
    global _last_request
    with _lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            r = client.get(url)
        finally:
            _last_request = time.monotonic()
    if r.status_code in (403, 429):
        raise Blocked(f"zakupki.gov.ru ответил {r.status_code} — останавливаемся")
    r.raise_for_status()
    if "search-registry-entrys-block" not in r.text and NO_RESULTS not in r.text:
        raise Blocked("zakupki.gov.ru вернул не страницу поиска РНП — останавливаемся")
    return r.status_code, r.text


def fetch(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None) -> CachedPage:
    cache = cache or SourceCache()
    url = search_url(inn)
    hit = cache.get(url, allow_stale=True)
    if os.environ.get("ENRICH_OFFLINE") == "1":
        if hit is None:
            raise KeyError(url)
        return hit
    if hit is not None and time.time() - hit.fetched_at <= MAX_AGE_S:
        return hit
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, verify=gov_ssl_context(), headers={"User-Agent": UA})
    try:
        status, body = _get(client, url)
    except Exception:
        if hit is not None:
            return hit  # stale answer beats none; its fetched_at is shown with the fact
        raise
    finally:
        if own:
            client.close()
    return cache.put(url, body, status)


def check(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None) -> RnpResult:
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    page = fetch(inn, cache, client)
    at = datetime.fromtimestamp(page.fetched_at, tz=UTC)
    day = at.strftime("%d.%m.%Y")

    def ev(value: str, fact: str) -> Evidence:
        return Evidence(source=SOURCE, url=page.url, fetched_at=at, fact=fact, field="rnp", value=value)

    if NO_RESULTS in page.body:
        return RnpResult(False, [ev(NO_RESULTS, f"В РНП ЕИС по ИНН {inn} записей нет (поиск {day})")])
    entries, total, total_text = parse(page.body)
    own = [e for e in entries if e.inn == inn]
    current = [e for e in own if not e.excluded]
    if current:
        e = current[0]
        return RnpResult(True, [ev(e.number, f"В РНП ЕИС: реестровая запись {e.number}, включено {e.included}")])
    complete = total is not None and total <= len(entries)
    if own:
        e = own[0]
        fact = f"Был в РНП ЕИС: запись {e.number}, включено {e.included}, исключено {e.excluded}"
        return RnpResult(False if complete else None, [ev(e.number, fact)])
    if complete:
        return RnpResult(False, [ev(total_text or "",
                                    f"В РНП ЕИС нет записей, где поставщик — ИНН {inn} (поиск {day})")])
    return RnpResult(None, [])
