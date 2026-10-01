"""Discovery by web search (#194, #186): Yandex Search → company site → INN → registry. Owner: SOURCES.

Yandex Search is paid, so it is the FALLBACK path only (decisions of the human of 02.10.2026, D-093):
`lot_candidates()` asks it for one lot or query, and only when the free sources left fewer than MIN_STRONG
strong candidates (product_fit 1.0 / 0.8) AND Yandex is switched on (the key and both ceilings in `.env` —
off by default, src/sources/yandex_search.py::gate). Otherwise it does nothing and says why. `plan_run()` /
`log_plan()` tell, before a run spends anything, how many lots fall under Yandex and the maximum of requests
and roubles. Queries: the text of the lot, cleaned (`clean_text`), × role modifiers by the type of the
purchase (ROLE_MODIFIERS). The site of a found company also gives its contacts, role phrases
(src/enrich/site_facts.py), the product_fit step and the role by the priority of bases (src/enrich/role.py) —
returned next to the candidates, NOT part of the score. Not connected to `predict` here (#229 owns it).
Agents never run this against the live API: tests use fixtures and a stub transport.

The group run below (a pool per OKPD2 group, #194 before the decision of 02.10) is kept behind the same gate
and ceilings — one group counts as one lot:

    uv run python -m src.enrich.web_discover Y-yandex-sub --groups 21.20 --offline   # cache only, free
    uv run python -m src.enrich.web_discover Y-yandex-sub --groups 21.20,20.14,10.51 --max-requests 50  # human only

Per group (configs/yandex_discovery.yaml): queries «template × product» → Yandex Search (paid, cached,
budget-limited: src/sources/yandex_search.py) → result domains minus aggregators / marketplaces /
directories (`skip_domains`) → the company site states its INN (src/sources/site.py: robots.txt, pacing,
labelled INN with a valid checksum, one INN per site) → registry card by enrich() (pb.nalog → МСП → ГИР
БО) → dropped: not found in the registry, liquidated, already in organizers' data (suppliers.parquet).
What is left is «new»: a Counterparty whose every fact is an Evidence (site page with the INN, the
search answer with the domain, the registry card); check_counterparty() must return no errors.

The pool of new companies is cached per group under `pool_url(group)` (JSON list of Counterparty), so the
block «Новые» and the demo read it without network; the result file `eval/results/<id>.json` has the
per-group funnel and the requests spent.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import yaml

from src.contracts import Counterparty, CounterpartyMatch, Evidence
from src.enrich import role, site_facts
from src.enrich.enrich import enrich
from src.enrich.evidence import check_counterparty
from src.ingest.load import suppliers_path
from src.match.filters import _DEAD
from src.match.store import processed_dir
from src.sources import site, yandex_search
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "yandex_discovery.yaml"
RESULTS = ROOT / "eval" / "results"


def pool_url(group: str) -> str:
    return f"pool://yandex_search/{group}"


def load_config(path: Path = CONFIG) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def queries(cfg: dict, group: str) -> list[str]:
    """«template × product» for the group, products outer: a cut by max_requests keeps every template."""
    out = [t.format(p=p) for p in cfg["groups"][group] for t in cfg["templates"]]
    return list(dict.fromkeys(out))[: int(cfg.get("max_requests_per_group", len(out)))]


def skipped(domain: str, skip: list[str]) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in skip)


def is_dead(status: str | None) -> bool:
    return bool(status) and any(w in status.lower() for w in _DEAD)


@dataclass
class Site:
    domain: str
    home: str
    search_url: str  # the cached search answer that listed the domain
    query: str
    inn: str | None = None
    inn_url: str | None = None
    # new / in_data / unchecked / no_inn / ambiguous / refused / not_in_registry / dead / evidence_failed
    outcome: str = ""
    reason: str = ""
    name: str | None = None
    region: str | None = None
    kind: str | None = None
    okved: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)  # the pages of the site that were read (all cached)


@dataclass
class GroupReport:
    group: str
    queries: int = 0
    requests_live: int = 0
    docs: int = 0
    domains: int = 0
    domains_skipped: int = 0
    sites: list[Site] = field(default_factory=list)

    def funnel(self) -> dict[str, int]:
        c = Counter(s.outcome for s in self.sites)
        return {"queries": self.queries, "requests_live": self.requests_live, "docs": self.docs,
                "domains": self.domains, "domains_skipped": self.domains_skipped,
                "sites_checked": len(self.sites), "inn_on_site": sum(1 for s in self.sites if s.inn),
                **{k: c.get(k, 0) for k in ("new", "in_data", "no_inn", "ambiguous", "refused", "not_in_registry",
                                             "dead", "evidence_failed", "unreachable", "unchecked")}}


def known(inn: str, processed: Path) -> bool | None:
    """INN present in organizers' data (all periods: «new» means new to the data); None — data absent."""
    from src.enrich.discover import _all_inns  # one cached read of suppliers.parquet, shared with discover()

    try:
        everyone = _all_inns(suppliers_path(None, processed))
    except OSError:
        return None
    return None if everyone is None else inn in everyone


