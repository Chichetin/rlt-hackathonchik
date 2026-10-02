"""One «Подбор» pipeline for /api/search, make eval and make predict (#220 №1). Owner: APP.

Before #220 it was built in three places, each differently: the API enriched from the cache and filtered,
eval ranked bare search() (no enrichment, no hard filters — the slide was not measured on what the jury
sees), predict wrote facts that never passed the Evidence check. Now all three call `pick()` and differ
only in options:

    spelling of the query                        layout and typos, config `spell` (src/match/speller.py, #282)
    search(k = CANDIDATES)                       organizers' history, scored (src/match/search.py)
      → enrichment from the cache                status, МСП, role, name — never the network here
      → block «Новые» (discover)                 optional: eval has no use for it (never relevant)
      → role for the purchase type               services, works: `unknown` with its basis (role.for_purchase, #188)
      → hard filters                             liquidated / bankrupt / in РНП (src/match/filters.py)
      → user filters                             query.filters
    verified(matches)                            the Evidence check of what is shown (card, xlsx, CSV)

`backtest=True` (make eval / eval-final, research/DECISIONS.md D-097): the main list does not depend on the
cache at all — status, РНП, МСП, role and region in the cache are facts of today, not of the lot date
(a leak in a backtest, and a different number on every machine). So no cached enrichment, no «Новые»,
no hard filters, no network or queue; a filter on a cached field (only_msp, kinds) is an error. What
today's hard filters would have removed is measured apart, outside the slide metrics: `today_audit()`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from src.contracts import CounterpartyMatch, Query, inn_is_valid
from src.enrich import discover as discover_mod
from src.enrich import enrich as enrich_mod
from src.enrich.evidence import cached_pages, strip_unverified
from src.match import confidence, fallback, ranker, speller
from src.match import search as search_mod
from src.match.filters import apply_legal, apply_user_filters, chip_facets, legal_ok, parse_filters
from src.match.index import get_index
from src.match.purchase_type import purchase_type
from src.sources import role
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)

Facets = dict[str, dict[str, dict[str, int]]]


@dataclass
class Picked:
    matches: list[CounterpartyMatch]  # history first, then «Новые»; best first within a block
    note: str | None = None  # why history is empty (#113) or «фильтры скрыли N»
    facets: Facets = field(default_factory=dict)  # X-Facets of /api/search: each chip group after the OTHER groups
    uncached: set[str] = field(default_factory=set)  # history INNs without a cached card (queue them)
    discovery: discover_mod.Discovery | None = None  # None — the block was not asked for or failed
    discovery_error: str | None = None  # the exception type when discover() failed
    correction: speller.Correction | None = None  # the query was searched as corrected (#282)
    weak_match: str | None = None  # X-Weak-Match (#281): the match with past lots is weak — the sentence for the user

    @property
    def history(self) -> list[CounterpartyMatch]:
        return [m for m in self.matches if m.origin == "history"]

    @property
    def discovered(self) -> list[CounterpartyMatch]:
        return [m for m in self.matches if m.origin == "discovered"]


def is_stub() -> bool:
    return bool(getattr(search_mod, "IS_STUB", False))


def pick(query: Query, *, config: dict | None = None, discovery: bool = True, backtest: bool = False,
         enqueue: Callable[[str], object] | None = None, offline: bool | None = None) -> Picked:
    """Every candidate of the query after the filters, best first. FileNotFoundError — data not built.

    «Новые» INNs are queued by discover() itself (`enqueue`); history INNs without a cache are only
    collected in `uncached` — the caller queues the visible page. The hard filters run on the enriched
    rows; the cached card passes strip_unverified BEFORE the filters, so «только производители» keeps
    only a role whose basis passed the check.
    """
    if backtest:
        if query.as_of is None:
            raise ValueError(f"{query.query_id}: backtest без as_of — это не backtest")
        uf = parse_filters(query.filters)
        if uf.only_msp or uf.kinds is not None:
            raise ValueError(f"{query.query_id}: фильтр МСП / роли в backtest — это факты кеша на сегодня (D-097)")
    correction = _spelled(query, config)
    if correction is not None:
        query = query.model_copy(update={"text": correction.text})
    # `config` only when given: the product reads configs/search.yaml on every call (and tests stub search())
    kw = {"config": config, "weights": config.get("weights")} if config is not None else {}
    found = search_mod.search(query, k=search_mod.CANDIDATES, **kw)
    predicted = search_mod.predicted_code()  # the class of «Новые» for a query without a code (#394)
    weak = confidence.weak_match(search_mod.fit_signals(), has_code=bool(query.okpd2))
    out = Picked(matches=[], note=fallback.search_note(), correction=correction, weak_match=weak)
    candidates: list[CounterpartyMatch] | None = None  # «Новые» before the user filters, None — no discovery
    if not is_stub() and not backtest:
        discovered: list[CounterpartyMatch] = []
        if discovery:
            discovered, candidates, out.discovery, out.discovery_error = _discovered(query, found, enqueue, offline,
                                                                                    predicted)
        found = [*enriched(found, out.uncached), *discovered]
        # services and works (#188): the role is `unknown` with its basis — before the filters and the chips
        found = _for_purchase(found, query)
        candidates = _for_purchase(candidates, query) if candidates is not None else None
    legal = found if backtest else apply_legal(found)
    # why above the next one — against the neighbour left after the filters, the one the customer sees (#278)
    shown = ranker.reasons(apply_user_filters(legal, query))
    hidden = sum(m.origin == "history" for m in found) - sum(m.origin == "history" for m in shown)
    if hidden and not any(m.origin == "history" for m in shown):
        out.note = f"фильтры скрыли {hidden}"
        log.info("search %s: %s", query.query_id, out.note)
    # each chip group counted after the user filters of the other groups (review of #240, #242, D-094)
    new = chip_facets((m for m in legal if m.origin == "discovered"), query)
    if candidates is not None:  # discovery: the block is cut to MAX_SHOWN after the user filters
        new = {g: {v: min(n, discover_mod.MAX_SHOWN) for v, n in counts.items()}
               for g, counts in chip_facets(apply_legal(candidates), query).items()}
    out.matches = shown
    out.facets = {"history": chip_facets((m for m in legal if m.origin == "history"), query), "discovered": new}
    return out


def _for_purchase(matches: list[CounterpartyMatch], query: Query) -> list[CounterpartyMatch]:
    """The role of every candidate for the purchase type of the query (role.for_purchase, #188)."""
    kind = purchase_type(query.okpd2)
    if kind not in role.SERVICE_DATIVE:
        return matches
    return [m.model_copy(update={"counterparty": role.for_purchase(m.counterparty, kind)}) for m in matches]


def _spelled(query: Query, config: dict | None) -> speller.Correction | None:
    """The query with its layout and typos fixed by the dictionary of the index it is searched in (#282).

    On when the configuration says `spell: true` and the user did not ask for the query as typed
    (`filters.spell = "off"`); None — nothing changed, off, or the speller failed (the search goes on as typed).
    """
    if is_stub() or str(query.filters.get("spell", "")).lower() == "off":
        return None
    try:
        cfg = config if config is not None else search_mod.load_config()
        if not cfg.get("spell", False):
            return None
        fixed = speller.for_index(get_index(None, query.as_of).path).correct(query.text)
    except Exception:  # noqa: BLE001 — no index / a broken dictionary never breaks the search (data absent: search says so)
        log.exception("speller: запрос %s ищется как введён", query.query_id)
        return None
    return fixed if fixed.changed else None


def with_reliability(matches: list[CounterpartyMatch], as_of: date | None) -> list[CounterpartyMatch]:
    """The reliability of what the user sees (#72, shadow): the page of /api/search, the rows of an export — not
    every candidate pick() scored. Weight 0: score and order stay."""
    return search_mod.shadow_reliability(matches, as_of)


def verified(matches: list[CounterpartyMatch], open_data: bool = True) -> list[CounterpartyMatch]:
    """What the card, the xlsx and the predict CSV show: ФНС open data added (only here, not in the loop over all
    candidates), then only facts and contacts that pass the Evidence check. `open_data=False` in a backtest:
    the facts are dated today, later than `as_of` (D-097)."""
    if is_stub():
        return matches
    cache = SourceCache()
    # one query for the whole list; before cached_pages(), because the facts' texts land in the cache here
    cards = enrich_mod.add_open_data([m.counterparty for m in matches], cache) if open_data else None
    shown = ([m.model_copy(update={"counterparty": cp}) for m, cp in zip(matches, cards, strict=True)]
             if cards is not None else matches)
    pages = cached_pages(cache.page_text)  # one read and one normalization per page per call
    return [m.model_copy(update={"counterparty": strip_unverified(m.counterparty, pages)}) for m in shown]


def enriched(history: list[CounterpartyMatch], uncached: set[str]) -> list[CounterpartyMatch]:
    """Mix the cached enrichment (status, МСП, name, kind, ...) into history candidates BEFORE the hard filters (#38).

    Cache only, never the network: no cache -> the INN is added to `uncached` and the fields stay None
    («не проверено»). Organizers' data (inn, kpp, stats) is kept.
    """
    cache = SourceCache()
    pages = cached_pages(cache.page_text)  # one read and normalization per page for the whole list
    out = []
    for m in history:
        cp = m.counterparty
        if m.origin != "history" or not cp.inn or not inn_is_valid(cp.inn):
            out.append(m)
            continue
        try:
            # filters see only facts that pass the Evidence check, as the card does (strip before filters)
            card = strip_unverified(enrich_mod.enrich(cp.inn, cache, offline=True, open_data=False), pages)
        except KeyError:  # nothing cached for this INN
            uncached.add(cp.inn)
            out.append(m)
            continue
        except Exception:  # noqa: BLE001 — a broken cached page must not break the search
            log.exception("enrich из кеша: %s", cp.inn)
            out.append(m)
            continue
        upd: dict[str, object] = {"evidence": [*cp.evidence, *card.evidence]}
        if cp.name.startswith("ИНН ") and not card.name.startswith("ИНН "):
            upd["name"] = card.name
        for f in ("kind", "region", "status", "is_msp", "in_rnp"):
            if getattr(card, f) is not None:
                upd[f] = getattr(card, f)
        if card.okved:
            upd["okved"] = card.okved
        upd["contacts"] = {**card.contacts, **cp.contacts}
        out.append(m.model_copy(update={"counterparty": cp.model_copy(update=upd)}))
    return out


def _discovered(query: Query, history: list[CounterpartyMatch], enqueue: Callable[[str], object] | None,
                offline: bool | None, predicted: str | None = None
                ) -> tuple[list[CounterpartyMatch], list[CounterpartyMatch], discover_mod.Discovery | None, str | None]:
    """Block «Новые» (#16, D-037): open sources via the cache; a failing source never breaks the search.

    `predicted` — the OKPD2 code search() predicted from the text (search.predicted_code(), P-02): the class of
    the block for a query without a code (#394). Returns the block (top MAX_SHOWN), every candidate before the
    user filters (X-Facets) and the Discovery.
    """
    try:
        # the offline pool (#185, #219) joins «Новые» as in make predict (#518): no network, cache and parquet only
        res = discover_mod.discover(query, history, enqueue=enqueue, offline=offline, predicted_code=predicted,
                                    pool=True)
    except Exception as e:  # noqa: BLE001 — the history block must be shown whatever happens to an open source
        log.exception("discovery: блок «Новые» пуст из-за ошибки")
        return [], [], None, type(e).__name__
    log.info("discovery %s: %d компаний; %s", query.query_id, len(res.matches), "; ".join(res.requests + res.notes))
    # «новые» only when the companies were checked against organizers' data
    if not res.history_checked:
        return [], [], res, None
    # matches ⊆ candidates: a Discovery built without candidates (a test double) still counts its own block
    return res.matches, res.candidates or res.matches, res, None


@dataclass
class TodayAudit:
    """What today's cache says about a backtest top-k (outside the metrics: D-097)."""
    checked: int = 0  # INNs of the top-k looked up
    cached: int = 0  # with a cached card
    dropped: int = 0  # the card fails legal_ok today (liquidated, bankrupt, in РНП)
    dropped_relevant: int = 0  # ... and the INN is relevant for the case
    cases_with_dropped_relevant: int = 0
    k: int = 10  # the top-k looked at
    _cards: dict[str, bool | None] = field(default_factory=dict, repr=False)  # inn -> legal today; None — no card

    def add(self, ranked: list[str], relevant: set[str]) -> None:
        cache: SourceCache | None = None
        lost = 0
        for inn in ranked[: self.k]:
            if inn not in self._cards:
                cache = cache or SourceCache()
                try:
                    self._cards[inn] = legal_ok(enrich_mod.enrich(inn, cache, offline=True, open_data=False))
                except KeyError:  # nothing cached
                    self._cards[inn] = None
                except Exception:  # noqa: BLE001 — a broken cached page is «no card», never a failed eval
                    log.exception("today_audit: %s", inn)
                    self._cards[inn] = None
            ok = self._cards[inn]
            self.checked += 1
            self.cached += ok is not None
            if ok is False:
                self.dropped += 1
                lost += inn in relevant
        self.dropped_relevant += lost
        self.cases_with_dropped_relevant += lost > 0

    def record(self) -> dict:
        return {"k": self.k, "checked": self.checked, "cached": self.cached,
                "cached_share": round(self.cached / self.checked, 4) if self.checked else None,
                "legal_dropped": self.dropped, "legal_dropped_relevant": self.dropped_relevant,
                "cases_with_legal_dropped_relevant": self.cases_with_dropped_relevant,
                "note": f"сегодняшний кеш ФНС/РНП по top-{self.k} backtest; в метрики и bootstrap не входит (D-097)"}

