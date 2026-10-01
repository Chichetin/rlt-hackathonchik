"""Company websites found by web search: a few pages per domain → the INN the site itself states. Owner: SOURCES.

#194. Honest access only (research/SOURCES.md): robots.txt is obeyed (it is cached too), requests to one
host are paced (MIN_INTERVAL_S), a refusal (403/429) or a protection page skips the domain — we never
get around it. At most MAX_PAGES pages per domain: the home page, then same-site links whose text or
address says «реквизиты / контакты / о компании».

What goes to the cache (src/sources/cache.py), under the page URL: the VISIBLE text of the page (scripts,
styles and tags removed, one block per line) followed by `[links]` — the same-site links that may lead
to requisites. src/enrich/evidence.py checks the INN against exactly this text.

An INN is taken only where the site labels it («ИНН 7801234567», «ИНН/КПП 7801234567/780101001») and
only if it passes the checksum. Several different labelled INNs on one site (a marketplace, a list of
partners) → no INN: `inn=None`, never a guess.
"""

from __future__ import annotations

import html as html_mod
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from src.contracts import inn_is_valid
from src.sources.cache import CachedPage, SourceCache

SOURCE = "сайт компании"
UA = "Mozilla/5.0 (compatible; RLTHack-Discovery/0.1; hackathon, cached, paced)"
ROBOTS_AGENT = "RLTHack-Discovery"
MIN_INTERVAL_S = 1.0  # between two requests to the same host
TIMEOUT_S = 10.0
MAX_BYTES = 3_000_000
MAX_PAGES = 4
MAX_LINKS = 15
# Link priority: requisites first, then contacts, then «about»; a link matches its best tier.
LINK_TIERS = (("реквизит", "rekvizit", "requisit", "details"),
              ("контакт", "kontakt", "contact"),
              ("о компании", "о нас", "o-kompanii", "about", "company"))
LINKS_MARK = "[links]"

