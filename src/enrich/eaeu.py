"""Companies of the EAEU countries in the block «Новые» (#200). Owner: SOURCES.

Goods only: goods of the EAEU are admitted on the national regime, for a service geography matters (#104).
`configs/eaeu/leads.csv` (`;`, UTF-8): one row per company a web search returned for an OKPD2 class — the country,
the national identifier as the company's own page shows it, the page. A lead is a CANDIDATE, not a fact (as
src/sources/web_leads.py): nothing of the row is shown as a fact. The block takes a lead only when the national
registry confirms the identifier — its answer is in the source cache — and names the company as active; the name,
the address and the status then come from that answer with its own Evidence. Registries connected: Belarus — the
register of payers, src/sources/by_grp.py; Armenia — the register of legal entities, src/sources/am_register.py
(#533). Recorded answers
of the registries (`configs/eaeu/grp/<УНП>.json`, `configs/eaeu/am/<ՀՎՀՀ>.json`, real requests) are put into the
cache when it has none, so the demo shows the companies without the network.

What is NOT established and is said so in the row: these registries hold no activity codes, so the product
and the role are not confirmed by a registry — product_fit is 0.0 («не подтверждено реестром»), geography is 0.0
(another country; proximity is not assessed). The score is the same formula of the same components, so such a row
stands after the block's own rows: it widens the pool, it does not compete in the ranking.
`DISCOVERY_EAEU=0` switches the channel off.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.contracts import EAEU_COUNTRIES, Counterparty, CounterpartyMatch, ScoreComponent
from src.enrich.evidence import strip_unverified
from src.settings import get_settings
from src.sources import am_register, by_grp
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
LEADS = ROOT / "configs" / "eaeu" / "leads.csv"
RECORDED = ROOT / "configs" / "eaeu" / "grp"  # Belarus
RECORDED_AM = ROOT / "configs" / "eaeu" / "am"
REGISTRY_NAME = {"BY": "Реестр плательщиков Беларуси", "AM": "Регистр юридических лиц Армении"}
COLUMNS = ("okpd2_class", "country", "national_id", "name", "site", "source_url", "search_query", "found_at")
COUNTRY_NAME = {"BY": "Беларусь", "KZ": "Казахстан", "AM": "Армения", "KG": "Кыргызстан"}
GOODS_CLASSES = range(1, 34)  # OKPD2 01–33 are goods (as src/enrich/web_discover.py)
MAX_SHOWN = 3  # EAEU rows added to the block per query
COMPONENT_ORDER = ("product_fit", "experience", "reliability", "activity", "geography", "price")


@dataclass(frozen=True)
class Lead:
    okpd2_class: str
    country: str
    national_id: str
    name: str  # as the search showed it — never shown as a fact
    source_url: str


def enabled() -> bool:
    return get_settings().discovery_eaeu


def is_goods(codes: Sequence[str]) -> bool:
    """Every code of the query is a good (OKPD2 01–33); no code at all — not established, the channel stays out."""
    heads = [c.strip()[:2] for c in codes if c.strip()]
    return bool(heads) and all(h.isascii() and h.isdigit() and int(h) in GOODS_CLASSES for h in heads)


@lru_cache(maxsize=4)
def _parse(path: str, mtime_ns: int) -> tuple[Lead, ...]:
    out: list[Lead] = []
    with Path(path).open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError(f"{Path(path).name}: колонки {reader.fieldnames}, ожидались {list(COLUMNS)}")
        for n, row in enumerate(reader, start=2):
            country, ident = (row["country"] or "").strip(), (row["national_id"] or "").strip()
            url = (row["source_url"] or "").strip()
            if country not in EAEU_COUNTRIES or not url.startswith(("http://", "https://")):
                log.warning("%s:%d: страна не из ЕАЭС или нет ссылки — строка пропущена", Path(path).name, n)
                continue
            if country == "BY" and not by_grp.unp_is_valid(ident):
                log.warning("%s:%d: УНП %s не проходит контрольную цифру — строка пропущена", Path(path).name, n, ident)
                continue
            if country == "AM" and not am_register.tin_is_valid(ident):
                log.warning("%s:%d: номер Армении %s — не 8 цифр, строка пропущена", Path(path).name, n, ident)
                continue
            out.append(Lead((row["okpd2_class"] or "").strip(), country, ident, (row["name"] or "").strip(), url))
    return tuple(out)


def load(path: Path | None = None) -> tuple[Lead, ...]:
    path = path or LEADS  # read at the call: tests point it elsewhere
    return _parse(str(path), path.stat().st_mtime_ns) if path.is_file() else ()


def _components(lead: Lead, status: str | None, predicted: bool, weights: dict[str, float]) -> list[ScoreComponent]:
    country = COUNTRY_NAME[lead.country]
    cls = (f"ОКПД2 {lead.okpd2_class} (в запросе ОКПД2 не указан — класс определён автоматически)" if predicted
           else f"ОКПД2 запроса {lead.okpd2_class}")
    parts: dict[str, tuple[float, float, str]] = {
        "product_fit": (0.0, weights.get("product_fit", 0.0),
                        f"Кандидат по товарам класса {cls}; идентификатор взят со страницы {lead.source_url}. "
                        f"{REGISTRY_NAME[lead.country]} не содержит видов деятельности — соответствие товару реестром "
                        "не подтверждено, в оценку не входит"),
        "experience": (0.0, weights.get("experience", 0.0),
                       "В данных организаторов за 2024–2025 не встречается ни в одной закупке"),
        "reliability": (0.0, 0.0, f"Статус по реестру страны ({country}): {status or 'не указан'} — "
                                  "в score не входит"),
        "activity": (0.0, weights.get("activity", 0.0), "Участий в закупках в данных организаторов нет"),
        "geography": (0.0, weights.get("geography", 0.0),
                      f"{country} — страна ЕАЭС: товары допускаются наравне с российскими (национальный режим); "
                      "близость к заказчику не оценивается"),
        "price": (0.0, 0.0, "Нет данных: в открытых источниках нет цен — в score не входит"),
    }
    return [ScoreComponent(name=n, value=parts[n][0], weight=float(parts[n][1]), explanation=parts[n][2])  # type: ignore[arg-type]
            for n in COMPONENT_ORDER]


def _lookup(lead: Lead, cache: SourceCache) -> tuple[Counterparty | None, bool]:
    """The lead's company by its national registry, cache only: (company or None — the registry does not know the
    identifier, is it active). KeyError — no cached answer, or no registry is connected for the country."""
    if lead.country == "BY":
        page = by_grp.fetch(lead.national_id, cache, offline=True)
        cp = by_grp.counterparty(lead.national_id, page)
        return cp, cp is not None and by_grp.is_active(cp, page)
    if lead.country == "AM":
        pages = am_register.fetch(lead.national_id, cache, offline=True)
        return am_register.counterparty(lead.national_id, pages), am_register.is_active(lead.national_id, pages)
    raise KeyError(lead.country)


def matches(codes: Sequence[str], classes: Sequence[str], cache: SourceCache, weights: dict[str, float],
            predicted: bool, *, leads: Sequence[Lead] | None = None, recorded: bool = True,
            ) -> tuple[list[CounterpartyMatch], str | None]:
    """EAEU companies of the query's classes confirmed by their national registry (cache only, never the network).
    Returns the rows (at most MAX_SHOWN) and one line of what happened; nothing for a query that is not goods.
    `recorded=False` — the recorded answers of configs/eaeu are not put into the cache (tests)."""
    if not is_goods(codes if codes else classes):
        return [], None
    mine = [lead for lead in (load() if leads is None else leads) if lead.okpd2_class in classes]
    if not mine:
        return [], None
    if recorded:
        if RECORDED.is_dir():
            by_grp.seed(RECORDED, cache)
        if RECORDED_AM.is_dir():
            am_register.seed(RECORDED_AM, cache)
    out: list[CounterpartyMatch] = []
    seen: set[str] = set()
    no_card = not_found = inactive = 0
    for lead in mine:
        try:
            cp, active = _lookup(lead, cache)
        except KeyError:
            no_card += 1
            continue
        if cp is None:
            not_found += 1
            continue
        if not active:
            inactive += 1
            continue
        cp = strip_unverified(cp, cache.page_text)
        if cp.foreign_id is None:  # the identifier did not pass the Evidence check: not shown at all
            not_found += 1
            continue
        if cp.foreign_id in seen:  # one row per company, whatever the number of its lead rows
            continue
        seen.add(cp.foreign_id)
        comps = _components(lead, cp.status, predicted, weights)
        out.append(CounterpartyMatch(counterparty=cp, origin="discovered", components=comps,
                                     score=round(sum(c.value * c.weight for c in comps), 4)))
    line = (f"ЕАЭС: лидов по классу {len(mine)}, без ответа реестра в кеше {no_card}, реестр не знает идентификатор "
            f"{not_found}, не действует {inactive}, в блок {min(len(out), MAX_SHOWN)}")
    return out[:MAX_SHOWN], line
