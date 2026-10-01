"""Yandex Search API v2 (Yandex Cloud, paid) — web search for discovery in thin categories. Owner: SOURCES.

#194, human confirmation №4 of 01.10.2026: the key is in `.env` (YANDEX_SEARCH_API_KEY), and there is a
HARD LIMIT of YANDEX_SEARCH_MAX_REQUESTS (2500) requests for the whole hackathon.

    POST https://searchapi.api.cloud.yandex.net/v2/web/search   Authorization: Api-Key <key>
    body: query.searchType / query.queryText, region, page, groupSpec, responseFormat=FORMAT_XML
    answer: {"rawData": <base64 XML>}; documents are <group><doc><url/><domain/><title/><passages/>

Budget (`Budget`): every live request is logged in its own SQLite file BEFORE it is sent (a failed
request counts too: we never under-count a paid API); the (limit+1)-th request raises BudgetExceeded and
makes no network call. The file is shared by all worktrees of one checkout (`budget_path()`), so a
second worktree does not start from zero. A cached answer costs nothing.

What goes to the cache (src/sources/cache.py):
- `search_url(...)` — a human-checkable yandex.ru/search URL: the answer as flat lines
  `doc.<n>.url|domain|title|passage: <value>` (src/enrich/evidence.py checks a «site» fact here);
- `search_url(...) + "#raw"` — the XML, for audit.
401/403 → Blocked (key or rights), 429 → Blocked with a cooldown: we stop, never retry around it.

`python -m src.sources.yandex_search budget` — requests used / left; `budget --record N --note …` — count
requests made outside this module (the probe of 01.10).
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from src.sources.cache import CachedPage, SourceCache

ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/search"
SOURCE = "Яндекс Поиск (Yandex Search API)"
REGION_SPB = "2"  # Yandex region id of Санкт-Петербург
DOCS_PER_PAGE = 10  # one document per domain (GROUP_MODE_DEEP, docsInGroup=1)
MIN_INTERVAL_S = float(os.environ.get("YANDEX_MIN_INTERVAL_S", "0.5"))
TIMEOUT_S = float(os.environ.get("YANDEX_TIMEOUT_S", "30"))
BLOCK_COOLDOWN_S = 900.0
DEFAULT_LIMIT = 2500

ROOT = Path(__file__).resolve().parents[2]

_lock = threading.Lock()
_last_request = 0.0
_blocked_until = 0.0


class Blocked(RuntimeError):
    """The API refuses us (bad key, no rights, rate limit): stop, do not retry around it."""


class BudgetExceeded(Blocked):
    """The hard request limit is reached: no request was made."""


def main_root(root: Path = ROOT) -> Path:
    """The main checkout of this repository: a worktree's `.git` is a file `gitdir: <main>/.git/worktrees/<n>`."""
    git = root / ".git"
    if git.is_file():
        line = git.read_text(encoding="utf-8").strip()
        if line.startswith("gitdir:"):
            return Path(line.split(":", 1)[1].strip()).resolve().parents[2]
    return root


def _dotenv(name: str) -> str | None:
    """A value from `.env` of this checkout or of the main one: nothing in the project loads .env itself."""
    for base in dict.fromkeys((ROOT, main_root())):
        path = base / ".env"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"').strip("'") or None
    return None


def setting(name: str) -> str | None:
    return os.environ.get(name) or _dotenv(name)


def api_key() -> str:
    key = setting("YANDEX_SEARCH_API_KEY")
    if not key or key.startswith("your_"):
        raise RuntimeError("YANDEX_SEARCH_API_KEY не задан (.env): запрос к Yandex Search API не отправлен")
    return key


def request_limit() -> int:
    return int(setting("YANDEX_SEARCH_MAX_REQUESTS") or DEFAULT_LIMIT)


def budget_path() -> Path:
    """YANDEX_BUDGET_DB, default <main checkout>/cache/yandex_budget.sqlite (one counter for all worktrees)."""
    p = setting("YANDEX_BUDGET_DB")
    return Path(p) if p else main_root() / "cache" / "yandex_budget.sqlite"


class Budget:
    """Persistent log of live requests; `take()` is atomic across processes (BEGIN IMMEDIATE)."""

    def __init__(self, path: Path | str | None = None, limit: int | None = None):
        self.path = Path(path) if path else budget_path()
        self.limit = request_limit() if limit is None else limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "at REAL NOT NULL, note TEXT NOT NULL)")

    def used(self) -> int:
        with closing(sqlite3.connect(self.path)) as c:
            return int(c.execute("SELECT count(*) FROM requests").fetchone()[0])

    def left(self) -> int:
        return max(0, self.limit - self.used())

    def take(self, note: str) -> int:
        """Count one request before it is sent; BudgetExceeded (no request) when the limit is reached."""
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=30)) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                used = int(c.execute("SELECT count(*) FROM requests").fetchone()[0])
                if used >= self.limit:
                    raise BudgetExceeded(f"Yandex Search API: лимит {self.limit} запросов исчерпан, запрос не послан")
                c.execute("INSERT INTO requests (at, note) VALUES (?, ?)", (time.time(), note))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return used + 1

    def record(self, n: int, note: str) -> None:
        """Requests made outside this module (they were paid for too)."""
        with closing(sqlite3.connect(self.path)) as c, c:
            c.executemany("INSERT INTO requests (at, note) VALUES (?, ?)", [(time.time(), note)] * n)


@dataclass(frozen=True)
class Doc:
    url: str
    domain: str
    title: str
    passage: str


@dataclass(frozen=True)
class Answer:
    meta: dict[str, str]
    docs: list[Doc]


