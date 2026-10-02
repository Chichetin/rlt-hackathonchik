"""Web discovery through Yandex Search — the fallback path of the block «Новые» (#194, #186). Owner: SOURCES.

Yandex Search is paid, so it is asked only (human decisions of 02.10, IR630, question 29 «б»; D-149):
1. when the client is on — the key AND the ceilings `YANDEX_MAX_PER_LOT`, `YANDEX_MAX_PER_RUN` in the human's
   `.env` (src/sources/yandex_search.py::disabled_reason); off by default, and then nothing here runs;
2. on a lot where the free sources (organizers' data, the МСП registry, the pool, web leads #108) gave fewer
   than `min_strong` strong candidates — product_fit ≥ 0.8 (configs/yandex_discovery.yaml).

The path of one lot: the subject of the lot, cleaned (`clean_text`) × role modifiers of the purchase type →
`yandex_search.search(..., lot=<lot>)` (cache first; live requests within the per-lot, per-run, daily and
all-time caps of the client) → result domains minus aggregators, marketplaces and directories (`skip_domains`)
→ the company site states its INN (src/sources/site.py: robots.txt, pacing, a labelled INN with a valid
checksum, one INN per site) → a `web_leads.Lead`. From there a found company takes EXACTLY the road of a web
lead (src/enrich/discover.py::lead_matches): absent from organizers' data, its ФНС card in the cache, the main
OKVED confirms the class, the region is the customer's or the neighbour; no card → the enrichment queue. One
row per company: an INN already found by the registry or a lead file is not repeated.

What the site adds to the card (`with_site`): the page that states the INN as an Evidence, and the phone, email
and address the site states (src/enrich/site_facts.py), each checked against its cached page — for an individual
entrepreneur (12-digit INN) no contact is taken from a site (152-ФЗ). Neither the title nor the snippet of the
search result is ever shown as a fact. Nothing here changes the formula or the weights of the score.

Agents never call the live API (decision of IR630): tests use fixtures and `httpx.MockTransport`.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import yaml

from src.contracts import Counterparty, CounterpartyMatch, Evidence
from src.enrich import site_facts
from src.enrich.evidence import check_counterparty
from src.settings import get_settings
from src.sources import site, web_leads, yandex_search
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "yandex_discovery.yaml"
SOURCE = "Яндекс Поиск → сайт компании"
STRONG_FIT = 0.8  # product_fit 1.0 / 0.8 — a strong candidate (the human, 02.10)
REGION_SPB = "2"  # the Yandex region of Saint Petersburg (lr)
MAX_QUERY_WORDS = 8  # of the cleaned lot text: a long subject finds nothing as one phrase
MAX_QUERY_CHARS = 400  # SearchQuery.query_text of the API
GOODS_CLASSES = range(1, 34)  # OKPD2 01–33 are goods; the rest — services and works

PurchaseKind = Literal["goods", "services"]


@lru_cache(maxsize=4)
def _config(path: Path, mtime_ns: int) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_config(path: Path = CONFIG) -> dict:
    return _config(path, path.stat().st_mtime_ns)


def enabled() -> bool:
    """The client may send a live request (key and ceilings set). Off → the fallback is not even planned."""
    return yandex_search.disabled_reason() is None


# --- queries ------------------------------------------------------------------------------------------------

_YEAR = re.compile(r"(?<!\w)(?:(?:на|в|за)\s+)?20[1-3]\d(?:\s*[-–—/]\s*20[1-3]\d)?"
                   r"(?:\s*(?:годах|годов|году|года|год|гг\.?|г\.?))?(?!\w)", re.I)
_POINTS = re.compile(r"(?<!\w)в\s+\d+\s*(?:-?\s*[а-яё]{1,2}\s+)?точк(?:е|ах|у)(?!\w)", re.I)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|«\s*»|\"\s*\"")
_EDGE = " \t\n,;:.-–—/"


def clean_text(text: str) -> str:
    """The subject of a lot as a search phrase: years («на 2025 год», «2024-2025 гг.») and «в 1 точке» cut out,
    one line without stray punctuation."""
    out = _POINTS.sub(" ", _YEAR.sub(" ", text))
    out = _EMPTY_BRACKETS.sub(" ", out)
    out = re.sub(r"\s+([,;.])", r"\1", " ".join(out.split()))
    return re.sub(r"([,;])\1+", r"\1", out).strip(_EDGE)


def purchase_kind(okpd2: Sequence[str]) -> PurchaseKind:
    """Goods (OKPD2 01–33) or services and works, by the first code; no code → goods (most lots of the ЭМ)."""
    for code in okpd2:
        head = code.strip()[:2]
        if head.isascii() and head.isdigit():
            return "goods" if int(head) in GOODS_CLASSES else "services"
    return "goods"


def lot_queries(text: str, okpd2: Sequence[str] = (), cfg: dict | None = None) -> list[str]:
    """«cleaned lot text × role modifier» — one query per modifier of the purchase type; [] for an empty text."""
    base = " ".join(clean_text(text).split()[:MAX_QUERY_WORDS]).strip(_EDGE)
    if not base:
        return []
    mods = (cfg or load_config())["modifiers"][purchase_kind(okpd2)]
    return list(dict.fromkeys(f"{base[:MAX_QUERY_CHARS - len(m) - 1].rstrip()} {m}" for m in mods))


# --- the trigger --------------------------------------------------------------------------------------------

def strong_count(matches: Sequence[CounterpartyMatch]) -> int:
    """Distinct companies whose product_fit is a strong step (≥ STRONG_FIT)."""
    return len({m.counterparty.inn or m.counterparty.name for m in matches
                if any(c.name == "product_fit" and c.value >= STRONG_FIT for c in m.components)})


def needed(strong: int, cfg: dict | None = None) -> tuple[bool, str]:
    """(ask Yandex?, why). The client off → «выключен: …»; enough strong candidates → «не нужен: …»."""
    why = yandex_search.disabled_reason()
    if why is not None:
        return False, why
    floor = int((cfg or load_config())["min_strong"])
    if strong >= floor:
        return False, f"не нужен: сильных кандидатов из бесплатных источников {strong} ≥ {floor}"
    return True, f"нужен: сильных кандидатов из бесплатных источников {strong} < {floor}"


# --- one lot ------------------------------------------------------------------------------------------------

def skipped(domain: str, skip: Sequence[str]) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in skip)


@dataclass
class SiteCheck:
    """One result domain: what its site said."""

    domain: str
    home: str
    query: str
    inn: str | None = None
    inn_url: str | None = None  # the cached page that states the INN
    pages: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class Fallback:
    """The answer of the fallback path for one lot."""

    lot: str
    used: bool  # Yandex was asked (live or from its cache) for this lot
    reason: str
    queries: list[str] = field(default_factory=list)
    requests_live: int = 0  # paid requests of this call
    sites: list[SiteCheck] = field(default_factory=list)
    leads: list[web_leads.Lead] = field(default_factory=list)  # one per site that states one INN

    def check(self, inn: str) -> SiteCheck | None:
        return next((s for s in self.sites if s.inn == inn), None)


def lot_leads(lot: str, text: str, okpd_class: str, strong: int, *, okpd2: Sequence[str] = (),
              cache: SourceCache | None = None, search_cache: SourceCache | None = None,
              client: httpx.Client | None = None, site_client: httpx.Client | None = None,
              offline: bool = False, deadline: float | None = None, cfg: dict | None = None) -> Fallback:
    """The fallback path for one lot: `text` — its subject, `okpd_class` — the class its leads answer, `strong`
    — how many strong candidates the free sources gave. Not needed or off → nothing is done, the reason is
    returned. `cache` — site pages (the source cache of discover); `search_cache` — the answers of the client
    (its own shared file by default). `offline` → cached answers and pages only. `deadline` (time.monotonic(), the
    deadline of the whole search) — after it nothing goes to the network: answers and pages are read from the cache
    only, and a live request is cut to the time left. Once `max_sites` sites are checked no further query is sent.
    What was not reached is cached by nobody and costs nothing: the next search of the lot continues."""
    cfg = cfg or load_config()
    need, why = needed(strong, cfg)
    out = Fallback(lot=lot, used=False, reason=why)
    if not need:
        return out
    out.queries = lot_queries(text, okpd2, cfg)
    if not out.queries:
        out.reason = f"{why}; не выполнен: в тексте лота нет слов для запроса"
        return out
    cache = cache or SourceCache()
    skip = list(cfg.get("skip_domains", []))
    region = get_settings().yandex_search_region or REGION_SPB
    out.used = True
    notes: list[str] = []
    seen: set[str] = set()
    max_sites = int(cfg.get("max_sites", 6))
    timed_out = False

    def left() -> float | None:
        """Seconds of network time left; None — no deadline; 0 — none left (cache only)."""
        return None if deadline is None else max(deadline - time.monotonic(), 0.0)

    def live() -> bool:
        nonlocal timed_out
        ok = not offline and left() != 0.0
        timed_out = timed_out or (not offline and not ok)
        return ok

    for q in out.queries:
        if len(out.sites) >= max_sites:  # nothing more would be opened: do not pay for another answer
            break
        try:
            if live():
                with _bounded(client, left(), yandex_search.TIMEOUT_S) as c:
                    answer = yandex_search.search(q, int(cfg.get("results", 10)), region, cache=search_cache,
                                                  client=c, lot=lot)
            else:
                answer = yandex_search.search(q, int(cfg.get("results", 10)), region, cache=search_cache,
                                              offline_only=True, lot=lot)
        except (yandex_search.YandexSearchError, httpx.HTTPError) as e:  # a ceiling, a refusal: no more requests
            notes.append(str(e))
            break
        if answer is None:  # offline and never asked
            continue
        out.requests_live += not answer.from_cache
        for hit in answer.hits:
            p = urlsplit(hit.url)
            dom = (p.hostname or "").removeprefix("www.")
            if not dom or dom in seen or skipped(dom, skip):
                continue
            seen.add(dom)
            if len(out.sites) >= max_sites:
                break
            s = SiteCheck(domain=dom, home=f"{p.scheme}://{p.netloc}/", query=answer.query)
            if live():
                with _bounded(site_client, left(), site.TIMEOUT_S) as c:
                    found = site.site_inn(s.home, cache, c, deadline=deadline)
            else:
                found = site.site_inn(s.home, cache, offline=True)
            s.inn, s.inn_url, s.pages, s.reason = found.inn, found.url, list(found.pages), found.reason
            out.sites.append(s)
            if s.inn and s.inn_url and s.inn not in {lead.inn for lead in out.leads}:
                out.leads.append(web_leads.Lead(
                    okpd2_class=okpd_class, inn=s.inn, name=dom, site=dom, role_claim="unknown", region=None,
                    source_url=s.inn_url, search_query=answer.query, found_at=date.today()))
    out.reason = (f"{why}; запросов {len(out.queries)}, из них платных {out.requests_live}; сайтов проверено "
                  f"{len(out.sites)}, ИНН на сайте у {len(out.leads)}"
                  + ("; время запроса вышло — дальше только кеш, остальное в следующий раз" if timed_out else "")
                  + ("; " + "; ".join(notes) if notes else ""))
    if out.requests_live:
        log.warning("Яндекс Search (запасной путь), лот %s: %d платных запросов", lot, out.requests_live)
    return out


@contextmanager
def _bounded(client: httpx.Client | None, left: float | None, default_s: float) -> Iterator[httpx.Client | None]:
    """The caller's client as is; without one, a client whose timeout is the time left (None — the module's own)."""
    if client is not None or left is None:
        yield client
        return
    own = httpx.Client(timeout=max(min(left, default_s), 0.5),
                       headers={"User-Agent": site.UA, "Accept-Language": "ru"})
    try:
        yield own
    finally:
        own.close()


def with_site(cp: Counterparty, s: SiteCheck, cache: SourceCache) -> Counterparty:
    """The card plus what the company's own site states: the page with the INN and its contacts. Every added
    fact passes the Evidence check against its cached page; a fact that does not is not added."""
    inn = cp.inn or ""
    page = cache.get(s.inn_url or "", allow_stale=True)
    added: list[Evidence] = []
    if page is not None and inn and s.inn == inn:
        at = datetime.fromtimestamp(page.fetched_at, tz=UTC)
        added.append(Evidence(source=site.SOURCE, url=page.url, fetched_at=at,
                              fact=f"ИНН {inn} указан на сайте {s.domain}", field="inn", value=inn))
    pages = [p for p in (cache.get(u, allow_stale=True) for u in s.pages) if p is not None and p.status == 200]
    facts = site_facts.merge(site_facts.extract(p, s.domain, individual=len(inn) == 12) for p in pages)
    contacts = {**facts.contacts(), **cp.contacts}  # the registry's own contact wins
    richer = cp.model_copy(update={"contacts": contacts,
                                   "evidence": [*cp.evidence, *added, *facts.phones, *facts.emails, *facts.addresses]})
    if check_counterparty(richer, cache.page_text):
        log.warning("web_discover: факты сайта %s не прошли Evidence — добавлена только страница с ИНН", s.domain)
        richer = cp.model_copy(update={"evidence": [*cp.evidence, *added]})
        if check_counterparty(richer, cache.page_text):
            return cp
    return richer
