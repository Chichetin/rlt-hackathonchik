"""Yandex Search API (Yandex Cloud, paid): web search for suppliers outside organizers' data (#344). Owner: SOURCES.

Every request costs money, so the cache is the point of this module, not an extra:

- Every answer (an empty one too) goes to the cache under the key `search_url()` — the same question
  within `YANDEX_SEARCH_TTL_S` (30 days by default) is never paid twice. The query is normalized first
  (case, ё, spaces), so «Канцтовары  СПб» and «канцтовары спб» are one question and one payment.
- ONE cache for every worktree of the machine: `YANDEX_SEARCH_CACHE`, default `cache/yandex_search.sqlite`
  of the main checkout (a worktree `.claude/worktrees/<name>` resolves to it). An answer paid in a worktree
  stays when the worktree is removed and reaches the demo.
- Spend ledger (`spend` table of the same file): one row per ATTEMPT, written before the request leaves —
  a timeout after sending counts too. `spent_today()` counts the rows since local midnight; at
  `YANDEX_SEARCH_DAILY_MAX` nothing is sent (`BudgetExceeded`) and the stale cache, if any, is returned.
  The same transaction holds the all-time cap of the hackathon, `YANDEX_SEARCH_MAX_REQUESTS` (2500, D-149, #438).
- OFF until the human sets the ceilings (D-149, #438): a live request needs the key AND `YANDEX_MAX_PER_LOT`,
  `YANDEX_MAX_PER_RUN` (positive integers in `.env`); otherwise `Disabled`, no network. Per lot (the caller's
  `lot=`) and per run (this process) are counted in memory (`Caps`); a cached answer counts nowhere.
  Within a process a lock makes «check the cap, pay, store» one step; separate processes share the file
  but not the lock (two of them may each pay once for the same miss — no batch consumer runs in parallel yet).
- Offline (`YANDEX_SEARCH_OFFLINE=1` or `ENRICH_OFFLINE=1`, tests, demo without internet) — cache only.
- Errors (401, 429, timeout, an XML error) are not cached: a failure is not an answer. 429, a timeout and the
  XML rate-limit errors (32, 55) pause the API (`HostPacer.block`): the next request of a batch is not sent.

A hit is a LEAD, not a fact: title and snippet are what the search engine showed. `inns` are the
10/12-digit numbers of the title and snippet that pass the checksum — a candidate for the ФНС card
(src/sources/web_leads.py, src/enrich), never an Evidence of a company by itself. The cached body is
the plain text of the answer, so src/enrich/evidence.py finds an extracted INN in it literally.

CLI (loads .env of the worktree, then of the main checkout; the second identical run is free):
  python -m src.sources.yandex_search "поставщик канцтоваров Санкт-Петербург" [-n 10] [--region 2]
  python -m src.sources.yandex_search --stats          # on/off and why; attempts today, all time, left of 2500
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from src.contracts import inn_is_valid
from src.settings import get_settings, load_dotenv
from src.sources.cache import SourceCache
from src.sources.pacing import HostPacer

ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/search"
SOURCE = "Яндекс Поиск (Search API)"
RAW_PREFIX = "yandex-search-raw://"
TIMEOUT_S = 30.0
BLOCK_COOLDOWN_S = 300.0  # after 429 / a rate-limit error: leave the API alone for 5 minutes
DOWN_COOLDOWN_S = 60.0  # after a timeout or a broken connection
NO_RESULTS_CODE = "15"  # «Искомая комбинация слов нигде не встречается» — an answer, cached as empty
RATE_LIMIT_CODES = frozenset({"32", "55"})  # quota exhausted / too many requests per second
MAX_DOCS = 50  # groups on one page; more is a separate (paid) page
ROOT = Path(__file__).resolve().parents[2]


# read at every call: tests, the CLI (.env) and an operator may set them after import (src/settings.py)
def api_key() -> str:
    return get_settings().yandex_search_api_key


def folder_id() -> str:
    return get_settings().yandex_search_folder_id


def ttl_s() -> int:
    return get_settings().yandex_search_ttl_s


def daily_max() -> int:
    return get_settings().yandex_search_daily_max


def max_requests() -> int:
    return get_settings().yandex_search_max_requests


def _ceiling(raw: str) -> int | None:
    """A ceiling of `.env`: a positive integer; empty or anything else → None (the client stays off)."""
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n > 0 else None


def ceilings() -> tuple[int | None, int | None]:
    """(per lot, per run) set by the human in `.env` (D-149); None — not set or not a positive integer."""
    s = get_settings()
    return _ceiling(s.yandex_max_per_lot), _ceiling(s.yandex_max_per_run)


def disabled_reason() -> str | None:
    """Why no live request may leave, None — the client is on. No network either way."""
    if not api_key():
        return "YANDEX_SEARCH_API_KEY не задан (.env)"
    lot, run = ceilings()
    missing = [name for name, v in (("YANDEX_MAX_PER_LOT", lot), ("YANDEX_MAX_PER_RUN", run)) if v is None]
    if missing:
        return f"выключен: не задан {', '.join(missing)} (целое > 0 в .env, D-149)"
    return None


def offline() -> bool:
    s = get_settings()
    return s.yandex_search_offline or s.enrich_offline


def main_checkout(root: Path = ROOT) -> Path:
    """The main checkout of a worktree `<main>/.claude/worktrees/<name>`; any other root is itself."""
    if root.parent.name == "worktrees" and root.parent.parent.name == ".claude":
        return root.parents[2]
    return root


def cache_path() -> Path:
    return get_settings().yandex_search_cache or main_checkout() / "cache" / "yandex_search.sqlite"


def default_cache() -> SourceCache:
    return SourceCache(cache_path(), ttl_s=ttl_s())


PACER = HostPacer(lambda: get_settings().yandex_search_min_interval_s, BLOCK_COOLDOWN_S)
_LOCK = threading.Lock()  # one «cap check → paid request → cache» at a time in this process


class YandexSearchError(RuntimeError):
    """The API refused or answered with an error: nothing is cached."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code  # the XML error code, when the answer carried one