def search_url(text: str, page: int = 0, region: str = REGION_SPB) -> str:
    """Cache key and Evidence url: the same search a person can repeat on yandex.ru."""
    if not text.strip() or page < 0 or not region.isdigit():
        raise ValueError(f"yandex: bad search arguments text={text!r} page={page} region={region!r}")
    return f"https://yandex.ru/search/?text={quote(text.strip())}&lr={region}&p={page}"


def _one_line(s: str) -> str:
    return " ".join(s.split())


def _text(el: ET.Element | None) -> str:
    return _one_line("".join(el.itertext())) if el is not None else ""


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def parse_xml(xml: str) -> tuple[str | None, list[Doc]]:
    """(found, docs) from the XML answer; an <error> answer (nothing found) gives no docs."""
    root = ET.fromstring(xml)
    found = root.find(".//found[@priority='all']")
    docs = []
    for doc in root.iter("doc"):
        url = _text(doc.find("url"))
        if not url.startswith(("http://", "https://")):
            continue
        passage = " … ".join(_text(p) for p in doc.iter("passage") if _text(p))
        docs.append(Doc(url=url, domain=_text(doc.find("domain")).lower() or domain_of(url),
                        title=_text(doc.find("title")), passage=passage))
    return (found.text.strip() if found is not None and found.text else None), docs


def flatten(text: str, page: int, region: str, xml: str) -> str:
    found, docs = parse_xml(xml)
    lines = [f"query.text: {_one_line(text)}", f"query.region: {region}", f"query.page: {page}"]
    if found is not None:
        lines.append(f"answer.found: {found}")
    for n, d in enumerate(docs, 1):
        lines += [f"doc.{n}.url: {d.url}", f"doc.{n}.domain: {d.domain}"]
        lines += [f"doc.{n}.{k}: {v}" for k, v in (("title", d.title), ("passage", d.passage)) if v]
    return "\n".join(lines)


def parse(body: str) -> Answer:
    """Inverse of flatten(): documents keep the search order."""
    meta: dict[str, str] = {}
    docs: dict[str, dict[str, str]] = {}
    for line in body.splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            continue
        m = re.fullmatch(r"doc\.(\d+)\.(\w+)", key)
        if m:
            docs.setdefault(m.group(1), {})[m.group(2)] = value
        else:
            meta[key] = value
    return Answer(meta=meta, docs=[Doc(url=d["url"], domain=d.get("domain", domain_of(d["url"])),
                                       title=d.get("title", ""), passage=d.get("passage", ""))
                                   for _, d in sorted(docs.items(), key=lambda kv: int(kv[0])) if "url" in d])


def blocked() -> bool:
    return time.monotonic() < _blocked_until


def _block(reason: str) -> Blocked:
    global _blocked_until
    _blocked_until = time.monotonic() + BLOCK_COOLDOWN_S
    return Blocked(f"Yandex Search API: {reason} — останавливаемся")


def body(text: str, page: int, region: str) -> dict:
    return {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": text.strip()}, "region": region, "page": str(page),
            "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(DOCS_PER_PAGE), "docsInGroup": "1"},
            "responseFormat": "FORMAT_XML"}


def fetch_raw(text: str, page: int = 0, region: str = REGION_SPB, client: httpx.Client | None = None,
              budget: Budget | None = None) -> str:
    """One live request → the XML text. The budget is taken first: BudgetExceeded means nothing was sent."""
    global _last_request
    search_url(text, page, region)
    if blocked():
        raise Blocked("Yandex Search API: пауза после отказа ещё не истекла")
    key = api_key()
    budget = budget or Budget()
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        with _lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
            if wait > 0:
                time.sleep(wait)
            budget.take(f"p={page} lr={region} {text.strip()}")
            try:
                r = client.post(ENDPOINT, json=body(text, page, region),
                                headers={"Authorization": f"Api-Key {key}"})
            finally:
                _last_request = time.monotonic()
        if r.status_code in (401, 403):
            raise _block(f"HTTP {r.status_code} (ключ или права)")
        if r.status_code == 429:
            raise _block("HTTP 429 (лимит частоты)")
        r.raise_for_status()
        raw = r.json().get("rawData")
        if not isinstance(raw, str):
            raise ValueError("Yandex Search API: в ответе нет rawData")
        return base64.b64decode(raw).decode("utf-8")
    finally:
        if own:
            client.close()


def search(text: str, page: int = 0, region: str = REGION_SPB, cache: SourceCache | None = None,
           client: httpx.Client | None = None, offline: bool = False,
           budget: Budget | None = None) -> CachedPage | None:
    """Cached answer text. offline → cache only (stale is fine), None when not cached.
    Live: fresh cache → cache (free); else one paid request; it failed → stale cache if any, else the error."""
    cache = cache or SourceCache()
    url = search_url(text, page, region)
    if offline:
        return cache.get(url, allow_stale=True)

    def live(_: str) -> tuple[int, str]:
        xml = fetch_raw(text, page, region, client, budget)
        cache.put(url + "#raw", xml)
        return 200, flatten(text, page, region, xml)

    return cache.fetch(url, live)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Yandex Search API: счётчик запросов (лимит — YANDEX_SEARCH_MAX_REQUESTS)")
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("budget", help="сколько запросов израсходовано и осталось")
    b.add_argument("--record", type=int, default=0, help="учесть N запросов, сделанных вне модуля")
    b.add_argument("--note", default="вне модуля")
    args = ap.parse_args(argv)
    budget = Budget()
    if args.record:
        budget.record(args.record, args.note)
    print(f"Yandex Search API: израсходовано {budget.used()} из {budget.limit}, осталось {budget.left()} "
          f"(счётчик: {budget.path.as_posix()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