def with_web_evidence(cp: Counterparty, s: Site, cache: SourceCache) -> Counterparty:
    """Registry card + the site page that states the INN + the search answer that listed the site."""
    ev = list(cp.evidence)
    page = cache.get(s.inn_url or "", allow_stale=True)
    if page is not None and s.inn:
        ev.append(Evidence(source=site.SOURCE, url=page.url, fetched_at=datetime.fromtimestamp(page.fetched_at, tz=UTC),
                           fact=f"ИНН {s.inn} указан на сайте {s.domain}", field="inn", value=s.inn))
    found = cache.get(s.search_url, allow_stale=True)
    if found is not None:
        ev.append(Evidence(source=yandex_search.SOURCE, url=found.url,
                           fetched_at=datetime.fromtimestamp(found.fetched_at, tz=UTC),
                           fact=f"Сайт {s.domain} найден в Яндекс Поиске по запросу «{s.query}»",
                           field="site", value=s.domain))
    return cp.model_copy(update={"evidence": ev})


def check_site(s: Site, cache: SourceCache, processed: Path, offline: bool,
               client: httpx.Client | None = None) -> Counterparty | None:
    """Fill the outcome of one site; the Counterparty when it is a new, verified company."""
    found = site.site_inn(s.home, cache, client, offline=offline)
    s.inn, s.inn_url, s.pages = found.inn, found.url, list(found.pages)
    if found.inn is None:
        s.reason = found.reason
        s.outcome = ("refused" if "не обходим" in found.reason or "robots" in found.reason
                     else "unreachable" if "недоступен" in found.reason
                     else "ambiguous" if "несколько" in found.reason else "no_inn")
        return None
    try:
        cp = enrich(found.inn, cache, offline=offline)
    except (KeyError, ValueError, TimeoutError, httpx.HTTPError, RuntimeError) as e:
        s.outcome, s.reason = "not_in_registry", f"{type(e).__name__}: {e}"[:200]
        return None
    s.name, s.region, s.kind, s.okved = cp.name, cp.region, cp.kind, list(cp.okved)
    if is_dead(cp.status):
        s.outcome, s.reason = "dead", cp.status or ""
        return None
    in_data = known(found.inn, processed)
    if in_data is None:  # as discover(): without organizers' data nobody is called «new»
        s.outcome, s.reason = "unchecked", "данные организаторов не загружены — сверка не выполнена"
        return None
    if in_data:
        s.outcome = "in_data"
        return None
    cp = with_web_evidence(cp, s, cache)
    errors = check_counterparty(cp, cache.page_text)
    if errors:
        s.outcome, s.reason = "evidence_failed", "; ".join(errors)[:300]
        return None
    s.outcome = "new"
    return cp