class BudgetExceeded(YandexSearchError):
    """A cap of live requests is reached (daily, all-time, per lot or per run): no request was sent."""


class Disabled(YandexSearchError):
    """The client is off: no key or no ceilings in `.env` (D-149). No request was sent."""


class Caps:
    """Per-lot and per-run counters of live requests in this process (one run = one process, D-149).

    A cached answer costs nothing and counts nowhere: `check()` and `add()` run only for a request about to
    leave — `check()` before the ledger row (a refused ceiling spends nothing), `add()` after it.
    """

    def __init__(self) -> None:
        self.run = 0
        self.lots: dict[str, int] = {}

    def check(self, lot: str | None) -> None:
        per_lot, per_run = ceilings()
        if per_lot is None or per_run is None:
            raise Disabled(f"Яндекс Search API: {disabled_reason()}")
        if self.run >= per_run:
            raise BudgetExceeded(f"Яндекс Search API: потолок прогона {per_run} живых запросов (YANDEX_MAX_PER_RUN)")
        if lot is not None and self.lots.get(lot, 0) >= per_lot:
            raise BudgetExceeded(f"Яндекс Search API: потолок {per_lot} живых запросов на лот {lot} "
                                 "(YANDEX_MAX_PER_LOT)")

    def add(self, lot: str | None) -> None:
        self.run += 1
        if lot is not None:
            self.lots[lot] = self.lots.get(lot, 0) + 1


RUN_CAPS = Caps()


def reset_run_caps() -> None:
    """A new run within one process (tests, a long-lived worker between batches)."""
    global RUN_CAPS
    RUN_CAPS = Caps()


@dataclass(frozen=True)
class SearchHit:
    position: int  # 1-based, in the order of the answer
    url: str
    domain: str
    title: str
    snippet: str  # headline and passages, highlight tags removed
    inns: tuple[str, ...]  # checksum-valid INNs of title + snippet: candidates, not facts


@dataclass(frozen=True)
class SearchAnswer:
    query: str  # normalized: exactly what was sent
    search_url: str  # cache key and the Evidence url: the same search on yandex.ru
    found: int | None  # «нашлось» of the engine, None when it said nothing
    hits: tuple[SearchHit, ...]
    fetched_at: float
    from_cache: bool


