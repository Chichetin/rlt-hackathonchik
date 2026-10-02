"""verify(candidate) -> the candidate checked now: name, legal status, RNP. Owner: SOURCES.

The synchronous check of `make predict` in the full mode (#229, decisions 19–20 of IR630): the test
export is judged by its file, so every shown row must carry a name and a checked status, and only
verified companies are shown:
- the INN passes the checksum and is backed by a cached page (Evidence);
- the legal status from a card is «действующая» (pb.nalog.ru card; ГИР БО when the card has no status,
  e.g. pb asked for a captcha and enrich() fell back to the МСП registry, which has no legal status);
- the name comes from a card whose Evidence passes (no «ИНН …» placeholder in the file);
- the company is not in the RNP of the ЕИС (zakupki.gov.ru, crawl-delay 60 s; `rnp=False` skips it and
  the reason says so; `offline=True` — the cached RNP page only).
Every fact goes through strip_unverified: a status or a name whose Evidence does not pass is not used.
Network errors never raise: the verdict says which source was unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from src.contracts import Counterparty, inn_is_valid
from src.enrich import enrich as enrich_mod
from src.enrich.evidence import CachedPages, cached_pages, strip_unverified
from src.sources import bo_nalog, eis_rnp, pb_nalog, rmsp
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)

ACTIVE_STEM = "действу"  # «Действующая организация» (pb), «действующая» (ГИР БО → enrich.bo_status)
DEAD_STEMS = ("ликвид", "банкрот", "прекращ", "исключ")  # src/match/filters.py::_DEAD + «исключён из ЕГРЮЛ»
FNS = "ФНС (карточка)"
BO = "ГИР БО"
RNP = "РНП ЕИС"


@dataclass
class Verdict:
    counterparty: Counterparty  # with the checked card merged in (organizers' stats kept)
    ok: bool
    why: str  # «проверен: …» or the reason it is not shown
    down: list[str] = field(default_factory=list)  # sources that did not answer


def is_active(status: str | None) -> bool | None:
    """True — «действующая», False — liquidated / bankrupt / stopped, None — no status or unknown text."""
    if not status:
        return None
    low = status.lower()
    if any(s in low for s in DEAD_STEMS):
        return False
    return True if ACTIVE_STEM in low else None


def merge(cp: Counterparty, card: Counterparty) -> Counterparty:
    """Card facts into the candidate, as src/api/app.py::_enriched: organizers' data (stats, kpp) kept,
    the name replaced only when the candidate has the placeholder «ИНН …»."""
    upd: dict[str, object] = {"evidence": [*cp.evidence, *(e for e in card.evidence if e not in cp.evidence)]}
    if cp.name.startswith("ИНН ") and not card.name.startswith("ИНН "):
        upd["name"] = card.name
    for f in ("kind", "region", "status", "is_msp", "in_rnp", "kpp"):
        if getattr(card, f) is not None and (f != "kpp" or cp.kpp is None):
            upd[f] = getattr(card, f)
    if card.okved:
        upd["okved"] = card.okved
    upd["contacts"] = {**card.contacts, **cp.contacts}
    return cp.model_copy(update=upd)


def _checked(card: Counterparty, pages: CachedPages, keep_name: bool = False) -> Counterparty:
    """Only facts whose Evidence passes; status / name without a passing Evidence are dropped (the name
    becomes the placeholder «ИНН …»; `keep_name` — a placeholder already there stays as it is)."""
    card = strip_unverified(card, pages)
    have = {e.field for e in card.evidence}
    upd: dict[str, object] = {}
    if "status" not in have:
        upd["status"] = None
    if "name" not in have and not (keep_name and card.name.startswith("ИНН ")):
        upd["name"] = f"ИНН {card.inn}" if card.inn else card.name
    return card.model_copy(update=upd) if upd else card


def _card(inn: str, cache: SourceCache, offline: bool, down: list[str],
          enrich_fn: Callable[..., Counterparty]) -> Counterparty | None:
    try:
        return enrich_fn(inn, cache, offline=offline)
    except KeyError:  # offline and nothing cached, or no card in the registry
        return None
    except (pb_nalog.CaptchaRequired, TimeoutError, httpx.HTTPError, rmsp.Blocked, bo_nalog.Blocked,
            ValueError, OSError) as e:
        log.warning("verify %s: карточка не получена: %s", inn, e)
        down.append(FNS)
        return None


def _bo_card(inn: str, cache: SourceCache, offline: bool, down: list[str]) -> Counterparty | None:
    try:
        page = bo_nalog.fetch(inn, cache, offline=offline)
    except (bo_nalog.Blocked, httpx.HTTPError, ValueError, KeyError, OSError) as e:
        log.warning("verify %s: ГИР БО недоступен: %s", inn, e)
        down.append(BO)
        return None
    if page is None or not bo_nalog.parse(page.body):
        return None
    return enrich_mod.from_bo(inn, page)


def verify(cp: Counterparty, cache: SourceCache | None = None, *, offline: bool = False, rnp: bool = True,
           enrich_fn: Callable[..., Counterparty] | None = None,
           rnp_fn: Callable[[str, SourceCache], eis_rnp.RnpResult] | None = None) -> Verdict:
    """Check one candidate now (network unless `offline`); never raises on a source failure."""
    cache = cache or SourceCache()
    enrich_fn = enrich_fn or enrich_mod.enrich
    rnp_fn = rnp_fn or (lambda inn, c: eis_rnp.check(inn, c, offline=offline))
    down: list[str] = []
    inn = cp.inn
    if not inn or not inn_is_valid(inn):
        return Verdict(cp, False, "ИНН не подтверждён")
    # the candidate's own facts count only with a passing Evidence too (a stale card of discover(), F-07);
    # the INN itself is checked after the card below
    cp = _checked(cp, cache_pages_fresh(cache), keep_name=True).model_copy(update={"inn": inn})

    card = _card(inn, cache, offline, down, enrich_fn)
    if card is not None:
        cp = merge(cp, _checked(card, cache_pages_fresh(cache)))
    if is_active(cp.status) is None:  # no checked legal status: ГИР БО has one for legal entities
        bo = _bo_card(inn, cache, offline, down) if len(inn) == 10 else None
        if bo is not None:
            cp = merge(cp, _checked(bo, cache_pages_fresh(cache)))
    if strip_unverified(cp, cache_pages_fresh(cache)).inn is None:
        why = "ИНН не подтверждён источником" + (f" (не ответили: {', '.join(down)})" if down else "")
        return Verdict(cp, False, why, down)
    if cp.name.startswith("ИНН "):  # the file for the jury has no rows without a name (#229)
        return Verdict(cp, False, "название не подтверждено" + (f" (не ответили: {', '.join(down)})" if down else ""),
                       down)
    active = is_active(cp.status)
    if active is False:
        return Verdict(cp, False, f"статус: {cp.status}", down)
    if active is None:
        why = "статус не подтверждён" + (f" (не ответили: {', '.join(down)})" if down else "")
        return Verdict(cp, False, why, down)

    if not rnp:
        return Verdict(cp, True, f"проверен: {cp.status}; РНП не проверялся (--no-rnp)", down)
    try:
        res = rnp_fn(inn, cache)
    except (eis_rnp.Blocked, httpx.HTTPError, KeyError, ValueError, OSError) as e:
        log.warning("verify %s: РНП недоступен: %s", inn, e)
        down.append(RNP)
        return Verdict(cp, False, f"РНП не проверен ({type(e).__name__})", down)
    rnp_pages = cache_pages_fresh(cache)
    good = [e for e in res.evidence if strip_unverified(cp.model_copy(update={"evidence": [e]}), rnp_pages).evidence]
    cp = cp.model_copy(update={"in_rnp": res.in_rnp, "evidence": [*cp.evidence, *good]})
    if res.in_rnp is True:
        return Verdict(cp, False, "в РНП ЕИС", down)
    if res.in_rnp is None or not good:
        return Verdict(cp, False, "РНП: ответ не определён", down)
    return Verdict(cp, True, f"проверен: {cp.status}; в РНП нет", down)


def cache_pages_fresh(cache: SourceCache) -> CachedPages:
    """A new page view: a page fetched during this check is not in the view made before it."""
    return cached_pages(cache.page_text)