def run_group(group: str, cfg: dict, cache: SourceCache, budget: yandex_search.Budget, processed: Path,
              offline: bool, max_requests: int, seen_domains: set[str],
              caps: yandex_search.Caps | None = None) -> tuple[GroupReport, list[Counterparty]]:
    """One OKPD2 group; live requests are bounded by `max_requests`, by the ceilings (the group is one lot)
    and by the hard limit of the budget."""
    rep = GroupReport(group=group)
    caps = caps or yandex_search.run_caps()
    pool: list[Counterparty] = []
    skip = list(cfg.get("skip_domains", []))
    region = str(cfg.get("region", yandex_search.REGION_SPB))
    for q in queries(cfg, group):
        for page_no in range(int(cfg.get("pages", 1))):
            url = yandex_search.search_url(q, page_no, region)
            cached = cache.get(url) is not None
            if not cached and not offline and rep.requests_live >= max_requests:
                log.info("yandex: группа %s — лимит запросов прогона %d", group, max_requests)
                break
            before = caps.used_run
            try:
                page = yandex_search.search(q, page_no, region, cache, offline=offline, budget=budget, caps=caps,
                                            lot=f"group:{group}")
            except yandex_search.Blocked as e:  # off, a ceiling, the hard limit or a refusal: stop
                log.warning("%s", e)
                rep.requests_live += caps.used_run - before
                return rep, pool
            except yandex_search.OperationFailed as e:
                log.warning("%s", e)
                page = None
            rep.queries += 1
            rep.requests_live += caps.used_run - before  # a cached answer and a status request cost nothing
            if page is None:
                continue
            docs = yandex_search.parse(page.body).docs
            rep.docs += len(docs)
            for d in docs:
                dom = yandex_search.domain_of(d.url)
                if dom in seen_domains:
                    continue
                seen_domains.add(dom)
                rep.domains += 1
                if skipped(dom, skip):
                    rep.domains_skipped += 1
                    continue
                home = f"{d.url.split('://', 1)[0]}://{d.url.split('://', 1)[1].split('/', 1)[0]}/"
                s = Site(domain=dom, home=home, search_url=url, query=q)
                cp = check_site(s, cache, processed, offline)
                rep.sites.append(s)
                log.info("yandex %s: %-30s %-16s %s %s", group, dom, s.outcome, s.inn or "", s.reason[:80])
                if cp is not None:
                    pool.append(cp)
    cache.put(pool_url(group), json.dumps([cp.model_dump(mode="json") for cp in pool], ensure_ascii=False))
    return rep, pool


def read_pool(group: str, cache: SourceCache | None = None) -> list[Counterparty]:
    """New companies found for the group (cached by the last run); [] when there was no run."""
    page = (cache or SourceCache()).get(pool_url(group), allow_stale=True)
    return [Counterparty.model_validate(d) for d in json.loads(page.body)] if page else []


# --- the fallback path: one lot → candidates (#194 / #186, D-093) ------------------------------------------

MIN_STRONG = 3  # fewer strong candidates from the free sources → Yandex may be asked (the human, 02.10)
PurchaseKind = Literal["goods", "services"]
# Role modifiers by the type of the purchase (#186): goods; services and works.
ROLE_MODIFIERS: dict[PurchaseKind, tuple[str, ...]] = {
    "goods": ("производитель", "оптом", "официальный дилер"),
    "services": ("исполнитель", "подрядчик", "аккредитованная лаборатория"),
}
GOODS_CLASSES = range(1, 34)  # OKPD2 01–33 are goods (#194); the rest — services and works
MAX_QUERY_WORDS = 8  # of the cleaned lot text: a long subject of a lot finds nothing as one phrase
MAX_QUERY_CHARS = 400  # SearchQuery.query_text of the API specification
_YEAR = re.compile(r"(?<!\w)(?:(?:на|в|за)\s+)?20[1-3]\d(?:\s*[-–—/]\s*20[1-3]\d)?"
                   r"(?:\s*(?:годах|годов|году|года|год|гг\.?|г\.?))?(?!\w)", re.I)