_SPACES = re.compile(r"\s+")
_INN = re.compile(r"(?<!\d)(\d{12}|\d{10})(?!\d)")


def normalize(query: str) -> str:
    q = _SPACES.sub(" ", query.replace("ё", "е").replace("Ё", "Е")).strip().lower()
    if not q:
        raise ValueError("пустой поисковый запрос")
    return q


def search_url(query: str, n: int = 10, region: str | None = None, page: int = 0) -> str:
    """Cache key of a normalized question; also a link a human can open to see the same search."""
    url = f"https://yandex.ru/search/?text={quote(normalize(query))}&numdoc={n}&p={page}"
    return url + (f"&lr={region}" if region else "")


def raw_url(query: str, n: int = 10, region: str | None = None, page: int = 0) -> str:
    return RAW_PREFIX + search_url(query, n, region, page).split("://", 1)[1]


def _text(el: ET.Element | None) -> str:
    return "" if el is None else _SPACES.sub(" ", "".join(el.itertext())).strip()


def parse_xml(xml: str) -> tuple[int | None, list[SearchHit]]:
    """`found` and hits of a FORMAT_XML answer. No results (error 15) → (0, []); other errors → raise."""
    root = ET.fromstring(xml)
    error = root.find("response/error")
    if error is not None:
        code = error.get("code")
        if code == NO_RESULTS_CODE:
            return 0, []
        raise YandexSearchError(f"Яндекс Search API: ошибка {code}: {_text(error)}", code=code)
    found_text = root.findtext("response/found[@priority='all']") or ""
    found = int(found_text) if found_text.isdigit() else None
    hits: list[SearchHit] = []
    for doc in root.iterfind("response/results/grouping/group/doc"):
        title = _text(doc.find("title"))
        parts = [_text(doc.find("headline"))] + [_text(p) for p in doc.iterfind("passages/passage")]
        snippet = " … ".join(p for p in parts if p)
        inns = tuple(dict.fromkeys(m for m in _INN.findall(f"{title} {snippet}") if inn_is_valid(m)))
        hits.append(SearchHit(position=len(hits) + 1, url=_text(doc.find("url")), domain=_text(doc.find("domain")),
                              title=title, snippet=snippet, inns=inns))
    return found, hits


def _body(query: str, found: int | None, hits: list[SearchHit]) -> str:
    """What the cache keeps: plain JSON text (INNs, names literally present for evidence.py)."""
    return json.dumps({"query": query, "found": found,
                       "hits": [{"url": h.url, "domain": h.domain, "title": h.title, "snippet": h.snippet,
                                 "inns": list(h.inns)} for h in hits]}, ensure_ascii=False)


def _from_body(body: str) -> tuple[int | None, tuple[SearchHit, ...]]:
    data = json.loads(body)
    hits = tuple(SearchHit(position=i, url=h["url"], domain=h["domain"], title=h["title"], snippet=h["snippet"],
                           inns=tuple(h["inns"])) for i, h in enumerate(data["hits"], start=1))
    return data["found"], hits


# --- spend ledger: one row per attempt, written before the request leaves ---------------------------------

def _ledger(cache: SourceCache) -> sqlite3.Connection:
    c = sqlite3.connect(cache.path)
    c.execute("CREATE TABLE IF NOT EXISTS spend (id INTEGER PRIMARY KEY AUTOINCREMENT, sent_at REAL NOT NULL, "
              "url TEXT NOT NULL, outcome TEXT NOT NULL)")
    return c


def _midnight(now: float | None = None) -> float:
    return datetime.fromtimestamp(time.time() if now is None else now).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()


