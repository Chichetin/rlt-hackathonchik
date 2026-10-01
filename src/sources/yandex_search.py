"""Yandex Search API v2 (Yandex Cloud, paid) — web search, the FALLBACK path of discovery. Owner: SOURCES.

#194 / #186, decisions of the human (IR630, 02.10.2026, D-093):
- the client is OFF by default: a live request needs the key (YANDEX_SEARCH_API_KEY) AND both ceilings
  (YANDEX_MAX_PER_LOT, YANDEX_MAX_PER_RUN — positive integers set by the human in `.env`); anything missing →
  `Disabled`, no network (`gate()`);
- three counters guard the money: per lot and per run (`Caps`, in memory, one run = one process) on top of
  the persistent HARD LIMIT of YANDEX_SEARCH_MAX_REQUESTS (2500) requests for the whole hackathon (`Budget`);
- deferred mode is the default (YANDEX_SEARCH_MODE=deferred|sync): 30.5 ₽ per 1000 requests instead of 488 ₽
  (price list of AI Studio as quoted by the human in #194 on 02.10 — PRICE_RUB_PER_1000);
- agents never call the API: tests run on fixtures with a stub transport.

    sync:      POST https://searchapi.api.cloud.yandex.net/v2/web/search        Authorization: Api-Key <key>
               answer: {"rawData": <base64 XML>}
    deferred:  POST https://searchapi.api.cloud.yandex.net/v2/web/searchAsync   → Operation {"id", "done"}
               GET  https://operation.api.cloud.yandex.net/operations/<id>      → Operation; when `done`:
               {"response": {"rawData": <base64 XML>}} or {"error": {"code", "message"}}
    body (both): query.searchType / query.queryText / query.page, region, groupSpec, responseFormat=FORMAT_XML
    documents are <group><doc><url/><domain/><title/><passages/>

Paths and messages are those of the public API specification github.com/yandex-cloud/cloudapi
(yandex/cloud/searchapi/v2/search_service.proto, search_query.proto, yandex/cloud/operation/*.proto), the
operation host — of yandex-cloud/docs ru/api-design-guide/concepts/operation.md, read on 02.10.2026.
[ПРОВЕРИТЬ] the deferred mode was NOT checked live — agents are forbidden to call the API; its fixtures
(tests/fixtures/yandex_search/operation_*.json) are built from the specification, not recorded. Not known
from the specification: how long a deferred operation takes and whether a status request is billed (we
assume it is not and count only the search requests).

Budget (`Budget`): every live request is logged in its own SQLite file BEFORE it is sent (a failed
request counts too: we never under-count a paid API); the (limit+1)-th request raises BudgetExceeded and
makes no network call. The file is shared by all worktrees of one checkout (`budget_path()`), so a
second worktree does not start from zero. A cached answer costs nothing: neither the budget nor the ceilings.

What goes to the cache (src/sources/cache.py):
- `search_url(...)` — a human-checkable yandex.ru/search URL: the answer as flat lines
  `doc.<n>.url|domain|title|passage: <value>` (src/enrich/evidence.py checks a «site» fact here);
- `search_url(...) + "#raw"` — the XML, for audit;
- `search_url(...) + "#operation"` — the deferred operation waiting for its answer (id, when submitted), so
  that the next run collects the answer instead of paying for the request again.
401/403 → Blocked (key or rights), 429 → Blocked with a cooldown: we stop, never retry around it.

`python -m src.sources.yandex_search budget` — on/off and why, requests used / left (no network);
`budget --record N --note …` — count requests made outside this module (the probe of 01.10).
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx

from src.sources.cache import CachedPage, SourceCache

log = logging.getLogger(__name__)

ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/search"
ASYNC_ENDPOINT = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"  # [ПРОВЕРИТЬ] not called live
OPERATION_ENDPOINT = "https://operation.api.cloud.yandex.net/operations/"  # [ПРОВЕРИТЬ] not called live
Mode = Literal["sync", "deferred"]
# ₽ per 1000 requests, VAT included: aistudio.yandex.ru/docs/ru/search-api/pricing as quoted by the human
# (IR630, #194, 02.10.2026). Not read by an agent: the documentation site answers with a captcha.
PRICE_RUB_PER_1000: dict[Mode, float] = {"sync": 488.0, "deferred": 30.5}
SOURCE = "Яндекс Поиск (Yandex Search API)"
REGION_SPB = "2"  # Yandex region id of Санкт-Петербург
DOCS_PER_PAGE = 10  # one document per domain (GROUP_MODE_DEEP, docsInGroup=1)
MIN_INTERVAL_S = float(os.environ.get("YANDEX_MIN_INTERVAL_S", "0.5"))
TIMEOUT_S = float(os.environ.get("YANDEX_TIMEOUT_S", "30"))
BLOCK_COOLDOWN_S = 900.0
DEFAULT_LIMIT = 2500
POLL_S = float(os.environ.get("YANDEX_DEFERRED_POLL_S", "5"))  # between two status requests of one operation
NO_LOT = "-"  # requests made outside a lot share one per-lot ceiling

ROOT = Path(__file__).resolve().parents[2]

_lock = threading.Lock()
_last_request = 0.0
_blocked_until = 0.0


class Blocked(RuntimeError):
    """The API refuses us (bad key, no rights, rate limit): stop, do not retry around it."""


class BudgetExceeded(Blocked):
    """The hard request limit is reached: no request was made."""


class Disabled(Blocked):
    """Yandex is switched off (no key or no ceiling in `.env` — the default state): no request was made."""


class CapExceeded(Blocked):
    """The ceiling of requests per lot or per run is reached: no request was made."""


class OperationFailed(RuntimeError):
    """A deferred operation ended with an error: there is no answer; the request was paid for."""


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
    """The environment wins over `.env`, also when it is set to an empty value: `YANDEX_SEARCH_API_KEY= <cmd>`
    switches Yandex off for one command whatever `.env` says."""
    if name in os.environ:
        return os.environ[name].strip() or None
    return _dotenv(name)


def api_key() -> str:
    key = setting("YANDEX_SEARCH_API_KEY")
    if not key or key.startswith("your_"):
        raise RuntimeError("YANDEX_SEARCH_API_KEY не задан (.env): запрос к Yandex Search API не отправлен")
    return key


def request_limit() -> int:
    return int(setting("YANDEX_SEARCH_MAX_REQUESTS") or DEFAULT_LIMIT)


def _ceiling(name: str) -> int | None:
    """A positive integer from the settings; None when absent, not a number or not positive."""
    raw = setting(name) or ""
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else None


@dataclass(frozen=True)
class Gate:
    """Whether Yandex may be called at all, and the ceilings the human set."""

    enabled: bool
    reason: str  # one line for the log and the UI: «включён …» / «выключен: …»
    per_lot: int = 0
    per_run: int = 0


def gate() -> Gate:
    """On only with the key AND both ceilings; the default (nothing in `.env`) is off."""
    key = setting("YANDEX_SEARCH_API_KEY")
    if not key or key.startswith("your_"):
        return Gate(False, "выключен: нет ключа (YANDEX_SEARCH_API_KEY)")
    per_lot, per_run = _ceiling("YANDEX_MAX_PER_LOT"), _ceiling("YANDEX_MAX_PER_RUN")
    missing = [n for n, v in (("YANDEX_MAX_PER_LOT", per_lot), ("YANDEX_MAX_PER_RUN", per_run)) if v is None]
    if per_lot is None or per_run is None:
        return Gate(False, f"выключен: нет потолка ({', '.join(missing)})")
    return Gate(True, f"включён: не больше {per_lot} запросов на лот и {per_run} на прогон", per_lot, per_run)


def search_mode() -> Mode:
    """YANDEX_SEARCH_MODE; anything but `sync` is the deferred (16 times cheaper) mode."""
    return "sync" if (setting("YANDEX_SEARCH_MODE") or "").lower() == "sync" else "deferred"


def cost_rub(requests: int, mode: Mode) -> float:
    return round(requests * PRICE_RUB_PER_1000[mode] / 1000, 2)


class Caps:
    """Ceilings of live requests per lot and per run (one run = one process), on top of `Budget`.
    `check()` then `add()` under the client's lock; a cached answer never gets here."""

    def __init__(self, per_lot: int, per_run: int):
        self.per_lot, self.per_run = per_lot, per_run
        self.used_run = 0
        self.used_lot: dict[str, int] = {}

    def left(self, lot: str = NO_LOT) -> int:
        return max(0, min(self.per_lot - self.used_lot.get(lot, 0), self.per_run - self.used_run))

    def check(self, lot: str = NO_LOT) -> None:
        if self.used_run >= self.per_run:
            raise CapExceeded(f"Yandex Search API: потолок {self.per_run} запросов на прогон исчерпан, "
                              "запрос не послан")
        if self.used_lot.get(lot, 0) >= self.per_lot:
            raise CapExceeded(f"Yandex Search API: потолок {self.per_lot} запросов на лот исчерпан "
                              f"(лот {lot}), запрос не послан")

    def add(self, lot: str = NO_LOT) -> None:
        self.used_run += 1
        self.used_lot[lot] = self.used_lot.get(lot, 0) + 1