_POINTS = re.compile(r"(?<!\w)в\s+\d+\s*(?:-?\s*[а-яё]{1,2}\s+)?точк(?:е|ах|у)(?!\w)", re.I)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|«\s*»|\"\s*\"")
_EDGE = " \t\n,;:.-–—/"


def clean_text(text: str) -> str:
    """The text of a lot or a product name as a search phrase (#186): years («на 2025 год», «2024-2025 гг.»)
    and «в 1 точке» are cut out, what is left is one line without stray punctuation."""
    out = _POINTS.sub(" ", _YEAR.sub(" ", text))
    out = _EMPTY_BRACKETS.sub(" ", out)
    out = re.sub(r"\s+([,;.])", r"\1", " ".join(out.split()))
    return re.sub(r"([,;])\1+", r"\1", out).strip(_EDGE)


def _same(a: str, b: str) -> bool:
    def norm(x: str) -> str:
        return " ".join(x.lower().replace("ё", "е").split()).strip(_EDGE)
    return norm(a) == norm(b)


def clean_names(names: Sequence[str], okpd_names: Sequence[str] = ()) -> list[str]:
    """Product names for the queries (#186): cleaned, without those that merely repeat the name of an OKPD2
    code (too general to find a supplier), without repeats, in the given order."""
    out: list[str] = []
    for raw in names:
        name = clean_text(raw)
        if name and not any(_same(name, o) for o in okpd_names) and not any(_same(name, o) for o in out):
            out.append(name)
    return out


def purchase_kind(okpd2: Sequence[str]) -> PurchaseKind:
    """Goods (OKPD2 01–33) or services and works, by the first code; no code → goods (most lots of the ЭМ)."""
    for code in okpd2:
        head = code.strip()[:2]
        if head.isascii() and head.isdigit():
            return "goods" if int(head) in GOODS_CLASSES else "services"
    return "goods"


def lot_queries(text: str, okpd2: Sequence[str] = (), kind: PurchaseKind | None = None) -> list[str]:
    """«cleaned lot text × role modifier» — one query per modifier of the purchase type; [] for an empty text."""
    base = " ".join(clean_text(text).split()[:MAX_QUERY_WORDS]).strip(_EDGE)
    if not base:
        return []
    mods = ROLE_MODIFIERS[kind or purchase_kind(okpd2)]
    return [f"{base[:MAX_QUERY_CHARS - len(m) - 1].rstrip()} {m}" for m in mods]


def strong_count(matches: Sequence[CounterpartyMatch]) -> int:
    """Candidates whose product_fit is one of the strong steps (1.0 / 0.8)."""
    return sum(1 for m in matches
               if any(c.name == "product_fit" and c.value >= site_facts.STRONG_FIT for c in m.components))


def fallback_needed(strong: int, gate: yandex_search.Gate | None = None) -> tuple[bool, str]:
    """(ask Yandex?, why). Off → «выключен: …»; enough strong candidates → «не нужен: …»."""
    gate = gate or yandex_search.gate()
    if not gate.enabled:
        return False, gate.reason
    if strong >= MIN_STRONG:
        return False, f"не нужен: сильных кандидатов из бесплатных источников {strong} ≥ {MIN_STRONG}"
    return True, f"нужен: сильных кандидатов из бесплатных источников {strong} < {MIN_STRONG}"


