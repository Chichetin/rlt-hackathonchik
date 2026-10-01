"""ЕИС, реестр недобросовестных поставщиков (РНП) by INN → Counterparty.in_rnp. Owner: SOURCES.

`GET zakupki.gov.ru/epz/dishonestsupplier/search/results.html?searchString=<ИНН>` (research/SOURCES.md).
The search also matches INNs of founders and managers, so only entries whose «ИНН (аналог ИНН)»
equals the INN count. An entry with an «Исключено» date is history, not a current listing.
If the INN occurs as a whole number in an entry block but no entry is recognised as its own (the
cell reads «9717122721 (RU)», a new layout), the answer is None — never «not found».

Answer (in_rnp, evidence). False means «not found in the РНП on the date of the search» — it is
given only on a page that is verifiably a complete answer; whenever the search may not have worked
the answer is None («not checked»), never False:
- an own entry with status «Размещено» → True, Evidence = its registry number;
- own entries, all «Исключено» → False if the page is complete, Evidence «был в РНП, исключён»;
- «Поиск не дал результатов» on a page with no entry blocks and no «Результаты поиска N записей»,
  or a complete page without own entries → False «не найдена в РНП на <дата>» (date in Moscow time);
- results do not fit one page, no total on the page, an empty entries block without
  «Поиск не дал результатов», a contradictory page (that text next to entry blocks or a total),
  the INN inside an entry block but no recognised own entry, or an own entry of unknown status → None.
Error, captcha, 403/429 and a page that is not a РНП search page never reach the answer: `_get`
raises `Blocked` (or an HTTP error), and nothing is cached or turned into «not in РНП».

robots.txt: `Crawl-delay: 60` → at most one request per MIN_INTERVAL_S (60 s, the environment can
only raise it, never lower), blocking; for the
final shortlist only. Pages go to SourceCache as is (Evidence values literally occur there);
a cached answer older than MAX_AGE_S is refetched. `ENRICH_OFFLINE=1` — cache only; `enrich()`
(D-101, #111) passes `offline` explicitly instead so the API (always offline for РНП) and the
worker (live) do not depend on process-wide environment state.
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
from zoneinfo import ZoneInfo

import httpx

from src.contracts import Evidence, inn_is_valid
from src.sources.cache import CachedPage, SourceCache
from src.sources.tls import gov_ssl_context

BASE = "https://zakupki.gov.ru"
SOURCE = "ЕИС, реестр недобросовестных поставщиков"
UA = "Mozilla/5.0 (compatible; RLTHack-Enrich/0.1; hackathon, cached, paced)"
CRAWL_DELAY_S = 60.0  # robots.txt of zakupki.gov.ru: Crawl-delay
MIN_INTERVAL_S = max(CRAWL_DELAY_S, float(os.environ.get("RNP_MIN_INTERVAL_S") or CRAWL_DELAY_S))
MAX_AGE_S = float(os.environ.get("RNP_MAX_AGE_S", str(24 * 3600)))
TIMEOUT_S = 40.0
NO_RESULTS = "Поиск не дал результатов"
MSK = ZoneInfo("Europe/Moscow")  # the date in the fact is the one the user sees on site

_lock = threading.Lock()
_last_request = 0.0

_ENTRY = re.compile(r"search-registry-entry-block.*?(?=search-registry-entry-block|$)", re.S)
_BLOCK = re.compile(r"(?:registry-entry__body-title|data-block__title)\"?>\s*(.*?)\s*</div>\s*"
                    r"<div class=\"(?:registry-entry__body-value|data-block__value)\">\s*(.*?)\s*</div>", re.S)
_STATUS = re.compile(r"registry-entry__header-mid__title[^\"]*\">\s*(.*?)\s*</div>", re.S)
_NUMBER = re.compile(r"№\s*([^\W\d_]{0,3}\d+)")  # 44-ФЗ «№ 26011096», 223-ФЗ «№ Р2621500»
_TOTAL = re.compile(r"Результаты поиска\s+(\d+)\s+запис\w*")


class Blocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    number: str  # «№ 26011096» / «№ Р2621500» as on the page
    name: str
    inn: str
    status: str  # «Размещено» (current) or «Исключено» (history)
    included: str | None
    excluded: str | None

    @property
    def current(self) -> bool:
        return self.status == "Размещено" and not self.excluded

    @property
    def history(self) -> bool:
        return bool(self.excluded) or self.status.startswith("Исключ")


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
        status = _STATUS.search(block)
        entries.append(Entry(number=f"№ {number[1]}", name=name,
                             inn=fields["ИНН (аналог ИНН)"], status=_text(status[1]) if status else "",
                             included=fields.get("Включено"), excluded=fields.get("Исключено")))
    total = _TOTAL.search(_text(body))
    return entries, (int(total[1]) if total else None), (total[0] if total else None)


def mentions(body: str, inn: str) -> int:
    """Entry blocks whose field values contain the INN as a whole number (also «9717122721 (RU)»)."""
    whole = re.compile(rf"(?<!\d){re.escape(inn)}(?!\d)")
    return sum(1 for block in _ENTRY.findall(body)
               if any(whole.search(_text(value)) for _, value in _BLOCK.findall(block)))


def _entry_fact(prefix: str, e: Entry) -> str:
    """One line from the fields the page really has: an absent date or status is left out, not printed as None."""
    parts = [f"{prefix} {e.number}"]
    if e.included:
        parts.append(f"включено {e.included}")
    if e.excluded:
        parts.append(f"исключено {e.excluded}")
    else:  # current or excluded by status: the status is on the page
        parts.append(f"статус «{e.status}»")
    return ", ".join(parts)


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


def fetch(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None,
          offline: bool | None = None) -> CachedPage:
    """`offline` explicit (passed by enrich(), D-101) wins; None falls back to `ENRICH_OFFLINE`
    (CLI / standalone use) — cache only either way, never a live request."""
    cache = cache or SourceCache()
    url = search_url(inn)
    hit = cache.get(url, allow_stale=True)
    if offline if offline is not None else os.environ.get("ENRICH_OFFLINE") == "1":
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


def check(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None,
          offline: bool | None = None) -> RnpResult:
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    page = fetch(inn, cache, client, offline=offline)
    at = datetime.fromtimestamp(page.fetched_at, tz=UTC)
    day = at.astimezone(MSK).strftime("%d.%m.%Y")

    def ev(value: str, fact: str) -> Evidence:
        return Evidence(source=SOURCE, url=page.url, fetched_at=at, fact=fact, field="rnp", value=value)

    entries, total, total_text = parse(page.body)
    if NO_RESULTS in page.body:
        if total is not None or "search-registry-entry-block" in page.body:
            return RnpResult(None, [])  # «no results» next to entry blocks or a total: not a reliable answer
        return RnpResult(False, [ev(NO_RESULTS, f"Не найдена в РНП ЕИС на {day}: поиск по ИНН {inn} записей не дал")])
    own = [e for e in entries if e.inn == inn]
    current = [e for e in own if e.current]
    if current:
        e = current[0]
        return RnpResult(True, [ev(e.number, _entry_fact("В РНП ЕИС: реестровая запись", e))])
    unknown = [e for e in own if not e.history]
    if unknown:  # status is neither «Размещено» nor «Исключено»: do not call it clean
        e = unknown[0]
        status = f"статус «{e.status}»" if e.status else "статус не указан"
        return RnpResult(None, [ev(e.number, f"В РНП ЕИС запись {e.number}, {status} — не определён")])
    if not own and mentions(page.body, inn):  # the INN is in an entry we could not read as ours
        return RnpResult(None, [])
    complete = total is not None and total <= len(entries)
    if own:
        e = own[0]
        return RnpResult(False if complete else None, [ev(e.number, _entry_fact("Был в РНП ЕИС: запись", e))])
    if complete:
        return RnpResult(False, [ev(total_text or "", f"Не найдена в РНП ЕИС на {day}: "
                                    f"в выдаче по ИНН {inn} нет записей с этим ИНН поставщика")])
    return RnpResult(None, [])