def _reserve(cache: SourceCache, url: str, limit: int, total: int | None = None) -> int:
    """Count today's and all rows and insert ours in ONE write transaction: threads and processes cannot both pass.

    `limit` — the daily cap, `total` — the all-time cap of the hackathon (None — `max_requests()`).
    The row stays whatever the request returns (a request that left may be billed). At a cap: no row.
    """
    total = max_requests() if total is None else total
    c = _ledger(cache)
    c.isolation_level = None  # explicit BEGIN IMMEDIATE below
    try:
        c.execute("BEGIN IMMEDIATE")
        try:
            (n,) = c.execute("SELECT COUNT(*) FROM spend WHERE sent_at >= ?", (_midnight(),)).fetchone()
            if n >= limit:
                raise BudgetExceeded(f"Яндекс Search API: дневной лимит {limit} живых запросов исчерпан "
                                     "(YANDEX_SEARCH_DAILY_MAX)")
            (n_all,) = c.execute("SELECT COUNT(*) FROM spend").fetchone()
            if n_all >= total:
                raise BudgetExceeded(f"Яндекс Search API: потолок {total} живых запросов на весь хакатон исчерпан "
                                     "(YANDEX_SEARCH_MAX_REQUESTS)")
            cur = c.execute("INSERT INTO spend (sent_at, url, outcome) VALUES (?, ?, 'sent')", (time.time(), url))
            attempt = int(cur.lastrowid or 0)
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")
        return attempt
    finally:
        c.close()


def _settle(cache: SourceCache, attempt: int, outcome: str) -> None:
    with closing(_ledger(cache)) as c, c:
        c.execute("UPDATE spend SET outcome = ? WHERE id = ?", (outcome[:200], attempt))


def spent_today(cache: SourceCache, now: float | None = None) -> int:
    """Attempts since local midnight — every request that left, whatever came back."""
    with closing(_ledger(cache)) as c:
        (n,) = c.execute("SELECT COUNT(*) FROM spend WHERE sent_at >= ?", (_midnight(now),)).fetchone()
    return int(n)


def spent_total(cache: SourceCache) -> int:
    """Attempts of all days — against the all-time cap `YANDEX_SEARCH_MAX_REQUESTS`."""
    with closing(_ledger(cache)) as c:
        (n,) = c.execute("SELECT COUNT(*) FROM spend").fetchone()
    return int(n)


def _ready() -> None:
    """Nothing to pay for when the request cannot go: the client is off (no key, no ceilings), or on pause."""
    why = disabled_reason()
    if why is not None:
        raise Disabled(f"Яндекс Search API: {why}")
    if PACER.blocked():
        raise YandexSearchError("Яндекс Search API: пауза после отказа или таймаута ещё не истекла")


def fetch_raw(query: str, n: int = 10, region: str | None = None, page: int = 0,
              client: httpx.Client | None = None, *, on_send: Callable[[], None]) -> str:
    """One live, paid POST. The decoded XML; YandexSearchError on a refusal (no key, 4xx/5xx, timeout).

    `on_send` runs inside the pacer slot right before the POST — the spend reservation and the ceilings; it may
    raise. Required: a paid request never leaves without them (use search(), it passes them).
    """
    _ready()
    body: dict[str, object] = {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": normalize(query), "page": str(page)},
        "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(n), "docsInGroup": "1"},
        "responseFormat": "FORMAT_XML",
    }
    if region:
        body["region"] = region
    if folder_id():
        body["folderId"] = folder_id()
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        with PACER.slot():
            on_send()
            # bytes, not str: the body must leave as UTF-8 whatever the console encoding is
            r = client.post(ENDPOINT, content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                            headers={"Authorization": f"Api-Key {api_key()}",
                                     "Content-Type": "application/json; charset=utf-8"})
    except httpx.TransportError as e:
        PACER.block(DOWN_COOLDOWN_S)
        raise YandexSearchError(f"Яндекс Search API: {type(e).__name__} — пауза {DOWN_COOLDOWN_S:.0f} с") from e
    finally:
        if own:
            client.close()
    if r.status_code == 429:
        PACER.block()
        raise YandexSearchError("Яндекс Search API: HTTP 429 — пауза 5 минут")
    if r.status_code != 200:
        raise YandexSearchError(f"Яндекс Search API: HTTP {r.status_code}: {r.text[:300]}")
    try:
        return base64.b64decode(r.json()["rawData"]).decode("utf-8")
    except (ValueError, KeyError, TypeError) as e:
        raise YandexSearchError(f"Яндекс Search API: ответ без rawData: {r.text[:200]}") from e