@dataclass(frozen=True)
class Plan:
    """What a run may cost at most, known before the first request."""

    enabled: bool
    reason: str
    mode: yandex_search.Mode
    lots: int
    lots_yandex: int  # lots with fewer than MIN_STRONG strong candidates (0 while Yandex is off)
    requests_max: int
    rub_max: float
    per_lot: int = 0
    per_run: int = 0
    budget_left: int = 0

    def line(self) -> str:
        if not self.enabled:
            return (f"Yandex (запасной путь) {self.reason}: из {self.lots} лотов под Yandex не попадает ни один, "
                    "запросов 0, 0 ₽")
        price = yandex_search.PRICE_RUB_PER_1000[self.mode]
        return (f"Yandex (запасной путь) {self.reason}: под Yandex попадают {self.lots_yandex} из {self.lots} лотов "
                f"(сильных кандидатов < {MIN_STRONG}); максимум {self.requests_max} запросов ≈ {self.rub_max:.2f} ₽ "
                f"(режим {self.mode}, {price} ₽ за 1000); остаток общего лимита {self.budget_left}")


def plan_run(strong_by_lot: Mapping[str, int], *, gate: yandex_search.Gate | None = None,
             mode: yandex_search.Mode | None = None, budget_left: int | None = None) -> Plan:
    """The plan of a run from the number of strong candidates of every lot (free sources only). The maximum
    of requests: lots under Yandex × min(ceiling per lot, queries of one lot), not above the ceiling per run
    and the rest of the hard limit. Cached answers make the real number smaller."""
    gate = gate or yandex_search.gate()
    mode = mode or yandex_search.search_mode()
    if not gate.enabled:
        return Plan(False, gate.reason, mode, len(strong_by_lot), 0, 0, 0.0)
    if budget_left is None:
        budget_left = yandex_search.Budget().left()
    under = sum(1 for n in strong_by_lot.values() if n < MIN_STRONG)
    per_lot = min(gate.per_lot, max(len(m) for m in ROLE_MODIFIERS.values()))
    requests = max(0, min(under * per_lot, gate.per_run, budget_left))
    return Plan(True, gate.reason, mode, len(strong_by_lot), under, requests, yandex_search.cost_rub(requests, mode),
                gate.per_lot, gate.per_run, budget_left)


def log_plan(plan: Plan) -> str:
    """The line every run writes before its first request (WARNING: it is about money)."""
    line = plan.line()
    log.warning("%s", line)
    return line


@dataclass
class Fallback:
    """The answer of the fallback path for one lot."""

    lot: str
    used: bool  # Yandex was asked (or its cached answers were read) for this lot
    reason: str
    queries: list[str] = field(default_factory=list)
    requests_live: int = 0  # paid requests of this call
    pending: int = 0  # deferred requests whose answer is not ready: the next run collects them
    sites: list[Site] = field(default_factory=list)
    candidates: list[Counterparty] = field(default_factory=list)  # new companies, every fact with an Evidence
    fits: dict[str, site_facts.FitStep] = field(default_factory=dict)  # INN → product_fit step (v0, not in score)
    roles: dict[str, role.RoleDecision] = field(default_factory=dict)  # INN → role by the priority of bases


def _offline_env() -> bool:
    return os.environ.get("DISCOVERY_OFFLINE") == "1" or os.environ.get("ENRICH_OFFLINE") == "1"


def _with_site_facts(cp: Counterparty, s: Site, name: str, okpd2: Sequence[str], cache: SourceCache,
                     out: Fallback) -> Counterparty:
    """Contacts and role phrases of the company's cached pages; the product_fit step and the role decision go
    to `out`. A fact that fails the Evidence check is not added: the card stays as the registry gave it."""
    from src.enrich.discover import okpd_classes, wholesale_okveds  # the OKPD2 → OKVED table lives there

    inn = cp.inn or ""
    pages = [p for p in (cache.get(u, allow_stale=True) for u in s.pages) if p is not None and p.status == 200]
    facts = site_facts.merge(site_facts.extract(p, s.domain, individual=len(inn) == 12) for p in pages)
    richer = cp.model_copy(update={"contacts": {**facts.contacts(), **cp.contacts},
                                   "evidence": [*cp.evidence, *facts.evidence()]})
    errors = check_counterparty(richer, cache.page_text)
    if errors:
        log.warning("web_discover: факты сайта %s не прошли Evidence и не добавлены: %s", s.domain, errors[:3])
        richer, facts = cp, site_facts.SiteFacts(url=s.home)

    okved = next(iter(cp.okved), None)
    okved_ev = next((e for e in cp.evidence if e.field == "okved"), None)
    fit_facts = [f for f in (site_facts.name_fact(name, p) for p in pages) if f is not None][:1]
    for code in okpd2:
        for cls in okpd_classes([code]):
            f = site_facts.okved_fact(okved, cls, wholesale_okveds(code.strip()), okved_ev)
            if f is not None:
                fit_facts.append(f)
    fit = site_facts.new_company_fit(fit_facts)
    if fit is not None:
        out.fits[inn] = fit
    said = facts.role()
    phrase = next((r for r in facts.roles if r.kind == said), None)
    out.roles[inn] = role.decide(site=role.site_basis(phrase.kind, phrase.phrase, phrase.evidence) if phrase else None,
                                 okved=role.okved_basis(okved, evidence=okved_ev))
    return richer