_run_caps: Caps | None = None


def run_caps() -> Caps:
    """The ceilings of this process, created once from the settings (0 / 0 while Yandex is off)."""
    global _run_caps
    if _run_caps is None:
        g = gate()
        _run_caps = Caps(g.per_lot, g.per_run)
    return _run_caps


def reset_run_caps() -> None:
    global _run_caps
    _run_caps = None


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
    """WebSearchRequest. `page` is a field of SearchQuery (search_query.proto), not of the request itself:
    the top-level `page` sent until 02.10 is not in the specification (only page 0 was ever requested)."""
    return {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": text.strip(), "page": str(page)},
            "region": region,
            "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(DOCS_PER_PAGE), "docsInGroup": "1"},
            "responseFormat": "FORMAT_XML"}


def _refused(r: httpx.Response) -> None:
    if r.status_code in (401, 403):
        raise _block(f"HTTP {r.status_code} (ключ или права)")
    if r.status_code == 429:
        raise _block("HTTP 429 (лимит частоты)")


def _open(what: str) -> str:
    """The key, or the reason why nothing is sent: a pause after a refusal, Yandex switched off."""
    if blocked():
        raise Blocked("Yandex Search API: пауза после отказа ещё не истекла")
    g = gate()
    if not g.enabled:
        raise Disabled(f"Yandex Search API {g.reason} — {what} не отправлен")
    return api_key()