def search(query: str, n: int = 10, region: str | None = None, page: int = 0, *,
           cache: SourceCache | None = None, client: httpx.Client | None = None,
           offline_only: bool | None = None, lot: str | None = None) -> SearchAnswer | None:
    """Cached search. Offline → cache only (stale is fine), None when the question was never asked.

    Live: fresh cache → cache, free; else, within every cap (per lot `lot` and per run in this process, daily,
    all-time), one paid request; the request failed or a cap is reached → stale cache if any, else the error.
    `lot` — the lot the caller searches for (its per-lot ceiling); None — only the run ceiling applies.
    """
    if not 1 <= n <= MAX_DOCS:
        raise ValueError(f"n должно быть от 1 до {MAX_DOCS}")
    q = normalize(query)
    cache = cache or default_cache()
    url = search_url(q, n, region, page)
    if offline() if offline_only is None else offline_only:
        cached = cache.get(url, allow_stale=True)
        if cached is None:
            return None
        found, hits = _from_body(cached.body)
        return SearchAnswer(q, url, found, hits, cached.fetched_at, from_cache=True)

    live = False

    def fetcher(_: str) -> tuple[int, str]:
        nonlocal live
        _ready()
        attempts: list[int] = []

        def reserve() -> None:  # inside the pacer slot, atomic with the cap check
            RUN_CAPS.check(lot)  # per lot / per run: refused → no ledger row, nothing spent
            attempts.append(_reserve(cache, url, daily_max()))
            RUN_CAPS.add(lot)

        try:
            xml = fetch_raw(q, n, region, page, client, on_send=reserve)
            found, hits = parse_xml(xml)  # an XML error raises before anything is cached
        except YandexSearchError as e:
            if e.code in RATE_LIMIT_CODES:
                PACER.block()
            if attempts:
                _settle(cache, attempts[0], f"error: {e}")
            raise
        except Exception as e:
            if attempts:
                _settle(cache, attempts[0], f"error: {type(e).__name__}")
            raise
        _settle(cache, attempts[0], "ok")
        cache.put(raw_url(q, n, region, page), xml)
        live = True
        return 200, _body(q, found, hits)

    with _LOCK:  # the second of two identical misses waits and then finds the answer in the cache
        page_ = cache.fetch(url, fetcher)
    found, hits = _from_body(page_.body)
    return SearchAnswer(q, url, found, hits, page_.fetched_at, from_cache=not live)


def main(argv: list[str] | None = None) -> int:
    for env in dict.fromkeys((ROOT / ".env", main_checkout() / ".env")):  # the worktree's own first
        load_dotenv(env)
    ap = argparse.ArgumentParser(description="Яндекс Search API через кеш (каждый живой запрос платный)")
    ap.add_argument("query", nargs="?")
    ap.add_argument("-n", type=int, default=10, help="результатов (групп) на странице, 1–50")
    ap.add_argument("--region", default=get_settings().yandex_search_region or None,
                    help="регион Яндекса (lr), СПб — 2")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--offline", action="store_true", help="только кеш, без сети и без трат")
    ap.add_argument("--stats", action="store_true", help="включён ли клиент; живых запросов сегодня и всего")
    args = ap.parse_args(argv)
    cache = default_cache()
    if args.stats or not args.query:
        why = disabled_reason()
        lot, run = ceilings()
        print("клиент: " + (why or f"включён, потолки: на лот {lot}, на прогон {run}"))
        print(f"живых запросов сегодня: {spent_today(cache)} из {daily_max()}; всего: {spent_total(cache)} из "
              f"{max_requests()} (осталось {max(max_requests() - spent_total(cache), 0)}); кеш: {cache.path}")
        return 0
    answer = search(args.query, args.n, args.region, args.page, cache=cache, offline_only=args.offline or None)
    if answer is None:
        print("в кеше нет, а режим офлайн — запрос не отправлен")
        return 1
    origin = "из кеша, бесплатно" if answer.from_cache else "живой запрос (платный)"
    print(f"«{answer.query}»: нашлось {answer.found}; {origin}; "
          f"{datetime.fromtimestamp(answer.fetched_at):%Y-%m-%d %H:%M}; {answer.search_url}")
    for h in answer.hits:
        inns = f"  ИНН-кандидаты: {', '.join(h.inns)}" if h.inns else ""
        print(f"{h.position:>2}. {h.title}\n    {h.url}{inns}")
    print(f"живых запросов сегодня: {spent_today(cache)} из {daily_max()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
