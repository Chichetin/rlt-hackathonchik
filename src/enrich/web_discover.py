"""Discovery by web search in thin OKPD2 groups (#194): Yandex Search → company site → INN → registry. Owner: SOURCES.

    uv run python -m src.enrich.web_discover Y-yandex-sub --groups 21.20,20.14,10.51 --max-requests 50
    uv run python -m src.enrich.web_discover Y-yandex-sub --groups 21.20 --offline   # cache only, free

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
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from src.contracts import Counterparty, Evidence
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


def check_site(s: Site, cache: SourceCache, processed: Path, offline: bool) -> Counterparty | None:
    """Fill the outcome of one site; the Counterparty when it is a new, verified company."""
    found = site.site_inn(s.home, cache, offline=offline)
    s.inn, s.inn_url = found.inn, found.url
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
              offline: bool, max_requests: int, seen_domains: set[str]) -> tuple[GroupReport, list[Counterparty]]:
    rep = GroupReport(group=group)
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
            try:
                page = yandex_search.search(q, page_no, region, cache, offline=offline, budget=budget)
            except yandex_search.Blocked as e:
                log.warning("%s", e)
                return rep, pool
            rep.queries += 1
            rep.requests_live += 0 if cached or offline else 1
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