def _send(endpoint: str, text: str, page: int, region: str, client: httpx.Client | None, budget: Budget | None,
          caps: Caps | None, lot: str) -> dict:
    """One paid request → the JSON answer. Before anything is sent: the gate, the ceilings of the lot and of
    the run, the hard limit — Disabled / CapExceeded / BudgetExceeded mean no network call was made."""
    global _last_request
    search_url(text, page, region)
    key = _open("запрос")
    caps = caps or run_caps()
    budget = budget or Budget()
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        with _lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
            if wait > 0:
                time.sleep(wait)
            caps.check(lot)  # first: a refused request must not spend the persistent budget
            budget.take(f"p={page} lr={region} {text.strip()}")
            caps.add(lot)
            try:
                r = client.post(endpoint, json=body(text, page, region), headers={"Authorization": f"Api-Key {key}"})
            finally:
                _last_request = time.monotonic()
        _refused(r)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("Yandex Search API: ответ не объект JSON")
        return data
    finally:
        if own:
            client.close()


def _raw(data: dict) -> str:
    raw = data.get("rawData")
    if not isinstance(raw, str):
        raise ValueError("Yandex Search API: в ответе нет rawData")
    return base64.b64decode(raw).decode("utf-8")


def fetch_raw(text: str, page: int = 0, region: str = REGION_SPB, client: httpx.Client | None = None,
              budget: Budget | None = None, caps: Caps | None = None, lot: str = NO_LOT) -> str:
    """One live synchronous request → the XML text. Nothing is sent when Yandex is off or a limit is reached."""
    return _raw(_send(ENDPOINT, text, page, region, client, budget, caps, lot))


def operation_url(url: str) -> str:
    return url + "#operation"


def _finish(url: str, text: str, page: int, region: str, op: dict, cache: SourceCache) -> CachedPage | None:
    """The cached answer of a finished operation; None while it is running. An operation that ended with an
    error is marked as spent (the next search submits a new one) and raises OperationFailed."""
    if not op.get("done"):
        return None
    if "error" in op or not isinstance(op.get("response"), dict):
        cache.put(operation_url(url), json.dumps(op, ensure_ascii=False), status=500)
        err = op.get("error")
        why = f"{err.get('code', '')} {err.get('message', '')}".strip() if isinstance(err, dict) else "без ответа"
        raise OperationFailed(f"Yandex Search API: отложенный запрос завершился ошибкой: {why}")
    xml = _raw(op["response"])
    cache.put(url + "#raw", xml)
    return cache.put(url, flatten(text, page, region, xml))


def submit(text: str, page: int = 0, region: str = REGION_SPB, cache: SourceCache | None = None,
           client: httpx.Client | None = None, budget: Budget | None = None, caps: Caps | None = None,
           lot: str = NO_LOT) -> dict:
    """Deferred mode, step 1: one paid request → the Operation; its id is kept in the cache."""
    cache = cache or SourceCache()
    op = _send(ASYNC_ENDPOINT, text, page, region, client, budget, caps, lot)
    if not isinstance(op.get("id"), str) or not op["id"]:
        raise ValueError("Yandex Search API: в ответе на отложенный запрос нет id операции")
    cache.put(operation_url(search_url(text, page, region)),
              json.dumps({"id": op["id"], "submitted_at": time.time()}, ensure_ascii=False))
    return op


