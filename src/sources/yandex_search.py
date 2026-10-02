"""Yandex Search API (Yandex Cloud, paid): web search for suppliers outside organizers' data (#344). Owner: SOURCES.

Every request costs money, so the cache is the point of this module, not an extra:

- Every answer (an empty one too) goes to `SourceCache` under the key `search_url()` — the same question
  within `YANDEX_SEARCH_TTL_S` (30 days by default) is never paid twice. The query is normalized first
  (case, ё, spaces), so «Канцтовары  СПб» and «канцтовары спб» are one question and one payment.
- Next to the parsed answer the raw XML of every live request is stored (`raw_url()`); those rows are
  also the spend ledger: `spent_today()` counts them, and above `YANDEX_SEARCH_DAILY_MAX` live requests a
  day nothing is sent (`BudgetExceeded`) — the stale cache, if any, is returned instead.
- Offline (`YANDEX_SEARCH_OFFLINE=1` or `ENRICH_OFFLINE=1`, tests, demo without internet) — cache only.
- Errors (401, 429, an XML error) are not cached: a failure is not an answer.

A hit is a LEAD, not a fact: title and snippet are what the search engine showed. `inns` are the
10/12-digit numbers of the title and snippet that pass the checksum — a candidate for the ФНС card
(src/sources/web_leads.py, src/enrich), never an Evidence of a company by itself. The cached body is
the plain text of the answer, so src/enrich/evidence.py finds an extracted INN in it literally.

CLI (loads .env; the second identical run is free):
  python -m src.sources.yandex_search "поставщик канцтоваров Санкт-Петербург" [-n 10] [--region 2]
  python -m src.sources.yandex_search --stats          # live requests today / the daily cap
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from src.contracts import inn_is_valid
from src.sources.cache import SourceCache
from src.sources.pacing import HostPacer

ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/search"
SOURCE = "Яндекс Поиск (Search API)"
RAW_PREFIX = "yandex-search-raw://"
TIMEOUT_S = 30.0
BLOCK_COOLDOWN_S = 300.0  # after 429: leave the API alone for 5 minutes
NO_RESULTS_CODE = "15"  # «Искомая комбинация слов нигде не встречается» — an answer, cached as empty
MAX_DOCS = 50  # groups on one page; more is a separate (paid) page


def _env_float(name: str, default: str) -> float:
    return float(os.environ.get(name) or default)


# read at every call: tests, the CLI (.env) and an operator may set them after import
def api_key() -> str:
    return os.environ.get("YANDEX_SEARCH_API_KEY", "").strip()


def folder_id() -> str:
    return os.environ.get("YANDEX_SEARCH_FOLDER_ID", "").strip()


def ttl_s() -> int:
    return int(_env_float("YANDEX_SEARCH_TTL_S", str(30 * 24 * 3600)))


def daily_max() -> int:
    return int(_env_float("YANDEX_SEARCH_DAILY_MAX", "100"))


def offline() -> bool:
    return os.environ.get("YANDEX_SEARCH_OFFLINE") == "1" or os.environ.get("ENRICH_OFFLINE") == "1"


PACER = HostPacer(lambda: _env_float("YANDEX_SEARCH_MIN_INTERVAL_S", "1.0"), BLOCK_COOLDOWN_S)


class YandexSearchError(RuntimeError):
    """The API refused or answered with an error: nothing is cached."""


class BudgetExceeded(YandexSearchError):
    """The daily cap of live requests is reached: no request was sent."""


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
        if error.get("code") == NO_RESULTS_CODE:
            return 0, []
        raise YandexSearchError(f"Яндекс Search API: ошибка {error.get('code')}: {_text(error)}")
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


def spent_today(cache: SourceCache, now: float | None = None) -> int:
    """Live requests since local midnight: the raw rows written today."""
    midnight = datetime.fromtimestamp(time.time() if now is None else now).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    with closing(sqlite3.connect(cache.path)) as c:
        (n,) = c.execute("SELECT COUNT(*) FROM pages WHERE url LIKE ? AND fetched_at >= ?",
                         (RAW_PREFIX + "%", midnight)).fetchone()
    return int(n)


def fetch_raw(query: str, n: int = 10, region: str | None = None, page: int = 0,
              client: httpx.Client | None = None) -> str:
    """One live, paid POST. The decoded XML; YandexSearchError on a refusal (no key, 4xx/5xx)."""
    key = api_key()
    if not key:
        raise YandexSearchError("YANDEX_SEARCH_API_KEY не задан (.env)")
    if PACER.blocked():
        raise YandexSearchError("Яндекс Search API: пауза после 429 ещё не истекла")
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
            # bytes, not str: the body must leave as UTF-8 whatever the console encoding is
            r = client.post(ENDPOINT, content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                            headers={"Authorization": f"Api-Key {key}",
                                     "Content-Type": "application/json; charset=utf-8"})
    finally:
        if own:
            client.close()
    if r.status_code == 429:
        PACER.block()
        raise YandexSearchError("Яндекс Search API: HTTP 429 — пауза 5 минут")
    if r.status_code != 200:
        raise YandexSearchError(f"Яндекс Search API: HTTP {r.status_code}: {r.text[:300]}")
    return base64.b64decode(r.json()["rawData"]).decode("utf-8")


def search(query: str, n: int = 10, region: str | None = None, page: int = 0, *,
           cache: SourceCache | None = None, client: httpx.Client | None = None,
           offline_only: bool | None = None) -> SearchAnswer | None:
    """Cached search. Offline → cache only (stale is fine), None when the question was never asked.

    Live: fresh cache → cache, free; else, within the daily cap, one paid request; the request failed or
    the cap is reached → stale cache if any, else the error.
    """
    if not 1 <= n <= MAX_DOCS:
        raise ValueError(f"n должно быть от 1 до {MAX_DOCS}")
    q = normalize(query)
    cache = cache or SourceCache(ttl_s=ttl_s())
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
        if spent_today(cache) >= daily_max():
            raise BudgetExceeded(f"Яндекс Search API: дневной лимит {daily_max()} живых запросов исчерпан "
                                 "(YANDEX_SEARCH_DAILY_MAX)")
        xml = fetch_raw(q, n, region, page, client)
        found, hits = parse_xml(xml)  # an XML error raises before anything is cached
        cache.put(raw_url(q, n, region, page), xml)
        live = True
        return 200, _body(q, found, hits)

    page_ = cache.fetch(url, fetcher)
    found, hits = _from_body(page_.body)
    return SearchAnswer(q, url, found, hits, page_.fetched_at, from_cache=not live)


def _load_dotenv(path: Path) -> None:
    """KEY=VALUE lines of .env into the environment; a variable already set wins. CLI only."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name and not name.startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Яндекс Search API через кеш (каждый живой запрос платный)")
    ap.add_argument("query", nargs="?")
    ap.add_argument("-n", type=int, default=10, help="результатов (групп) на странице, 1–50")
    ap.add_argument("--region", default=os.environ.get("YANDEX_SEARCH_REGION") or None,
                    help="регион Яндекса (lr), СПб — 2")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--offline", action="store_true", help="только кеш, без сети и без трат")
    ap.add_argument("--stats", action="store_true", help="живых запросов сегодня / дневной лимит")
    args = ap.parse_args(argv)
    _load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    cache = SourceCache(ttl_s=ttl_s())
    if args.stats or not args.query:
        print(f"живых запросов сегодня: {spent_today(cache)} из {daily_max()}; кеш: {cache.path}")
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