def lot_candidates(lot: str, text: str, strong: int, *, okpd2: Sequence[str] = (), kind: PurchaseKind | None = None,
                   cache: SourceCache | None = None, budget: yandex_search.Budget | None = None,
                   caps: yandex_search.Caps | None = None, processed: Path | None = None, cfg: dict | None = None,
                   client: httpx.Client | None = None, site_client: httpx.Client | None = None,
                   offline: bool | None = None, wait_s: float = 0.0) -> Fallback:
    """The fallback path for one lot or query: `text` — its subject, `strong` — how many strong candidates
    (product_fit 1.0 / 0.8) the free sources gave. Yandex off or `strong` ≥ MIN_STRONG → nothing is done, the
    reason is returned. Otherwise: queries of the lot → Yandex (cache first; live requests within the ceilings
    of the lot and of the run and the hard limit) → sites → new companies with Evidence.
    offline (default: DISCOVERY_OFFLINE / ENRICH_OFFLINE) → cached answers and pages only."""
    need, why = fallback_needed(strong)
    out = Fallback(lot=lot, used=False, reason=why)
    if not need:
        return out
    out.queries = lot_queries(text, okpd2, kind)
    if not out.queries:
        out.reason = "не выполнен: в тексте лота нет слов для запроса"
        return out
    cache = cache or SourceCache()
    caps = caps or yandex_search.run_caps()
    cfg = cfg or load_config()
    processed = processed or processed_dir()
    offline = _offline_env() if offline is None else offline
    skip, region = list(cfg.get("skip_domains", [])), str(cfg.get("region", yandex_search.REGION_SPB))
    name = " ".join(clean_text(text).split()[:MAX_QUERY_WORDS]).strip(_EDGE)
    out.used = True
    notes: list[str] = []
    seen: set[str] = set()
    for q in out.queries:
        url = yandex_search.search_url(q, 0, region)
        before = caps.used_run
        try:
            page = yandex_search.search(q, 0, region, cache, client, offline, budget, caps=caps, lot=lot,
                                        wait_s=wait_s)
        except yandex_search.Blocked as e:  # a ceiling, the hard limit, a refusal: no more requests for the lot
            out.requests_live += caps.used_run - before
            notes.append(str(e))
            break
        except (yandex_search.OperationFailed, httpx.HTTPError, ValueError) as e:
            out.requests_live += caps.used_run - before
            notes.append(f"запрос «{q}» не выполнен: {type(e).__name__}")
            continue
        out.requests_live += caps.used_run - before
        if page is None:
            out.pending += 0 if offline else 1
            continue
        for d in yandex_search.parse(page.body).docs:
            dom = yandex_search.domain_of(d.url)
            if not dom or dom in seen:
                continue
            seen.add(dom)
            if skipped(dom, skip):
                continue
            s = Site(domain=dom, home=f"{d.url.split('://', 1)[0]}://{d.url.split('://', 1)[1].split('/', 1)[0]}/",
                     search_url=url, query=q)
            cp = check_site(s, cache, processed, offline, site_client)
            out.sites.append(s)
            if cp is not None:
                out.candidates.append(_with_site_facts(cp, s, name, okpd2, cache, out))
    out.reason = (f"{why}; запросов {len(out.queries)}, из них платных {out.requests_live}, ответ ещё не готов у "
                  f"{out.pending}; сайтов проверено {len(out.sites)}, новых компаний {len(out.candidates)}"
                  + ("; " + "; ".join(notes) if notes else ""))
    return out