def _pending(url: str, cache: SourceCache) -> str | None:
    """Id of the operation submitted for this search and still awaited; None — none, failed or gone."""
    page = cache.get(operation_url(url), allow_stale=True)
    return json.loads(page.body)["id"] if page is not None and page.status == 200 else None


def collect(text: str, page: int = 0, region: str = REGION_SPB, cache: SourceCache | None = None,
            client: httpx.Client | None = None) -> CachedPage | None:
    """Deferred mode, step 2: one status request of the operation submitted earlier. The answer when it is
    ready (now in the cache); None — still running, or nothing was submitted. Not counted in the budget
    ([ПРОВЕРИТЬ] the specification does not say that a status request is billed)."""
    global _last_request
    cache = cache or SourceCache()
    url = search_url(text, page, region)
    op_id = _pending(url, cache)
    if op_id is None:
        return None
    key = _open("запрос статуса")
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        with _lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                r = client.get(OPERATION_ENDPOINT + quote(op_id, safe=""), headers={"Authorization": f"Api-Key {key}"})
            finally:
                _last_request = time.monotonic()
        _refused(r)
        if r.status_code == 404:  # the operation is gone: the next search submits a new one
            cache.put(operation_url(url), json.dumps({"id": op_id}), status=404)
            return None
        r.raise_for_status()
        return _finish(url, text, page, region, r.json(), cache)
    finally:
        if own:
            client.close()


def _deferred(url: str, text: str, page: int, region: str, cache: SourceCache, client: httpx.Client | None,
              budget: Budget | None, caps: Caps | None, lot: str, wait_s: float, poll_s: float) -> CachedPage | None:
    ask_now = _pending(url, cache) is not None  # submitted by an earlier call: its status is asked at once
    if not ask_now:
        done = _finish(url, text, page, region, submit(text, page, region, cache, client, budget, caps, lot), cache)
        if done is not None or wait_s <= 0:
            return done
    deadline = time.monotonic() + wait_s
    while True:
        if not ask_now:
            if time.monotonic() + poll_s > deadline:
                log.info("yandex: отложенный запрос «%s» ещё не готов — ответ заберёт следующий вызов", text)
                return None
            time.sleep(poll_s)
        ask_now = False
        got = collect(text, page, region, cache, client)
        if got is not None or _pending(url, cache) is None:  # ready, or the operation is gone (404)
            return got


def search(text: str, page: int = 0, region: str = REGION_SPB, cache: SourceCache | None = None,
           client: httpx.Client | None = None, offline: bool = False,
           budget: Budget | None = None, *, caps: Caps | None = None, lot: str = NO_LOT,
           mode: Mode | None = None, wait_s: float = 0.0, poll_s: float | None = None) -> CachedPage | None:
    """Cached answer text. offline → cache only (stale is fine), None when not cached.
    Live: fresh cache → cache (free: no budget, no ceilings); else by `mode` (default — the settings):
    - sync: one paid request; it failed → stale cache if any, else the error;
    - deferred: the operation submitted earlier is asked once (free); none → one paid request submits it.
      The answer is awaited for `wait_s` seconds (a status request every `poll_s`); not ready → None: the
      next call (the next run) collects it without paying again.
    Yandex off or a limit reached → Disabled / CapExceeded / BudgetExceeded, nothing is sent."""
    cache = cache or SourceCache()
    url = search_url(text, page, region)
    if offline:
        return cache.get(url, allow_stale=True)
    hit = cache.get(url)
    if hit is not None:
        return hit
    if (mode or search_mode()) == "deferred":
        try:
            return _deferred(url, text, page, region, cache, client, budget, caps, lot, wait_s,
                             POLL_S if poll_s is None else poll_s)
        except (httpx.HTTPError, ValueError):
            stale = cache.get(url, allow_stale=True)
            if stale is not None:
                return stale
            raise

    def live(_: str) -> tuple[int, str]:
        xml = fetch_raw(text, page, region, client, budget, caps, lot)
        cache.put(url + "#raw", xml)
        return 200, flatten(text, page, region, xml)

    return cache.fetch(url, live)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Yandex Search API: включён ли, счётчик запросов (без сети)")
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("budget", help="сколько запросов израсходовано и осталось")
    b.add_argument("--record", type=int, default=0, help="учесть N запросов, сделанных вне модуля")
    b.add_argument("--note", default="вне модуля")
    args = ap.parse_args(argv)
    budget = Budget()
    if args.record:
        budget.record(args.record, args.note)
    mode = search_mode()
    print(f"Yandex Search API: {gate().reason}; режим {mode} ({PRICE_RUB_PER_1000[mode]} ₽ за 1000 запросов)")
    print(f"Yandex Search API: израсходовано {budget.used()} из {budget.limit}, осталось {budget.left()} "
          f"(счётчик: {budget.path.as_posix()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