_DROP = re.compile(r"<(script|style|noscript|svg|template)\b.*?</\1\s*>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BREAK = re.compile(r"<(?:br|/p|/div|/li|/tr|/h[1-6]|/td|/th|/section|/footer|/header|/table)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_INN = re.compile(r"ИНН\D{0,40}?(?<!\d)(\d{10}|\d{12})(?!\d)", re.I)
_PROTECTION = ("captcha", "капча", "ddos-guard", "cf-chl", "доступ ограничен", "access denied")

_lock = threading.Lock()
_last: dict[str, float] = {}


class Refused(RuntimeError):
    """The site refuses (403/429, protection page) or robots.txt disallows: skip the domain."""


def html_to_text(html: str) -> str:
    text = _COMMENT.sub(" ", _DROP.sub(" ", html))
    text = _TAG.sub(" ", _BREAK.sub("\n", text))
    lines = (" ".join(html_mod.unescape(line).replace("\xa0", " ").split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def same_site(url: str, home: str) -> bool:
    a, b = (urlsplit(u).hostname or "" for u in (url, home))
    return a.removeprefix("www.") == b.removeprefix("www.")


def requisite_links(html: str, base: str) -> list[str]:
    """Same-site links whose anchor text or path looks like requisites / contacts / about: requisites
    first, then contacts, then about (page order inside a tier)."""
    tiered: dict[str, int] = {}
    for href, anchor in _HREF.findall(html):
        url = urljoin(base, href.strip())
        if not url.startswith(("http://", "https://")) or not same_site(url, base):
            continue
        if url.rstrip("/") == base.rstrip("/"):
            continue
        label = (html_to_text(anchor) + " " + urlsplit(url).path).lower()
        tier = next((n for n, words in enumerate(LINK_TIERS) if any(w in label for w in words)), None)
        if tier is not None and tier < tiered.get(url, len(LINK_TIERS)):
            tiered[url] = tier
    return sorted(tiered, key=lambda u: tiered[u])[:MAX_LINKS]


def page_body(html: str, base: str) -> str:
    links = requisite_links(html, base)
    return html_to_text(html) + ("\n" + LINKS_MARK + "\n" + "\n".join(links) if links else "")


def links_of(body: str) -> list[str]:
    _, sep, tail = body.partition("\n" + LINKS_MARK + "\n")
    return [line for line in tail.splitlines() if line.startswith("http")] if sep else []


def text_of(body: str) -> str:
    return body.partition("\n" + LINKS_MARK + "\n")[0]


def labelled_inns(text: str) -> list[str]:
    """INNs the page labels as such («ИНН …»), checksum-valid, in page order (repeats kept)."""
    return [m for m in _INN.findall(text) if inn_is_valid(m)]


def _pace(host: str) -> None:
    with _lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()


def _get(client: httpx.Client, url: str) -> httpx.Response:
    _pace(urlsplit(url).hostname or "")
    return client.get(url, follow_redirects=True)


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": UA, "Accept-Language": "ru"})


def robots_url(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}/robots.txt"


def robots_allows(url: str, cache: SourceCache, client: httpx.Client | None, offline: bool) -> bool:
    """robots.txt through the cache; absent (4xx) → allowed; unreachable → not allowed (we cannot know)."""
    rurl = robots_url(url)
    if offline:
        page = cache.get(rurl, allow_stale=True)
        if page is None:
            return False
    else:
        def live(u: str) -> tuple[int, str]:
            c = client or _client()
            try:
                r = _get(c, u)
            finally:
                if client is None:
                    c.close()
            return r.status_code, r.text if r.status_code == 200 else ""
        try:
            page = cache.fetch(rurl, live)
        except httpx.HTTPError:
            return False
    if page.status >= 500:
        return False
    if page.status != 200:
        return True
    rp = RobotFileParser()
    rp.parse(page.body.splitlines())
    return rp.can_fetch(ROBOTS_AGENT, url)


def fetch_page(url: str, cache: SourceCache | None = None, client: httpx.Client | None = None,
               offline: bool = False) -> CachedPage | None:
    """Cached page body (visible text + `[links]`). offline → cache only. Refused when robots.txt disallows,
    on 403/429 or a protection page; None for a non-HTML or a missing page."""
    cache = cache or SourceCache()
    if offline:
        return cache.get(url, allow_stale=True)
    if not robots_allows(url, cache, client, offline):
        raise Refused(f"{url}: robots.txt не разрешает или недоступен")

    def live(u: str) -> tuple[int, str]:
        c = client or _client()
        try:
            r = _get(c, u)
        finally:
            if client is None:
                c.close()
        if r.status_code in (403, 429):
            raise Refused(f"{u}: HTTP {r.status_code} — сайт отказал, не обходим")
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html").lower():
            return r.status_code, ""
        html = r.text[:MAX_BYTES]
        if len(html) < 20000 and any(w in html.lower() for w in _PROTECTION):
            raise Refused(f"{u}: страница защиты — не обходим")
        return 200, page_body(html, str(r.url))

    page = cache.fetch(url, live)
    return page if page.status == 200 and page.body else None


@dataclass
class SiteInn:
    domain: str
    inn: str | None
    url: str | None  # the page where the INN is stated (Evidence url)
    pages: list[str] = field(default_factory=list)
    candidates: dict[str, int] = field(default_factory=dict)
    reason: str = ""


def decide(seen: Counter[str]) -> tuple[str | None, str]:
    """One labelled INN on the site → it; several → the dominant one only if it is at least 3× as frequent."""
    if not seen:
        return None, "на сайте нет ИНН"
    (top, n), *rest = seen.most_common()
    if not rest:
        return top, "ok"
    if n >= 3 * rest[0][1]:
        return top, "ok (основной ИНН сайта)"
    return None, f"на сайте несколько ИНН ({len(seen)})"


def site_inn(home: str, cache: SourceCache | None = None, client: httpx.Client | None = None,
             offline: bool = False) -> SiteInn:
    """Home page, then requisite links, until a page states an INN (at most MAX_PAGES pages)."""
    cache = cache or SourceCache()
    domain = (urlsplit(home).hostname or "").removeprefix("www.")
    res = SiteInn(domain=domain, inn=None, url=None)
    seen: Counter[str] = Counter()
    first_page: dict[str, str] = {}
    queue = [home]
    try:
        while queue and len(res.pages) < MAX_PAGES:
            url = queue.pop(0)
            if url in res.pages:
                continue
            res.pages.append(url)
            try:
                page = fetch_page(url, cache, client, offline)
            except httpx.HTTPError as e:
                res.reason = f"сайт недоступен: {type(e).__name__}"
                if url == home:
                    return res
                continue
            if page is None:
                continue
            inns = labelled_inns(text_of(page.body))
            for inn in inns:
                first_page.setdefault(inn, page.url)
            seen.update(inns)
            if inns and len(set(inns)) == 1:
                break  # the page (often the footer of the home page) names one INN: enough
            if url == home:
                queue += links_of(page.body)
    except Refused as e:
        res.reason = str(e)
        if not seen:
            return res
    res.candidates = dict(seen)
    res.inn, reason = decide(seen)
    res.reason = res.reason or reason if res.inn is None else reason
    res.url = first_page.get(res.inn) if res.inn else None
    return res