def build_record(exp_id: str, reports: list[GroupReport], budget_before: int, budget_after: int, limit: int,
                 offline: bool) -> dict:
    from src.eval.run import git

    groups = []
    for r in reports:
        groups.append({"group": r.group, "funnel": r.funnel(),
                       "new": [{"inn": s.inn if s.kind != "ip" else None, "name": s.name, "domain": s.domain,
                                "region": s.region, "kind": s.kind, "okved": s.okved[:1], "query": s.query}
                               for s in r.sites if s.outcome == "new"],
                       "in_data": [{"inn": s.inn, "domain": s.domain} for s in r.sites if s.outcome == "in_data"]})
    total = Counter()
    for r in reports:
        total.update(r.funnel())
    return {
        "exp_id": exp_id, "kind": "yandex_discovery", "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git("rev-parse", "--short", "HEAD"), "dirty": bool(git("status", "--porcelain")),
        "config": {"file": "configs/yandex_discovery.yaml", "groups": [r.group for r in reports], "offline": offline},
        "budget": {"limit": limit, "used_before": budget_before, "used_after": budget_after,
                   "spent_by_run": budget_after - budget_before},
        "metrics": dict(total), "groups": groups,
        "notes": "new = ИНН указан на сайте (контрольные цифры), найден в реестре ФНС, не ликвидирован, нет в "
                 "Поставщиках организаторов, check_counterparty() без ошибок; релевантность предмету закупки "
                 "не размечалась. ИНН ИП в файл не пишется (152-ФЗ).",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discovery через Yandex Search API по тонким группам ОКПД2 (#194)")
    ap.add_argument("exp_id")
    ap.add_argument("--groups", required=True, help="группы ОКПД2 XX.XX через запятую (configs/yandex_discovery.yaml)")
    ap.add_argument("--max-requests", type=int, default=50, help="платных запросов на весь прогон (кеш бесплатен)")
    ap.add_argument("--offline", action="store_true", help="только кеш, без сети и без трат")
    ap.add_argument("--config", type=Path, default=CONFIG)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    cfg = load_config(args.config)
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    missing = [g for g in groups if g not in cfg["groups"]]
    if missing:
        ap.error(f"нет в конфиге: {missing}")
    budget = yandex_search.Budget()
    before = budget.used()
    if not args.offline and not yandex_search.gate().enabled:
        ap.error(f"Yandex Search API {yandex_search.gate().reason} — платный прогон не запускаем (--offline: кеш)")
    if not args.offline and args.max_requests > budget.left():
        ap.error(f"--max-requests {args.max_requests} больше остатка лимита {budget.left()}")
    cache, processed = SourceCache(), processed_dir()
    if not args.offline and known("0000000000", processed) is None:  # paid answers would give no «new» at all
        ap.error("данные организаторов не загружены (нет suppliers.parquet) — платный прогон не запускаем")
    reports, seen, spent = [], set(), 0
    for g in groups:
        rep, _ = run_group(g, cfg, cache, budget, processed, args.offline, args.max_requests - spent, seen)
        spent += rep.requests_live
        reports.append(rep)
    rec = build_record(args.exp_id, reports, before, budget.used(), budget.limit, args.offline)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp_id}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"metrics": rec["metrics"], "budget": rec["budget"],
                      "per_group": {g["group"]: g["funnel"] for g in rec["groups"]}}, ensure_ascii=False, indent=1))
    print(f"-> {out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
