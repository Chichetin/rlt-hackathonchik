"""discover(query) -> companies from open sources that are NOT in organizers' data. Owner: SOURCES.

Block «Новые из открытых источников» (`origin="discovered"`, #16, D-036, D-037):
1. OKPD2 class of the query (first 4 digits): `Query.okpd2`, or — when empty — the code search()
   itself predicted from the text (`predicted_code`: search.predicted_code(), P-02, passed by
   src/match/pipeline.py — so both blocks speak about the same class, #394); without it — the class
   search() wrote into its `product_fit` explanations, and if it wrote none — the class voted by the
   lots of the top history matches (`data:items#<lot>` Evidence of search()).
2. OKPD2 class → OKVED prefixes: the same class (OKPD2 and OKVED2 share the first four digits by
   design) and, for goods, a wholesale code from a hand-made table (WHOLESALE). There is no official
   OKPD2 → OKVED2 key (research/SOURCES.md): the mapping is our rule and is shown to the user.
3. One cached rmsp.nalog.ru request per (OKVED, region): companies of СПб / ЛО with this main OKVED.
   At most MAX_REQUESTS per query. Live requests end before the deadline (LIVE_BUDGET_S from the
   start: pacing, connect and read included); after the first network failure the rest of the query
   reads only the cache, and a timeout pauses the source for every search (rmsp.DOWN_COOLDOWN_S).
4. Dropped: excluded from the registry, already present in organizers' data (suppliers*.parquet),
   liquidated by the ФНС card. Organizers' data absent → `history_checked=False`: the caller must
   not call these companies «новые».
5. ГИСП (#74, src/sources/gisp.py, offline export of the registry of Russian industrial products): a company whose
   product's OKPD2 agrees with the query's code (gisp.agrees) gets product_fit 1.0 «производит этот товар по
   реестру Минпромторга» and the role «производитель» with the registry as its basis — a company of the МСП channel
   or a web lead is confirmed, a registry company outside both is added (INN type DISCOVERY_NP_TYPES, region by
   the cached ФНС card's KPP, else by the INN's tax office code — said to be unconfirmed; the card is queued).
   Among equal scores a confirmed product comes first. Any registry product of a shown company makes it a
   manufacturer by the registry. No export → nothing changes.
6. Every shown fact is an Evidence whose value literally occurs in the cached answer. The ФНС card
   (status, KPP, address) is added only from the cache; an INN without a cached card goes to the
   enrichment queue (`enqueue`), so the request itself makes no pb.nalog.ru calls.

`DISCOVERY_OFFLINE=1` or `ENRICH_OFFLINE=1` — cache only, no network.
A query WITHOUT OKPD2 gets the block by the class search() predicts from the text (#394, D-141 revises D-041:
P-02 predicts the class right in 0.820 of positions, D-110). The class is never called «ОКПД2 запроса»:
the explanation and the UI say it was determined automatically. `DISCOVERY_PREDICTED_CLASS=0` switches
it off (also `false` / `off`, src/settings.py): such a query gets an empty block and no network request.
CLI (live check, cache warm-up): `python -m src.enrich.discover "бумага офисная" --okpd2 17.12.14`;
without `--okpd2` the class is the one search() predicts on organizers' data (as in /api/search).
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb
import httpx
import yaml

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query, ScoreComponent
from src.enrich import eaeu, web_discover
from src.enrich.enrich import enrich, from_bo, from_card
from src.enrich.evidence import cached_pages, check_counterparty, strip_unverified, value_in_page
from src.match import new_fit
from src.match import search as search_mod
from src.match.filters import apply_filters, apply_legal
from src.match.index import processed_dir
from src.match.purchase_type import purchase_type
from src.match.store import Store, get_store, store_for
from src.regions import NEIGHBOUR, NEIGHBOUR_VALUE, REGION_CODE, REGION_NAME
from src.settings import get_settings
from src.sources import bo_nalog, gisp, msp_dump, pb_nalog, rmsp, role, web_leads, yandex_search
from src.sources.cache import CachedPage, SourceCache
from src.sources.rmsp import MSP_CATEGORY

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]

MAX_SHOWN = 10  # producers / distributors mixed at the top of the block
MAX_REQUESTS = 6  # rmsp requests per query (cached or live): class + up to 2 wholesale codes × 2 regions
MAX_CLASSES = 2  # OKPD2 classes of one query that get their own requests
LIVE_BUDGET_S = 4.0  # all live rmsp requests of one /api/search end within this wall time
VOTE_MATCHES = 10  # top history matches whose lots vote for the OKPD2 class
FIT = {"class": 1.0, "wholesale": 0.6}  # product_fit of a discovered company (D-036)
RETAIL_OKVED, RETAIL_FIT = "47", 0.4  # the live button only (#583): a retailer of goods found by the product
COMPONENT_ORDER = ("product_fit", "experience", "reliability", "activity", "geography", "price")

# OKPD2 prefix (longest wins) → OKVED2 wholesale codes for these goods, the main one first. A hand-made
# table, not an official key (D-036; second codes and new rows — #108). The name of the code shown to the
# user always comes from the registry row. Every code here is «46.x except 46.1» — a distributor by
# src/sources/role.py.
WHOLESALE: dict[str, tuple[str, ...]] = {
    "10": ("46.3",), "11": ("46.34",),
    "13": ("46.41",), "14": ("46.42",), "15": ("46.42",),
    "16": ("46.73",), "23": ("46.73",),
    "17": ("46.49.33",), "17.12": ("46.49.33", "46.76.1"),  # office paper: stationery and paper/board wholesale
    "19.20": ("46.71",),
    "20": ("46.75",),  # chemical products (disinfectants 20.20 and the rest of class 20 without a row)
    "20.30": ("46.73.4",), "20.41": ("46.44.2",), "20.42": ("46.45",),
    "21": ("46.46.1",),
    "22.19.60": ("46.46.2",), "32.50": ("46.46.2",), "26.60": ("46.46.2",),
    "25.7": ("46.74",),
    "26.1": ("46.52",), "26.20": ("46.51", "46.66"), "26.30": ("46.52",), "26.40": ("46.43",),
    "27.40": ("46.47.3",), "27.51": ("46.43.1",),
    "28.23": ("46.66",), "28": ("46.69",),
    "31.01": ("46.65",), "31": ("46.47.1",),
    "32.30": ("46.49.43",), "32.40": ("46.49.42",),
}
_LEGAL_FORMS = (
    ("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ", "ООО"),
    ("НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "НАО"),
    ("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ПАО"),
    ("ЗАКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ЗАО"),
    ("ОТКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ОАО"),
    ("АКЦИОНЕРНОЕ ОБЩЕСТВО", "АО"),
)
_OKPD_CLASS = re.compile(r"\d{2}\.\d{2}", re.ASCII)
_LOT_URL = "data:items#"
_PREDICTED_MARK = "ОКПД2 определён по похожим позициям"  # wording of src/match/search.py::_product_fit

Relation = Literal["class", "wholesale"]


@dataclass(frozen=True)
class Channel:
    """Why an OKVED prefix is searched: which OKPD2 class of the query it answers and how."""

    okved: str
    okpd_class: str
    relation: Relation


@dataclass
class Discovery:
    matches: list[CounterpartyMatch] = field(default_factory=list)
    okpd_classes: list[str] = field(default_factory=list)
    predicted: bool = False  # the class comes from history lots, not from Query.okpd2
    history_checked: bool = False  # False → organizers' data absent: «новые» is NOT established
    known: int = 0  # found in the registry but already present in organizers' data
    inactive: int = 0  # found but excluded from the registry
    pool: list[CounterpartyMatch] = field(default_factory=list)  # every candidate after filters, before top-10
    # after the legal status, before the user filters and top-10: X-Facets of the block in /api/search (#232)
    candidates: list[CounterpartyMatch] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)  # one line per (OKVED, region): what happened
    leads: list[str] = field(default_factory=list)  # web leads (#108): what happened to them, one line
    # INN → why it matched, a key of STEP: class / wholesale / extra, registry / registry_kind (ГИСП, #74)
    relation: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # product_fit_new (#77, src/match/new_fit.py): off — not computed; shadow — computed here only;
    # on — it IS product_fit of every match (experiment only, confirmation №3 to make it the default)
    fit_mode: str = "off"
    fit_new: dict[str, new_fit.NewFit] = field(default_factory=dict)  # INN → product_fit_new
    profile: new_fit.Profile | None = None  # signal 8: words of the full OKPD2 code of the query
    profile_gap: str | None = None  # why `profile` is None in shadow / on: «not computed», not «0»
    source_failed: bool = False  # a registry request was refused or failed: the block may be incomplete (#287)
    gisp: list[str] = field(default_factory=list)  # ГИСП (#74): what the registry of industrial products gave
    gisp_backed: set[str] = field(default_factory=set)  # INNs whose product the registry confirms (product_fit 1.0)
    eaeu: list[str] = field(default_factory=list)  # EAEU companies (#200): what happened to the leads, one line


def predicted_class_allowed() -> bool:
    return get_settings().discovery_predicted_class


def offline_mode() -> bool:
    s = get_settings()
    return s.discovery_offline or s.enrich_offline


def okpd_classes(codes: Sequence[str]) -> list[str]:
    """Distinct OKPD2 classes (`17.12`) of the codes, in order; shorter codes are too broad — skipped."""
    out: list[str] = []
    for code in codes:
        m = _OKPD_CLASS.match(code.strip())
        if m and m.group(0) not in out:
            out.append(m.group(0))
    return out


def wholesale_okveds(okpd2: str) -> tuple[str, ...]:
    """Wholesale OKVED codes of the longest matching OKPD2 prefix; () when there is no row."""
    best = max((p for p in WHOLESALE if okpd2.startswith(p)), key=len, default=None)
    return WHOLESALE[best] if best else ()


def wholesale_okved(okpd2: str) -> str | None:
    """The main wholesale code (the first of the row)."""
    return next(iter(wholesale_okveds(okpd2)), None)


def okved_plan(codes: Sequence[str]) -> list[Channel]:
    """OKVED prefixes to search for the query's OKPD2 codes: the class itself, then wholesale."""
    plan: list[Channel] = []
    for cls in okpd_classes(codes)[:MAX_CLASSES]:
        plan.append(Channel(cls, cls, "class"))
        # the deepest code of this class decides the wholesale row (22.19.60 vs 22.19)
        deepest = max((c.strip() for c in codes if c.strip().startswith(cls)), key=len)
        for trade in wholesale_okveds(deepest):
            if all(ch.okved != trade for ch in plan):
                plan.append(Channel(trade, cls, "wholesale"))
    return plan


def stated_class(history: Sequence[CounterpartyMatch]) -> str | None:
    """The OKPD2 class search() predicted for the query, read from its product_fit explanations.

    Fallback only: search() exposes its predicted code as data (search.predicted_code(), #394) and the
    pipeline passes it to discover(); this parse of the explanation serves callers that have only the
    matches (a reworded sentence switches to the lot vote — test_class_is_predicted_from_history_lots
    pins the wording). The prediction can be wrong, so the class is always shown as «определён
    автоматически», never as the class of the query.
    """
    votes: dict[str, int] = {}
    for m in history[:VOTE_MATCHES]:
        for comp in m.components:
            _, mark, tail = comp.explanation.partition(_PREDICTED_MARK)
            found = _OKPD_CLASS.search(tail) if comp.name == "product_fit" and mark else None
            if found:
                votes[found.group(0)] = votes.get(found.group(0), 0) + 1
    return max(votes, key=lambda c: votes[c]) if votes else None  # ties: the higher-ranked match wins


def _store_errors() -> tuple[type[Exception], ...]:
    """What a failed read of organizers' data raises on either store: the same «not checked» note on both."""
    try:
        import psycopg
    except ImportError:  # no pg extra: parquet only
        return (OSError, duckdb.Error)
    return (OSError, duckdb.Error, psycopg.Error)


_STORE_ERRORS = _store_errors()


def predict_class(history: Sequence[CounterpartyMatch], store: Store | None = None) -> str | None:
    """OKPD2 class of a query without codes: stated by search(), else voted by the lots behind the
    top history matches (their positions from `store`, default — the store search() read); None when
    neither can be read."""
    return stated_class(history) or _lots_class(history, store)


def _lots_class(history: Sequence[CounterpartyMatch], store: Store | None = None) -> str | None:
    lots: dict[int, float] = {}
    for m in history[:VOTE_MATCHES]:
        for e in m.counterparty.evidence:
            if e.url.startswith(_LOT_URL) and e.url[len(_LOT_URL):].isdigit():
                lot = int(e.url[len(_LOT_URL):])
                lots[lot] = max(lots.get(lot, 0.0), m.score)
    if not lots:
        return None
    try:
        rows = (store or get_store()).lot_codes(sorted(lots))
    except _STORE_ERRORS as e:  # StoreUnavailable is an OSError: no data, Postgres down
        log.warning("discovery: позиции лотов не прочитаны: %s", e)
        return None
    by_lot: dict[int, set[str]] = {}
    for lot, code in rows:
        cls = code[:5]  # okpd2_l4 of the ingest: the first 5 characters of a code at least that long
        if len(code) >= 5 and _OKPD_CLASS.fullmatch(cls):
            by_lot.setdefault(lot, set()).add(cls)
    votes: dict[str, float] = {}
    for lot, classes in sorted(by_lot.items()):
        for cls in sorted(classes):  # a lot with several classes splits its vote
            votes[cls] = votes.get(cls, 0.0) + lots[lot] / len(classes)
    return min(votes, key=lambda c: (-votes[c], c)) if votes else None


def known_inns(inns: Sequence[str], as_of: date | None, store: Store | None = None) -> set[str] | None:
    """INNs present in organizers' data (before as_of) — the suppliers table of `store` (default: the store
    search() reads, Postgres in `make up`); None — the data is absent, nothing checked."""
    if as_of is not None:  # a lot after the data (#589, defense export): everyone in the data is before it
        from src.match.index import get_index

        try:
            end = get_index(store, None).data_end
        except _STORE_ERRORS:
            end = None
        if end is not None and as_of > end:
            as_of = None
    try:
        everyone = (store or get_store()).supplier_inns(as_of)
    except _STORE_ERRORS as e:  # StoreUnavailable (no table for as_of, Postgres down) is an OSError
        log.warning("discovery: ИНН данных организаторов не прочитаны: %s", e)
        return None
    return set(inns) & everyone


def display_name(row: dict[str, str]) -> str | None:
    name = row.get("name_ex")
    if not name:
        return None
    if row["nptype"] == "IP":
        return f"ИП {name}"
    for full, short in _LEGAL_FORMS:
        if name.upper().startswith(full):
            return f"{short}{name[len(full):]}"
        if name.upper().endswith(full):  # «"ЭЛАСТИС" ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ»
            return f"{short} {name[:-len(full)].strip()}"
    return name


def from_row(row: dict[str, str], page: CachedPage) -> Counterparty:
    """Registry row → Counterparty: only what the row says, every fact with an Evidence."""
    inn = row["inn"]
    at = datetime.fromtimestamp(page.fetched_at, tz=UTC)
    ev: list[Evidence] = []

    def add(fld: str, value: str | None, fact: str) -> bool:
        if not value:
            return False
        ev.append(Evidence(source=rmsp.SOURCE, url=page.url, fetched_at=at, fact=fact, field=fld, value=value))
        return True

    is_ul = row["nptype"] == "UL"
    add("inn", inn, f"ИНН {inn} — запись в едином реестре субъектов МСП")
    ogrn = row.get("ogrn")
    add("ogrn", ogrn, f"{'ОГРН' if is_ul else 'ОГРНИП'} {ogrn}")
    full = row.get("name_ex")
    add("name", full, f"Наименование: {full}" if is_ul else f"Индивидуальный предприниматель {full}")
    okved, okved_name = row.get("okved1"), row.get("okved1name")
    if okved and okved_name:
        add("okved", okved_name, f"Основной ОКВЭД {okved} — {okved_name}")
    elif okved:  # no name in the row: the code itself is the fact, tied to its own registry line
        add("okved", f"{inn}.okved1: {okved}", f"Основной ОКВЭД {okved}")
    kind = role.assign(okved, okved_name, f"row.{inn}.okved1: {okved}", add)

    category = row.get("category", "")
    is_msp: bool | None = None
    if row.get("is_active") == "1" and category in MSP_CATEGORY:
        is_msp = True
        since = row.get("dtregistry", "").split(" ")[0]
        add("msp", f"{inn}.category: {category}",
            f"Реестр МСП: {MSP_CATEGORY[category]}" + (f", включено {since}" if since else ""))

    code = row.get("regioncode", "")
    region = REGION_NAME.get(code)
    if region:
        add("region", f"{inn}.regioncode: {code}", f"Регион по реестру МСП: {region} (код {code})")

    contacts: dict[str, str] = {}
    if is_ul:  # corporate contacts of a legal entity from the registry; ИП contacts are never collected
        for fld, label in (("phone", "Телефон"), ("email", "Email")):
            value = row.get(fld)
            # a value the Evidence check would not recognise as a phone / email is not a contact
            if value and value_in_page(fld, value, page.body) and add(fld, value, f"{label} из реестра МСП: {value}"):
                contacts[fld] = value

    return Counterparty(
        inn=inn,
        name=display_name(row) or f"ИНН {inn}",
        kind=kind,
        region=region,
        is_msp=is_msp,
        okved=[okved] if okved else [],
        contacts=contacts,
        evidence=ev,
    )


def with_card(cp: Counterparty, cache: SourceCache) -> tuple[Counterparty, bool]:
    """Add the cached ФНС card (status, KPP, address); (cp, False) when the card is not cached."""
    if cp.inn is None:
        return cp, False
    page = cache.get(pb_nalog.card_url(cp.inn), allow_stale=True)
    if page is None:
        return cp, False
    card = from_card(cp.inn, page)
    have = {e.field for e in cp.evidence}
    return cp.model_copy(update={
        "status": card.status,
        "kpp": card.kpp,
        "name": cp.name if card.name.startswith("ИНН ") else card.name,
        "evidence": [*cp.evidence, *(e for e in card.evidence if e.field not in have)],
    }), True


def _components(cp: Counterparty, row: dict[str, str], ch: Channel, predicted: bool, has_card: bool,
                region: str, weights: dict[str, float], region_note: str | None = None,
                fit_new: new_fit.NewFit | None = None) -> list[ScoreComponent]:
    okved, okved_name = row["okved1"], row.get("okved1name", "")
    head = f"Основной ОКВЭД {okved}" + (f" «{okved_name}»" if okved_name else "")
    # A predicted class is never presented as the class of the query (#63: the prediction can be wrong).
    cls = (f"ОКПД2 {ch.okpd_class} (в запросе ОКПД2 не указан — класс определён автоматически по похожим "
           "закупкам в данных АИС ГЗ)") if predicted else f"ОКПД2 запроса {ch.okpd_class}"
    if ch.relation == "class":  # the same rule as the role of the card (src/sources/role.py, D-057)
        what = ("вероятно производитель (по ОКВЭД)" if cp.kind == "manufacturer"
                else "основной вид деятельности соответствует предмету закупки")
        fit_why = f"{head} совпадает с классом {cls} — {what}; совпадение по классу ОКВЭД (4 знака), не по товару"
    else:
        fit_why = (f"{head} — оптовая торговля товарами класса {cls}, "
                   "по таблице соответствия ОКПД2 → ОКВЭД — вероятно дистрибьютор")

    if has_card and cp.status:
        rel_why = f"Статус по реестру ФНС: {cp.status} — в score не входит"
    else:
        rel_why = "Состоит в реестре МСП; статус в ЕГРЮЛ ещё не проверен (обогащение в очереди) — в score не входит"

    want, code = REGION_CODE.get(region), row.get("regioncode", "")
    where = REGION_NAME.get(code, f"регион с кодом {code}") if code else "регион не установлен"
    if want is not None and code == want:
        geo, geo_why = 1.0, f"Реестр МСП: {where}"
    elif want is not None and NEIGHBOUR.get(want) == code:
        geo, geo_why = NEIGHBOUR_VALUE, f"Реестр МСП: соседний регион — {where}"
    else:
        geo, geo_why = 0.0, f"Реестр МСП: {where}; регион запроса — {region}"
    if region_note:  # the comparison is with the customer's region, say so
        geo_why = f"{geo_why.replace('регион запроса', 'регион заказчика')}; {region_note}"

    fit = FIT[ch.relation]
    if fit_new is not None:  # mode «on» only: the v0 sentence stays, product_fit_new explains the value
        fit, fit_why = fit_new.value, f"{fit_why}. {fit_new.explanation()}"
    parts: dict[str, tuple[float, float, str]] = {
        "product_fit": (fit, weights.get("product_fit", 0.0), fit_why),
        "experience": (0.0, weights.get("experience", 0.0),
                       "В данных организаторов за 2024–2025 не встречается ни в одной закупке"),
        "reliability": (0.0, 0.0, rel_why),
        "activity": (0.0, weights.get("activity", 0.0), "Участий в закупках в данных организаторов нет"),
        "geography": (geo, weights.get("geography", 0.0), geo_why),
        "price": (0.0, 0.0, "Нет данных: в открытых источниках нет цен — в score не входит"),
    }
    return [ScoreComponent(name=n, value=parts[n][0], weight=float(parts[n][1]), explanation=parts[n][2])
            for n in COMPONENT_ORDER]


def _order(backed: set[str]) -> Callable[[CounterpartyMatch], tuple[float, bool, str]]:
    """Score order; among equal scores a product confirmed by the registry (ГИСП) first, then by INN."""
    return lambda m: (-m.score, (m.counterparty.inn or "") not in backed, m.counterparty.inn or "~")


def _mix(ranked: list[CounterpartyMatch], relation: dict[str, Relation],
         backed: set[str] | None = None, min_score: float | None = None,
         max_shown: int = MAX_SHOWN) -> list[CounterpartyMatch]:
    """Top MAX_SHOWN with both kinds present: producers and distributors take slots in turn (best
    first within a kind), so ten producers do not hide every distributor; shown in score order.
    `min_score` (#549) — after them every other candidate scoring at least it, best first, up to `max_shown`
    in all; None — the top MAX_SHOWN only."""
    queues = [[m for m in ranked if relation[m.counterparty.inn or ""] == r] for r in ("class", "wholesale")]
    picked: list[CounterpartyMatch] = []
    while len(picked) < MAX_SHOWN and any(queues):
        for q in queues:
            if q and len(picked) < MAX_SHOWN:
                picked.append(q.pop(0))
    order = _order(backed or set())
    head = sorted(picked, key=order)[:max_shown]
    if min_score is None:
        return head
    taken = {id(m) for m in head}
    rest = sorted((m for m in ranked if id(m) not in taken and m.score >= min_score), key=order)
    return [*head, *rest][:max_shown]


def block_config(path: Path = new_fit.CONFIG) -> dict[str, Any]:
    """`block` of configs/discover.yaml (#549): the size of «Новые» in /api/search and the limits of its live
    registry requests."""
    return dict(yaml.safe_load(path.read_text(encoding="utf-8"))["block"])


def leads_enabled() -> bool:
    return get_settings().discovery_leads


def lead_matches(
    query: Query, codes: Sequence[str], classes: Sequence[str], skip: set[str], cache: SourceCache,
    weights: dict[str, float], predicted: bool, store: Store, region: str, region_note: str | None,
    leads: dict[str, web_leads.Lead] | None = None, *, live: bool = False,
) -> tuple[list[CounterpartyMatch], dict[str, Relation], list[str], str | None]:
    """Web leads of the query's classes (#108, src/sources/web_leads.py): companies outside the МСП registry.

    A lead is shown only when (1) it is absent from organizers' data before `as_of`, (2) its ФНС card (or the
    fallback registry answer) is in the cache — every shown fact comes from there with its Evidence, nothing
    from the search result — (3) the main OKVED of that card is the class or its wholesale code and (4) the
    region by KPP (or the registry) is the query's region or its neighbour, and the INN type is one of
    DISCOVERY_NP_TYPES — the same rules as the registry channel, so product_fit keeps its steps (D-036).
    Returns the matches, their relation, the new INNs without a cached card (for the queue) and one line
    for `Discovery.leads`. `leads` — INN → lead given by the caller (the Yandex fallback, src/enrich/web_discover.py);
    None — the lead files of the query's classes. `live` (#583, the button «Искать в реальном времени»): an
    individual entrepreneur is taken whatever DISCOVERY_NP_TYPES says, and for goods any region (the geography stays a
    component of the score, not a filter — the organizers' expert: for goods the region does not matter).
    """
    leads = dict(web_leads.for_classes(list(classes), web_leads.load()) if leads is None else leads)
    for inn in skip:  # already found in the МСП registry: one row per company
        leads.pop(inn, None)
    if not leads:
        return [], {}, [], None
    known = known_inns(list(leads), query.as_of, store) or set()
    plan = okved_plan(codes)
    regions = _regions(query)
    np_types = ["UL", "IP"] if live else _np_types()
    any_region = live and purchase_type(codes) == "goods"
    out: list[CounterpartyMatch] = []
    relation: dict[str, Relation] = {}
    no_card: list[str] = []
    other_okved = other_region = no_region = other_type = 0
    for inn, lead in leads.items():
        if inn in known:
            continue
        if ("UL" if len(inn) == 10 else "IP") not in np_types:
            other_type += 1
            continue
        try:
            cp = enrich(inn, cache, offline=True, open_data=False)  # pipeline.verified() adds the ФНС facts
        except KeyError:  # nothing cached for this INN
            no_card.append(inn)
            continue
        code = cp.okved[0] if cp.okved else ""
        ch = next((c for c in plan if c.okpd_class == lead.okpd2_class and code.startswith(c.okved)), None)
        retail = ch is None and live and any_region and code.startswith(RETAIL_OKVED)
        if retail:  # #583: the button takes a retailer of goods (47.x) found by the product, with a lower fit
            ch = Channel(okved=RETAIL_OKVED, okpd_class=lead.okpd2_class, relation="wholesale")
        if ch is None:  # the registry does not confirm the category the search found it for
            other_okved += 1
            continue
        regioncode = cp.kpp[:2] if cp.kpp else REGION_CODE.get(cp.region or "", "")
        if any_region:  # #583: goods from anywhere; an unknown region scores 0 in «География»
            regioncode = "" if regioncode == "99" else regioncode
        elif not regioncode or regioncode == "99":  # ГИР БО without KPP; KPP 99 of the largest taxpayers
            no_region += 1
            continue
        elif regioncode not in regions:
            other_region += 1
            continue
        # the OKVED name only from a fact «Основной ОКВЭД <code> — <name>» (ГИР БО gives the code alone)
        okved_name = next((e.value for e in cp.evidence if e.field == "okved" and e.value
                           and e.fact.startswith(f"Основной ОКВЭД {code} — ")), "")
        row = {"okved1": code, "okved1name": okved_name, "regioncode": regioncode}
        comps = []
        for c in _components(cp, row, ch, predicted, True, region, weights, region_note):
            why = c.explanation
            if c.name == "product_fit" and retail:
                c = c.model_copy(update={"value": RETAIL_FIT})
                why = (f"Найдена веб-поиском по товару; вид деятельности — по реестру ФНС: основной ОКВЭД {code} — "
                       "розничная торговля, производство и опт по реестру не подтверждены")
            elif c.name == "product_fit":
                why = f"Найдена веб-поиском по товару; вид деятельности — по реестру ФНС: {why}"
            elif c.name == "geography":  # the region comes from the card, not from a registry row
                why = why.replace("Реестр МСП", "Карточка ФНС (КПП)") if cp.kpp else why.replace("Реестр МСП: ", "")
            comps.append(c.model_copy(update={"explanation": why}))
        relation[inn] = ch.relation
        out.append(CounterpartyMatch(counterparty=cp, origin="discovered", components=comps,
                                     score=round(sum(c.value * c.weight for c in comps), 4)))
    line = (f"веб-лиды: по классу {len(leads)}, уже в данных {len(known)}, без карточки ФНС в кеше {len(no_card)}, "
            f"тип ИНН не из DISCOVERY_NP_TYPES {other_type}, ОКВЭД не подтвердил класс {other_okved}, "
            f"другой регион {other_region}, регион не установлен {no_region}, в блок {len(out)}")
    return out, relation, no_card, line


def yandex_matches(
    query: Query, codes: Sequence[str], classes: Sequence[str], skip: set[str], history: Sequence[CounterpartyMatch],
    current: Sequence[CounterpartyMatch], cache: SourceCache, weights: dict[str, float], predicted: bool,
    store: Store, region: str, region_note: str | None, *, offline: bool, deadline: float | None,
    force: bool = False, fetch_card: Callable[[str], object] | None = None, client: httpx.Client | None = None,
    site_client: httpx.Client | None = None,
) -> tuple[list[CounterpartyMatch], dict[str, Relation], list[str], str | None]:
    """The paid fallback (#194, src/enrich/web_discover.py): Yandex → company site → INN → the road of a web lead.

    Asked only when the free sources — every history candidate and every «Новые» found so far, after the legal
    filters — hold fewer strong candidates than `min_strong` (#558: 5, topped up to it). The lot of the per-lot
    ceiling is `query.query_id`. `deadline` —
    the one of the whole discover(): after it Yandex and the sites are read from their caches only. Leads answer the
    first class of the query. A company already in `skip` (found by the registry or a lead file) is not repeated.
    Returns what `lead_matches` returns. `force` (#550, the button «Искать в реальном времени») — asked whatever the
    strong count is, the explanation says «найдена в реальном времени»; `fetch_card(inn)` — gets the ФНС card of a
    found INN that has none in the cache before the lead road, which shows only a company with a cached card.
    """
    strong = 0 if force else web_discover.strong_count([*apply_legal(list(history)), *apply_legal(list(current))])
    fb = web_discover.lot_leads(query.query_id, query.text, classes[0], strong, okpd2=codes, cache=cache,
                                offline=offline, deadline=deadline, force=force, client=client,
                                site_client=site_client,
                                known={*skip, *(m.counterparty.inn or "" for m in history)})
    head = f"Яндекс ({'в реальном времени' if force else 'запасной путь'}): {fb.reason}"
    if not fb.used:
        return [], {}, [], head
    leads = {lead.inn: lead for lead in fb.leads if lead.inn and lead.inn not in skip}
    if fetch_card is not None:  # only a company the lead road could show: not in organizers' data, an allowed type
        known = known_inns(list(leads), query.as_of, store) or set()
        np_types = ["UL", "IP"] if force else _np_types()  # the button takes an individual entrepreneur too (#583)
        for inn in leads:
            if inn in known or ("UL" if len(inn) == 10 else "IP") not in np_types:
                continue
            if cache.get(pb_nalog.card_url(inn)) is None:
                try:
                    fetch_card(inn)
                except Exception:  # noqa: BLE001 — a card that fails stays «без карточки», the rest go on
                    log.exception("живой поиск: карточка ФНС %s не получена", inn)
    found, relation, no_card, line = lead_matches(query, codes, classes, skip, cache, weights, predicted, store,
                                                  region, region_note, leads=leads, live=force)
    out: list[CounterpartyMatch] = []
    for m in found:
        s = fb.check(m.counterparty.inn or "")
        if s is None:
            continue
        found_as = "Найдена в реальном времени в Яндекс Поиске" if force else "Найдена в Яндекс Поиске"
        comps = [c.model_copy(update={"explanation": c.explanation.replace(
            "Найдена веб-поиском по товару", f"{found_as} по запросу «{s.query}», ИНН указан на сайте "
            f"{s.domain}")}) if c.name == "product_fit" else c for c in m.components]
        out.append(m.model_copy(update={"counterparty": web_discover.with_site(m.counterparty, s, cache),
                                        "components": comps}))
    return out, relation, no_card, head + (f"; {line.removeprefix('веб-лиды: ')}" if line else "")


class LiveOff(Exception):
    """The live search cannot run: the Yandex client is off (no key or no ceilings) or the query has no class."""


@dataclass
class Live:
    matches: list[CounterpartyMatch]
    note: str  # what happened: the requests, the sites, the funnel of the lead road


def live_search(query: Query, *, cache: SourceCache | None = None, processed: Path | None = None,
                weights: dict[str, float] | None = None, client: httpx.Client | None = None,
                site_client: httpx.Client | None = None, offline: bool | None = None) -> Live:
    """The button «Искать в реальном времени» (#550): Yandex → company site → INN → the road of a web lead for this
    purchase now, without the trigger «< min_strong strong» and without a deadline; a found INN without a cached
    ФНС card gets it live. The class — the first OKPD2 class of the query (the UI sends the class «Новые» searched
    by). Only the page of the site that states the INN and the ФНС card are facts; the result snippet never is.
    LiveOff — the client is off or the query has no OKPD2 class."""
    why = yandex_search.disabled_reason()
    if why is not None:
        raise LiveOff(f"живой поиск не настроен: {why}")
    classes = okpd_classes(query.okpd2)
    if not classes:
        raise LiveOff("живой поиск по закупке без кода ОКПД2 невозможен: класс не определён")
    cache = cache or SourceCache()
    offline = offline_mode() if offline is None else offline
    weights = weights if weights is not None else search_mod.load_config()["weights"]
    store = store_for(processed)
    target, target_note = search_mod.customer_region(query)
    codes = [c.strip() for c in query.okpd2]

    def card(inn: str) -> object:
        return enrich(inn, cache, offline=offline, open_data=False)

    found, _, no_card, line = yandex_matches(
        query, codes, classes[:MAX_CLASSES], set(), (), (), cache, weights, False, store, target, target_note,
        offline=offline, deadline=None, force=True, fetch_card=card, client=client, site_client=site_client)
    note = line or ""
    if no_card:
        note += f"; без карточки ФНС не показаны {len(no_card)}"
    log.info("живой поиск %s: %d компаний; %s", query.query_id, len(found), note)
    return Live(matches=sorted(apply_filters(found, query), key=_order(set())), note=note)


GISP_MARK = "реестру Минпромторга (ГИСП)"  # in product_fit of a product confirmed by the registry (#74)
_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)
# Stems that do not tell a product (review of PR #429 on dev lots): «вакцина» met «препараты», «материалы»,
# «медицинский», «применения»; Dr.Web met «электропитание». Compared by 5 letters, as configs/discover.yaml.
GISP_STOPWORDS = ("через", "около", "после", "между", "перед", "медиц", "приме", "препа", "матер", "однор", "компл",
                  "средс", "защит", "элект", "издел", "марки", "массо", "форма", "разме", "назна", "испол")
# A word of exactly MIN_STEM letters is its own stem («шкаф», «стол», «стул», «мыло», «клей», #476); these are the
# 4-letter words of dev lot items that name no product (counted over eval/cases/dev.jsonl), compared whole.
GISP_SHORT_STOPWORDS = ("года", "типа", "виде", "вида", "двух", "трех", "пяти", "одно", "иные", "этап", "мини",
                        "прав", "мест", "спец", "штук", "раза", "июле", "анти", "срок", "теме", "кода", "ктру",
                        "цена", "день", "базе", "базы", "база", "сети", "учет", "корп", "цвет", "тела", "сред",
                        "гост", "фгос", "один", "июль", "июне", "гбук", "гкуз", "губз", "мцбс", "сшор")
GISP_STEP = {"code": "registry", "kind": "registry_kind"}  # match level → key of STEP
GISP_MIN_SHARED = {"code": 1, "kind": 2}  # a neighbour code needs two words of the items in the product's name
MIN_STEM = 4  # a stem of ≤ 3 letters («при», «лот», «мнн») is not a word of the product


PLURAL_ENDINGS = ("ами", "ями", "ов", "ев", "ей", "ых", "их", "ях")  # genitive/instrumental plural: «шкафов» → «шкаф»


def _stem(word: str) -> str:
    """Up to 5 letters. A plural ending (PLURAL_ENDINGS) is cut when ≥ 4 letters stay («шкафов» → «шкаф» meets
    «шкафы»); otherwise one ending letter is dropped from a short word: «папки» → «папк» meets «папка»."""
    for end in PLURAL_ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= MIN_STEM:
            return word[:len(word) - len(end)][:new_fit.PREFIX]
    return word[:max(3, min(new_fit.PREFIX, len(word) - 1))]


def items_text(text: str) -> str:
    """The items of a lot (after the first «;»: «Поставка … для нужд <заказчик>; <позиция>; <позиция>»); a free-text
    query has no «;» and is all items."""
    head, sep, tail = text.partition(";")
    return tail if sep and tail.strip() else head


def query_stems(text: str) -> list[str]:
    """Stems of the lot's ITEMS that may name the product: stems of ≥ MIN_STEM letters, minus the service words of a
    purchase (configs/discover.yaml `query_stopwords`) and GISP_STOPWORDS, compared by 5 letters."""
    stop = {str(w).lower()[:new_fit.PREFIX]
            for w in (*new_fit.config().get("query_stopwords", ()), *GISP_STOPWORDS, *GISP_SHORT_STOPWORDS)}
    out: list[str] = []
    for w in _LETTERS.findall(items_text(text).lower().replace("ё", "е")):
        st = w if len(w) == MIN_STEM else _stem(w)  # #476: «шкаф» minus a letter is «шка», shorter than MIN_STEM
        if len(st) >= MIN_STEM and w[:new_fit.PREFIX] not in stop and st not in out:
            out.append(st)
    return out


def shared_words(stems: Sequence[str], name: str) -> list[str]:
    """Stems of the query that begin a word of the product's name."""
    words = _LETTERS.findall(name.lower().replace("ё", "е"))
    return [s for s in stems if any(w.startswith(s) for w in words)]


@dataclass(frozen=True)
class GispHit:
    """Products of one maker that answer the lot, the best first, and the level of the best one."""

    level: str  # "code" | "kind" (gisp.match_level)
    products: list[gisp.Product]

    @property
    def step(self) -> str:
        return GISP_STEP[self.level]


def _level(codes: Sequence[str], p: gisp.Product) -> str | None:
    levels = {gisp.match_level(c, p.okpd2) for c in codes}
    return "code" if "code" in levels else "kind" if "kind" in levels else None


def gisp_split(by_code: dict[str, list[gisp.Product]], codes: Sequence[str], stems: Sequence[str]
               ) -> tuple[dict[str, GispHit], dict[str, list[gisp.Product]]]:
    """Registry makers of the lot's code → (the product answers the lot: the same code and a word of the items in
    its name, or a code of the same kind and two such words; the rest — the code alone, a role basis only).
    A code is not enough: «прочие» codes are wide (32.50.50.190 holds UV irradiators and incontinence pads), and
    a kind holds neighbours (17.12.14: office paper and till rolls)."""
    hits: dict[str, GispHit] = {}
    code_only: dict[str, list[gisp.Product]] = {}
    for inn, products in by_code.items():
        good: list[tuple[int, int, gisp.Product]] = []
        for p in products:
            level, n = _level(codes, p), len(shared_words(stems, p.name))
            if level is not None and n >= GISP_MIN_SHARED[level]:
                good.append((0 if level == "code" else 1, -n, p))
        if good:
            good.sort(key=lambda t: (t[0], t[1]))  # stable: the closest code first within a rank
            ranked = [p for _, _, p in good]
            hits[inn] = GispHit("code" if good[0][0] == 0 else "kind", ranked)
        else:
            code_only[inn] = products
    return hits, code_only


def _common(a: str, b: str) -> str:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return a[:n]


def _gisp_why(p: gisp.Product, level: str, codes: Sequence[str], predicted: bool, stems: Sequence[str] = ()) -> str:
    """product_fit by the registry: which product, its code against the query's one, the shared words."""
    near = max(codes, key=lambda c: (gisp.match_level(c, p.okpd2) is not None, len(_common(c, p.okpd2))))
    # a predicted code is never presented as the code of the query (#63, #394: it is defined by the text)
    label = "ОКПД2, определённым по тексту запроса," if predicted else "ОКПД2 запроса"
    named = [w for w in _LETTERS.findall(p.name) if any(w.lower().replace("ё", "е").startswith(s) for s in stems)]
    said = f"; слова позиций в наименовании продукции: {', '.join(dict.fromkeys(named))}" if named else ""
    if level == "code":
        return (f"Производит этот товар по {GISP_MARK}: «{p.name}», ОКПД2 {p.okpd2} — совпадает с {label} {near}"
                f"{said}; реестровая запись № {p.reg_number}")
    kind = _common(near, p.okpd2)[:gisp.KIND_DEPTH].rstrip(".")
    return (f"Производит товар того же вида {kind} по {GISP_MARK}: «{p.name}», ОКПД2 {p.okpd2} — код не совпадает "
            f"с {label} {near}{said}; реестровая запись № {p.reg_number}")


def _rescore(m: CounterpartyMatch, cp: Counterparty, comps: list[ScoreComponent]) -> CounterpartyMatch:
    return m.model_copy(update={"counterparty": cp, "components": comps,
                                "score": round(sum(c.value * c.weight for c in comps), 4)})


def gisp_confirm(m: CounterpartyMatch, hit: GispHit | None, other: Sequence[gisp.Product], codes: Sequence[str],
                 predicted: bool, cache: SourceCache, stems: Sequence[str] = ()) -> CounterpartyMatch:
    """A candidate of another channel and the registry: its product answers the lot (`hit`) → product_fit at least
    the registry's step (1.0 the same code, 0.8 the same kind) with the registry as the first reason, the channel's
    own sentence kept; any registry product (`other`) → the role «производитель» by the registry only."""
    cp = m.counterparty
    if hit is not None:
        cp = gisp.with_role(cp, hit.products, cache)
        why = _gisp_why(hit.products[0], hit.level, codes, predicted, stems)
        comps = [c.model_copy(update={"value": max(c.value, STEP[hit.step]), "explanation": f"{why}. {c.explanation}"})
                 if c.name == "product_fit" else c for c in m.components]
        return _rescore(m, cp, comps)
    if other:
        return m.model_copy(update={"counterparty": gisp.with_role(cp, other[:1], cache)})
    return m


def gisp_matches(
    query: Query, codes: Sequence[str], hits: dict[str, GispHit], skip: set[str], cache: SourceCache,
    weights: dict[str, float], predicted: bool, store: Store, region: str, region_note: str | None,
    okpd_class: str, stems: Sequence[str] = (),
) -> tuple[list[CounterpartyMatch], list[str], str]:
    """Registry makers of the query's product that no other channel found (#74). Shown when absent from
    organizers' data, of an INN type of DISCOVERY_NP_TYPES and of the query's region or its neighbour — by the
    KPP of a cached ФНС card, else by the INN's tax office code (said to be unconfirmed; the card is queued).
    Returns the matches, the INNs without a cached card and one line for `Discovery.gisp`."""
    fresh = {inn: hit for inn, hit in hits.items() if inn not in skip}
    known = known_inns(list(fresh), query.as_of, store) or set()
    regions, np_types = _regions(query), _np_types()
    out: list[CounterpartyMatch] = []
    no_card: list[str] = []
    other_type = other_region = 0
    ch = Channel(okved="", okpd_class=okpd_class, relation="class")
    for inn, hit in fresh.items():
        if inn in known:
            continue
        np_type = "UL" if len(inn) == 10 else "IP"
        if np_type not in np_types:
            other_type += 1
            continue
        cp = gisp.counterparty(inn, hit.products, cache)
        if cp is None:
            continue
        cp = cp.model_copy(update={"name": display_name({"name_ex": cp.name, "nptype": np_type}) or cp.name})
        cp, has_card = with_card(cp, cache)
        by_kpp = bool(has_card and cp.kpp and cp.kpp[:2] not in ("00", "99"))
        code = cp.kpp[:2] if by_kpp and cp.kpp else inn[:2]
        if code not in regions:
            other_region += 1
            continue
        if not has_card:
            no_card.append(inn)
        row = {"okved1": "", "regioncode": code}
        comps = []
        for c in _components(cp, row, ch, predicted, has_card, region, weights, region_note):
            update: dict[str, object] = {}
            if c.name == "product_fit":
                update = {"value": STEP[hit.step], "explanation": _gisp_why(hit.products[0], hit.level, codes,
                                                                            predicted, stems)}
            elif c.name == "geography":
                update = {"explanation": c.explanation.replace("Реестр МСП", "Карточка ФНС (КПП)") if by_kpp else
                          c.explanation.replace("Реестр МСП", f"По ИНН (код налогового органа {code}; регион не "
                                                              "подтверждён карточкой ФНС)")}
            elif c.name == "reliability" and not (has_card and cp.status):
                update = {"explanation": "Изготовитель в реестре Минпромторга; статус в ЕГРЮЛ ещё не проверен "
                                         "(обогащение в очереди) — в score не входит"}
            comps.append(c.model_copy(update=update))
        out.append(CounterpartyMatch(counterparty=cp, origin="discovered", components=comps,
                                     score=round(sum(c.value * c.weight for c in comps), 4)))
    line = (f"ГИСП: изготовителей, чья продукция отвечает позициям (код и слово / вид и два слова) {len(hits)}, "
            f"уже найдены другими каналами {len(hits) - len(fresh)}, уже в данных {len(known)}, "
            f"тип ИНН не из DISCOVERY_NP_TYPES {other_type}, другой регион {other_region}, в блок {len(out)}")
    return out, no_card, line


def _regions(query: Query) -> list[str]:
    wanted = query.filters.get("region")
    primary = REGION_CODE.get(wanted if isinstance(wanted, str) and wanted else query.region, "78")
    return [primary, NEIGHBOUR[primary]]


def _np_types() -> list[str]:
    raw = get_settings().discovery_np_types
    return [t for t in (x.strip().upper() for x in raw.split(",")) if t in rmsp.NP_TYPES] or ["UL"]


PAGE_ROWS = 10  # rows on one registry page: fewer → the last page


def _pages(jobs: list[tuple[Channel, str, str]], pages: int,
           last_rows: dict[tuple[str, str, str], int]) -> Iterator[tuple[tuple[Channel, str, str], int | None]]:
    """(job, page number) in order: page 1 of every job, then pages 2..N of a job whose previous page
    was full (`last_rows`, filled lazily by the caller's loop); None — skip, the job has no more rows."""
    for job in jobs:
        yield job, 1
    for n in range(2, pages + 1):
        for job in jobs:
            full = last_rows.get((job[0].okved, job[1], job[2]), 0) >= PAGE_ROWS
            yield job, (n if full else None)


def discover(
    query: Query,
    history: Sequence[CounterpartyMatch] = (),
    *,
    cache: SourceCache | None = None,
    weights: dict[str, float] | None = None,
    processed: Path | None = None,
    enqueue: Callable[[str], object] | None = None,
    offline: bool | None = None,
    budget_s: float | None = LIVE_BUDGET_S,
    client: httpx.Client | None = None,
    max_requests: int | None = MAX_REQUESTS,
    pages: int = 1,
    predicted_class: bool | None = None,
    fit_mode: str | None = None,
    predicted_code: str | None = None,
    pool: bool = False,
    pool_limit: int | None = None,
    min_score: float | None = None,
    max_shown: int = MAX_SHOWN,
) -> Discovery:
    """Companies of the query's OKVED in СПб / ЛО from the МСП registry that are not in organizers' data.

    `/api/search` uses the defaults (budget, MAX_REQUESTS, the first page). `make predict` (#229) lifts
    them: `max_requests=None` — every (OKVED, region, type), `pages` — registry pages per request,
    `predicted_class=True` — build the block by the predicted class whatever DISCOVERY_PREDICTED_CLASS says
    (the caller decided the text is more reliable than the code).
    `fit_mode` (off | shadow | on) overrides new_fit.mode() — for the off/on comparison (#77).
    `predicted_code` — the OKPD2 code search() predicted from the text of this query (search.predicted_code());
    used only when the query has no OKPD2 of its own, and then its full code picks the wholesale row.
    `pool` — add the offline pool of companies (#185, #219) to the block, as `make predict` does (#518): offline,
    after the history check; a company another channel found keeps that channel's row; `pool_limit` — pool rows
    per region (POOL_API_LIMIT by default).
    `min_score` / `max_shown` — the block beyond its mixed top MAX_SHOWN (_mix, #549); `budget_s=None` — no deadline.
    """
    cache = cache or SourceCache()
    store = store_for(processed)  # organizers' data: the store search() read unless a parquet dir is given
    processed = processed or processed_dir()  # the OKPD2 profile of new_fit still reads the parquet
    offline = offline_mode() if offline is None else offline
    if weights is None:
        weights = search_mod.load_config()["weights"]  # validated, as search() reads it (#220 №7)
    assert weights is not None
    res = Discovery()

    classes = okpd_classes(query.okpd2)
    codes: list[str] = [c.strip() for c in query.okpd2]
    if not classes:
        allowed = predicted_class_allowed() if predicted_class is None else predicted_class
        if not allowed:  # #394: on by default, DISCOVERY_PREDICTED_CLASS=0 switches it off
            res.notes.append("ОКПД2 в запросе не указан — блок «Новые» не строится "
                             "(DISCOVERY_PREDICTED_CLASS выключен)")
            return res
        # the code search() predicted from the text (P-02); without it — what its explanations / lots say
        code = predicted_code.strip() if predicted_code and okpd_classes([predicted_code]) else None
        guess = okpd_classes([code])[0] if code else predict_class(history, store)
        if guess is None:
            res.notes.append("ОКПД2 запроса не указан и по тексту не определён — поиск по ОКВЭД невозможен")
            return res
        classes, codes, res.predicted = [guess], [code or guess], True
    res.okpd_classes = classes[:MAX_CLASSES]

    jobs = [(ch, region, np) for region in _regions(query) for ch in okved_plan(codes)
            for np in _np_types()][:max_requests]
    deadline = None if budget_s is None else time.monotonic() + budget_s  # every live request ends before it
    net_ok = True  # False after the first network failure: the rest of the query is cache only
    found: dict[str, tuple[dict[str, str], Channel, CachedPage]] = {}
    last_rows: dict[tuple[str, str, str], int] = {}
    for (ch, region, np_type), page_no in _pages(jobs, pages, last_rows):
        if page_no is None:  # the previous page was the last one
            continue
        label = f"ОКВЭД {ch.okved}, регион {region}, {np_type}" + (f", стр. {page_no}" if page_no > 1 else "")
        url = rmsp.search_url(ch.okved, region, np_type, page_no)
        # no time left before the deadline → the client raises Paused without a request
        live = not offline and net_ok and cache.get(url) is None and not rmsp.blocked()
        paused_source = not offline and rmsp.blocked()  # the source is resting after a failure (#287)
        try:
            page = rmsp.fetch_search(ch.okved, region, np_type, cache, client, offline=not live, deadline=deadline,
                                     page=page_no)
        except rmsp.Paused:  # no request was made: pause of the source or no time left
            net_ok, page = False, None
        except rmsp.Blocked as e:
            net_ok = False
            log.error("discovery: %s", e)
            res.requests.append(f"{label}: источник отказал — остановлено")
            res.notes.append(str(e))
            res.source_failed = True
            continue
        except (httpx.HTTPError, ValueError) as e:
            net_ok = False
            log.warning("discovery: %s: %s", label, e)
            res.requests.append(f"{label}: источник не ответил ({type(e).__name__})")
            res.source_failed = True
            continue
        if page is None:
            res.requests.append(f"{label}: нет в кеше, в сеть не ходили")
            res.source_failed = res.source_failed or paused_source  # not asked because the source failed before
            continue
        if live and cache.get(url) is None:  # the request failed and an outdated answer was used
            net_ok, live = False, False
        answer = rmsp.parse(page.body)
        last_rows[(ch.okved, region, np_type)] = len(answer.rows)
        active = [r for r in answer.rows if r.get("is_active") == "1"]
        res.inactive += len(answer.rows) - len(active)
        count = answer.meta.get("answer.rowCount", "")
        total = count if count.isdigit() else "не сообщается"  # the registry answers -1 for a long list
        res.requests.append(f"{label}: {'сеть' if live else 'кеш'}, всего в реестре {total}, "
                            f"на странице {len(answer.rows)}, действующих в реестре {len(active)}")
        for row in active:
            if row.get("okved1"):  # no OKVED in the row → nothing to explain the match with
                found.setdefault(row["inn"], (row, ch, page))  # first channel wins: class before wholesale

    fit_cfg = new_fit.config()
    res.fit_mode = new_fit.mode(fit_cfg) if fit_mode is None else fit_mode
    if res.fit_mode not in new_fit.MODES:
        raise ValueError(f"fit_mode {res.fit_mode!r}: нужен один из {', '.join(new_fit.MODES)}")
    if res.fit_mode != "off" and found:
        res.profile = new_fit.code_profile(codes, processed, query.as_of, fit_cfg)
        if res.profile is None:
            res.profile_gap = new_fit.profile_gap(processed, fit_cfg)
            log.warning("discovery %s: профиль ОКПД2 не посчитан — %s", query.query_id, res.profile_gap)

    known = known_inns(list(found), query.as_of, store)
    res.history_checked = known is not None
    if known is None:
        res.notes.append("данные организаторов не загружены — сверка с историей закупок не выполнена")
    res.known = len(known or ())

    matches: list[CounterpartyMatch] = []
    relation: dict[str, Relation] = {}
    no_card: set[str] = set()
    target, target_note = search_mod.customer_region(query)  # «География» against the customer's region (#218)
    for inn, (row, ch, page) in found.items():
        if known and inn in known:
            continue
        registry_cp = from_row(row, page)
        cp, has_card = with_card(registry_cp, cache)
        if not has_card:
            no_card.add(inn)
        relation[inn] = ch.relation
        if res.fit_mode != "off":  # only the registry row's own Evidence, checked against its cached page
            res.fit_new[inn] = new_fit.score(
                registry_cp, okved=row["okved1"], relation=ch.relation, base=FIT[ch.relation], codes=codes,
                okpd_class=ch.okpd_class, query_text=query.text, page_body=page.body, profile=res.profile,
                in_page=value_in_page, cfg=fit_cfg, profile_missing=res.profile_gap)
        comps = _components(cp, row, ch, res.predicted, has_card, target, weights, target_note,
                            res.fit_new.get(inn) if res.fit_mode == "on" else None)
        matches.append(CounterpartyMatch(counterparty=cp, origin="discovered", components=comps,
                                         score=round(sum(c.value * c.weight for c in comps), 4)))
    lead_no_card: list[str] = []
    if res.history_checked and leads_enabled():  # web leads (#108): «новые» only after the history check
        try:
            found_leads, lead_relation, lead_no_card, line = lead_matches(
                query, codes, res.okpd_classes, set(found), cache, weights, res.predicted, store, target,
                target_note)
        except Exception:  # noqa: BLE001 — a broken leads file never takes the registry channel down
            log.exception("discovery: веб-лиды пропущены из-за ошибки")
            found_leads, lead_relation, line = [], {}, None
        matches.extend(found_leads)
        relation.update(lead_relation)
        if line:
            res.leads.append(line)
    pool_step: dict[str, str] = {}  # INN → STEP key of a pool company (class / extra / wholesale)
    if pool and res.history_checked:
        found_pool, pool_step = pool_matches(query, codes, {m.counterparty.inn or "" for m in matches} | set(found),
                                             res, weights, cache, store, pool_limit or POOL_API_LIMIT)
        matches.extend(found_pool)
        # an additional OKVED of the class takes a producer's slot of _mix; its own step stays in res.relation
        relation.update({inn: "wholesale" if r == "wholesale" else "class" for inn, r in pool_step.items()})
    gisp_no_card: list[str] = []
    gisp_step: dict[str, str] = {}  # INN → STEP key of the registry's product match (#74)
    if gisp.available():  # ГИСП (#74): registry makers of the query's product; no export → nothing changes
        try:
            stems = query_stems(query.text)
            by_code = gisp.by_okpd2(codes)
            hits, code_only = gisp_split(by_code, codes, stems)
            shown = [m.counterparty.inn for m in matches if m.counterparty.inn]
            any_product = {**gisp.by_inn([i for i in shown if i not in by_code]), **code_only}
            matches = [gisp_confirm(m, hits.get(m.counterparty.inn or ""),
                                    any_product.get(m.counterparty.inn or "", []), codes, res.predicted, cache,
                                    stems)
                       for m in matches]
            res.gisp_backed = {i for i in shown if i in hits}
            extra: list[CounterpartyMatch] = []
            if res.history_checked:  # «новые» only after the history check, as the web leads
                extra, gisp_no_card, line = gisp_matches(
                    query, codes, hits, set(shown), cache, weights, res.predicted, store, target, target_note,
                    res.okpd_classes[0], stems)
                res.gisp.append(line)
            else:
                res.gisp.append("ГИСП: данные организаторов не загружены — компании только из реестра не добавлены")
            matches.extend(extra)
            for m in extra:
                relation[m.counterparty.inn or ""] = "class"  # a maker by the registry takes a producer's slot
                res.gisp_backed.add(m.counterparty.inn or "")
            for inn in res.gisp_backed:
                gisp_step[inn] = hits[inn].step
                relation[inn] = "class"  # a maker of this product takes a producer's slot of _mix
            no_card.update(gisp_no_card)
            res.gisp.append(f"ГИСП: подтверждён товар у найденных по ОКВЭД или веб-поиском "
                            f"{len(res.gisp_backed) - len(extra)}; код без слов позиций (только роль) {len(code_only)}")
        except Exception as e:  # noqa: BLE001 — a broken export never takes the other channels down
            log.exception("discovery: ГИСП пропущен из-за ошибки")
            res.gisp.append(f"ГИСП: выгрузка не прочитана ({type(e).__name__}) — канал пропущен")
    if res.history_checked and res.okpd_classes and web_discover.enabled():  # off by default: no work, no network
        try:
            found_web, web_relation, web_no_card, line = yandex_matches(
                query, codes, res.okpd_classes, {m.counterparty.inn or "" for m in matches}, history, matches, cache,
                weights, res.predicted, store, target, target_note, offline=offline, deadline=deadline)
        except Exception:  # noqa: BLE001 — the paid fallback never takes the free channels down
            log.exception("discovery: запасной путь Яндекс пропущен из-за ошибки")
            found_web, web_relation, web_no_card, line = [], {}, [], None
        matches.extend(found_web)
        relation.update(web_relation)
        lead_no_card += web_no_card
        if line:
            res.leads.append(line)
    order = _order(res.gisp_backed)
    res.candidates = sorted(apply_legal(matches), key=order)
    matches = apply_filters(matches, query)  # liquidated by the ФНС card are dropped here
    matches.sort(key=order)
    res.pool = matches
    res.relation = dict(relation)
    res.relation.update(pool_step)  # extra: an additional OKVED of the class (STEP 0.5)
    res.relation.update(gisp_step)  # the registry's step for make predict (STEP), over the OKVED one
    res.matches = _mix(matches, relation, res.gisp_backed, min_score, max_shown)
    if res.history_checked and res.okpd_classes and eaeu.enabled():  # EAEU (#200): goods only, cache only
        try:
            abroad, line = eaeu.matches(codes, res.okpd_classes, cache, weights, res.predicted)
        except Exception:  # noqa: BLE001 — the EAEU rows never take the block down
            log.exception("discovery: компании ЕАЭС пропущены из-за ошибки")
            abroad, line = [], None
        if line:
            res.eaeu.append(line)
        # after the block's own rows: confirmed by a registry as a company, not as a maker of the product
        res.candidates = [*res.candidates, *apply_legal(abroad)]
        res.matches = [*res.matches, *apply_filters(abroad, query)]
    if res.fit_new:
        log.info("discovery %s: product_fit_new (%s), профиль ОКПД2 %s: %s", query.query_id, res.fit_mode,
                 res.profile.code if res.profile else "—",
                 "; ".join(f"{m.counterparty.inn} {res.fit_new[m.counterparty.inn or ''].value:.2f}"
                           for m in res.matches if (m.counterparty.inn or "") in res.fit_new))
    if enqueue is not None:
        for m in res.matches:  # every shown row gets its card (human decision 02.10: no cap of 10)
            if m.counterparty.inn in no_card:
                enqueue(m.counterparty.inn)
        for inn in lead_no_card:  # a lead is shown only with a card: get it for the next search
            try:
                enqueue(inn)
            except Exception:  # noqa: BLE001 — a dead queue does not empty the block
                log.exception("discovery: лид %s не поставлен в очередь", inn)
                break
    return res


# Step of product_fit by the basis of a «Новые» match (#186, human decision 01.10; v0): the name of the query
# literally on the company's site 1.0, the goods in a catalog / marketplace 0.8, the main OKVED of the class
# 0.6, an additional OKVED of the class 0.5 (decision 22), a wholesale OKVED 0.4. `make predict` shows by it.
STEP = {"site": 1.0, "catalog": 0.8, "class": 0.6, "extra": 0.5, "wholesale": 0.4,
        # ГИСП (#74): the registry names this code with a word of the items — 1.0 (decision of IR630 02.10 in the
        # body of #74); a code of the same kind with two words — 0.8, our reading within that decision (as a catalog)
        "registry": 1.0, "registry_kind": 0.8}
POOL_LIMIT = 400  # pool candidates per region read for one query (main OKVED first)
POOL_EXTRA_FIT = 0.5  # an additional OKVED of the class (decision 22 of IR630): below the 0.6 step of «Новые»


LARGE_SOURCE = {"bo": "ГИР БО", "pb": "Карточка ФНС"}  # who names the region of a company outside the registry


def from_large(row: dict) -> Counterparty:
    """A company of the pool that is NOT in the dump of the МСП registry (#219, `large` of src/db/pool.py):
    the Evidence is built from the cached page of the row by the code of a live answer — from_bo() for ГИР БО,
    from_card() for the registry card. ГИР БО has no KPP: the region is the one its row names, when it is a
    region of src/regions.py. «Не МСП»: the card says so itself (`rsmpcategory`); otherwise it is the lookup
    in the registry dump — the page `msp_absent_page()` that large_pages() cached under the dump's address."""
    inn = row["inn"]
    page = CachedPage(url=row["url"], body=row["body"], fetched_at=row["fetched_at"], status=200)
    cp = from_card(inn, page) if row["source"] == "pb" else from_bo(inn, page)
    ev, update = list(cp.evidence), {}
    line = next((ln for ln in row["body"].splitlines() if ln.startswith(f"row.{inn}.region: ")), None)
    region = REGION_NAME.get(row["region_code"] or "")
    if row["source"] == "bo" and line is not None and region is not None:
        ev.append(Evidence(source=bo_nalog.SOURCE, url=page.url, field="region", value=line,
                           fetched_at=datetime.fromtimestamp(page.fetched_at, UTC),
                           fact=f"Регион по ГИР БО: {line.split(': ', 1)[1]}"))
        update["region"] = region
    if cp.is_msp is None and row["msp_checked_at"] is not None:
        ev.append(Evidence(source=msp_dump.SOURCE, url=msp_dump.page_url(inn), field="msp", value="inn_rows: 0",
                           fetched_at=datetime.fromtimestamp(row["msp_checked_at"], UTC),
                           fact=f"В выгрузке реестра МСП ФНС на {msp_dump.AS_OF} записи с этим ИНН нет — не МСП"))
        update["is_msp"] = False
    return cp.model_copy(update={**update, "evidence": ev}) if update else cp


def pool_source() -> tuple[str | None, Path | None]:
    """(Postgres url, parquet dir) of the pool (#185, D-090): the parquet when built, else Postgres when
    DATABASE_URL is one; (None, None) — the pool is not loaded."""
    from src.db import pool as pool_mod

    if (pool_mod.POOL / "okved.parquet").exists():
        return None, pool_mod.POOL
    url = get_settings().database_url
    return (url, None) if url.startswith("postgres") else (None, None)


_POOL_ROWS: dict[tuple, tuple[list[dict], list[dict]]] = {}  # (prefixes, region, limit, source) → rows; #518
_POOL_ROWS_MAX = 256


def _pool_rows(prefixes: Sequence[str], region: str, limit: int, url: str | None,
               pool_dir: Path) -> tuple[list[dict], list[dict]]:
    """Rows of the pool for one (OKVED prefixes, region): the registry dump (`candidates`, at most `limit`)
    and the companies outside it (`large`). The okved table is 79 M rows and a parquet scan by a code prefix
    takes seconds on it, so a repeated query (the demo, a page of /api/search) reads it once per process;
    the key holds the files' mtime — a rebuilt pool is read again."""
    from src.db import pool as pool_mod

    stamp = tuple((pool_dir / f"{t}.parquet").stat().st_mtime if (pool_dir / f"{t}.parquet").exists() else None
                  for t in ("okved", pool_mod.LARGE))
    key = (tuple(prefixes), region, limit, url, pool_dir.as_posix(), stamp)
    hit = _POOL_ROWS.get(key)
    if hit is None:
        hit = (pool_mod.candidates(prefixes, regions=[region], limit=limit, url=url, pool_dir=pool_dir, by_size=True),
               pool_mod.large_candidates(prefixes, regions=[region], pool_dir=pool_dir))
        if len(_POOL_ROWS) >= _POOL_ROWS_MAX:
            _POOL_ROWS.clear()
        _POOL_ROWS[key] = hit
    return hit


def from_pool(query: Query, codes: Sequence[str], *, predicted: bool, weights: dict[str, float],
              processed: Path | None, cache: SourceCache, url: str | None = None, pool_dir: Path | None = None,
              limit: int = POOL_LIMIT, store: Store | None = None,
              ) -> tuple[list[CounterpartyMatch], dict[str, str], bool | None]:
    """«Новые» from the offline МСП pool (#185) by the same OKVED plan as the live registry search: the
    class (product_fit 1.0 for the main OKVED, POOL_EXTRA_FIT for an additional one) and the wholesale codes
    (0.6). Regions: the query's, then the neighbour. Organizers' suppliers are dropped (`store` — the store
    the caller searched, else the one of `processed`). The third value: organizers' data checked (None — the
    pool is not loaded, nothing was searched)."""
    from src.db import pool as pool_mod

    if url is None and pool_dir is None:
        url, pool_dir = pool_source()
        if url is None and pool_dir is None:
            return [], {}, None
    plan = okved_plan(codes)
    if not plan:
        return [], {}, False
    rows: dict[str, dict] = {}
    large: set[str] = set()  # companies outside the registry dump (#219): the `large` table of the pool
    prefixes, regions = [ch.okved for ch in plan], _regions(query)
    with ThreadPoolExecutor(max_workers=len(regions)) as ex:  # a scan per region; DuckDB releases the GIL
        per_region = list(ex.map(lambda rg: _pool_rows(prefixes, rg, limit, url, pool_dir or pool_mod.POOL), regions))
    for dump_rows, large_rows in per_region:  # the query's region first: its rows win the limit
        for r in dump_rows:
            rows.setdefault(r["inn"], r)
        for r in large_rows:
            if r["inn"] not in rows:
                rows[r["inn"]] = r
                large.add(r["inn"])
    known = known_inns(list(rows), query.as_of, store or store_for(processed))
    if known is None:
        return [], {}, False
    fresh = [inn for inn in rows if inn not in known]
    cps = pool_mod.counterparties([i for i in fresh if i not in large], cache, url=url,
                                  pool_dir=pool_dir or pool_mod.POOL)
    outside = {inn: row for inn, row in pool_mod.large_pages(
        [i for i in fresh if i in large], cache, pool_dir=pool_dir or pool_mod.POOL).items()
        if pool_mod.large_active(row)}  # a newer answer in the cache may say the company is no longer active
    cps |= {inn: from_large(row) for inn, row in outside.items()}
    target, target_note = search_mod.customer_region(query)
    out: list[CounterpartyMatch] = []
    relation: dict[str, str] = {}
    for inn in fresh:
        cp = cps.get(inn)
        if cp is None:
            continue
        code, main = rows[inn]["code"], bool(rows[inn]["main"])
        ch = max((c for c in plan if code.startswith(c.okved)), key=lambda c: len(c.okved), default=plan[0])
        rc = next(((e.value or "").rsplit(": ", 1)[-1] for e in cp.evidence if e.field == "region"), "")
        name = next((e.value or "" for e in cp.evidence if e.field == "okved" and main and ": " not in (e.value or "")),
                    "")
        if inn in outside:  # the region code is the table's: ГИР БО names the region, the card gives a KPP
            rc = outside[inn]["region_code"] or ""
        row = {"okved1": code, "okved1name": name, "regioncode": rc}
        comps = _components(cp, row, ch, predicted, inn in outside, target, weights, target_note)
        if not main:
            fit = POOL_EXTRA_FIT if ch.relation == "class" else min(FIT[ch.relation], POOL_EXTRA_FIT)
            comps = [c.model_copy(update={"value": fit, "explanation": (
                f"Дополнительный ОКВЭД {code} (реестр МСП) — класс ОКПД2 {ch.okpd_class}; основной вид деятельности "
                "другой, поэтому ниже основного")}) if c.name == "product_fit" else c for c in comps]
        where = LARGE_SOURCE[outside[inn]["source"]] if inn in outside else "Выгрузка реестра МСП"
        comps = [c.model_copy(update={"explanation": c.explanation.replace("Реестр МСП", where)})
                 if c.name == "geography" else c for c in comps]
        relation[inn] = ch.relation if main or ch.relation == "wholesale" else "extra"
        out.append(CounterpartyMatch(counterparty=cp, origin="discovered", components=comps,
                                     score=round(sum(c.value * c.weight for c in comps), 4)))
    out = apply_filters(out, query)
    # equal scores within a basis: larger headcount first (N-order-size, #377: winner@10 +0.018), then INN (#518)
    size = {inn: r.get("employees") or 0 for inn, r in rows.items()}
    out.sort(key=lambda m: (-m.score, -size.get(m.counterparty.inn or "", 0), m.counterparty.inn or "~"))
    return out, relation, True


# pool rows per region for one /api/search query: the block shows MAX_SHOWN of them, and the pool's scores tie within
# a basis (class / wholesale), so more rows change nothing on the screen but cost the lookup and the Evidence check
# (≈ 2 s per 100 companies on the real pool, #518)
POOL_API_LIMIT = 25
POOL_MEMO_TTL_S = 600.0  # one answer of the pool per query: the pages, the card and the export repeat the search
_POOL_MEMO: dict[str, tuple[float, list[CounterpartyMatch], dict[str, str], str]] = {}
_POOL_MEMO_MAX = 64
POOL_ABSENT = ("офлайн-пул компаний не загружен (make data PART=pool) — «Новые» только из реестра МСП, "
               "веб-лидов и ГИСП")


def _store_key(store: Store) -> str:
    """Which organizers' data a memoized pool answer was checked against: the parquet dir or the Postgres schema."""
    return f"{type(store).__name__}:{getattr(store, 'processed', None) or getattr(store, 'schema', '')}"


def pool_matches(query: Query, codes: Sequence[str], skip: set[str], res: Discovery, weights: dict[str, float],
                 cache: SourceCache, store: Store, limit: int = POOL_API_LIMIT
                 ) -> tuple[list[CounterpartyMatch], dict[str, str]]:
    """«Новые» of the offline pool for discover(pool=True) — the from_pool() of `make predict` (#518): the same
    OKVED plan, regions, organizers' history before as_of (of the store the query searched), Evidence from the
    pool's cached pages; facts that fail the check are stripped. INNs in `skip` (another channel) are left
    out. No pool — nothing, one note; a broken pool — nothing, one line in `res.requests`."""
    try:
        url, pool_dir = pool_source()
        if url is None and pool_dir is None:
            res.notes.append(POOL_ABSENT)
            return [], {}
        started = time.monotonic()
        key = repr((query.model_dump_json(exclude={"query_id"}), list(codes), res.predicted, sorted(weights.items()),
                    url, pool_dir.as_posix() if pool_dir else None, limit, _store_key(store), cache.path.as_posix()))
        hit = _POOL_MEMO.get(key)
        if hit is not None and started - hit[0] < POOL_MEMO_TTL_S:
            _, extra, relation, _ = hit
            how = f"из памяти процесса, первый запрос {hit[3]}"
        else:
            found, relation, checked = from_pool(query, codes, predicted=res.predicted, weights=weights,
                                                 processed=None, cache=cache, url=url, pool_dir=pool_dir, limit=limit,
                                                 store=store)
            if checked is None:
                res.notes.append(POOL_ABSENT)
                return [], {}
            pages = cached_pages(cache.page_text)  # the pool's pages were cached by from_pool: checked like any
            extra = [m.model_copy(update={"counterparty": strip_unverified(m.counterparty, pages)}) for m in found]
            how = f"{time.monotonic() - started:.1f} с"
            if len(_POOL_MEMO) >= _POOL_MEMO_MAX:
                _POOL_MEMO.clear()
            _POOL_MEMO[key] = (started, extra, relation, how)
    except Exception as e:  # noqa: BLE001 — a broken pool never takes the other channels down
        log.exception("discovery: пул пропущен из-за ошибки")
        res.requests.append(f"пул: ошибка {type(e).__name__} — канал пропущен")
        return [], {}
    added = [m for m in extra if (m.counterparty.inn or "") not in skip]
    res.requests.append(f"пул: кандидатов {len(extra)}, новых к остальным каналам {len(added)} ({how})")
    return added, {m.counterparty.inn or "": relation[m.counterparty.inn or ""] for m in added}


def _report(query: Query, res: Discovery, cache: SourceCache) -> None:
    print(f"\nЗапрос: «{query.text}», ОКПД2 {', '.join(query.okpd2) or '—'} "
          f"→ классы {', '.join(res.okpd_classes) or '—'}"
          f"{' (определён по тексту search(), P-02)' if res.predicted else ''}")
    for line in res.requests:
        print(f"  rmsp: {line}")
    for line in [*res.leads, *res.gisp]:
        print(f"  {line}")
    for note in res.notes:
        print(f"  ! {note}")
    verified = 0
    for m in res.matches:
        errors = check_counterparty(m.counterparty, cache.page_text)
        shown = strip_unverified(m.counterparty, cache.page_text)
        ok = not errors and shown.inn is not None and any(e.url.startswith("http") for e in shown.evidence)
        verified += ok
        print(f"  {m.score:.2f}  {shown.inn or 'ИНН не подтверждён'}  {shown.name}  | ОКВЭД {', '.join(shown.okved)} | "
              f"статус: {shown.status or 'не проверен'} | фактов с источником: {len(shown.evidence)}"
              f"{'' if ok else ' | Evidence НЕ прошло: ' + '; '.join(errors)[:200]}")
    label = "новых (нет в данных организаторов)" if res.history_checked else \
        "кандидатов (сверка с данными организаторов НЕ выполнена — «новыми» не называем)"
    print(f"  ИТОГО: {len(res.matches)} {label}; Evidence прошло у {verified} из {len(res.matches)}; "
          f"уже есть в данных: {res.known}; исключены из реестра МСП: {res.inactive}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discovery v0: живая проверка и прогрев кеша по одному запросу")
    ap.add_argument("text")
    ap.add_argument("--okpd2", action="append", default=[], help="код ОКПД2 запроса (можно несколько)")
    ap.add_argument("--region", default="Санкт-Петербург")
    ap.add_argument("--offline", action="store_true", help="только кеш, без сети")
    ap.add_argument("--enrich", action="store_true",
                    help=f"обогатить показанные компании (до {MAX_SHOWN} ИНН) карточкой pb.nalog.ru")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cache = SourceCache()
    query = Query(query_id="discover-cli", text=args.text, okpd2=args.okpd2, region=args.region)
    offline = args.offline or offline_mode()
    history: list[CounterpartyMatch] = []
    predicted: str | None = None
    if not args.okpd2:  # the text path of the UI: the class is what search() predicts
        try:
            history = search_mod.search(query)
            predicted = search_mod.predicted_code()
        except FileNotFoundError:
            print("  ! данные организаторов не загружены — класс ОКПД2 по тексту не определить, укажите --okpd2")
    res = discover(query, history, cache=cache, offline=offline, budget_s=60.0, predicted_code=predicted)
    if args.enrich and not offline:
        for m in res.matches:
            inn = m.counterparty.inn
            if inn is None or cache.get(pb_nalog.card_url(inn), allow_stale=True) is not None:
                continue
            try:
                enrich(inn, cache)
            except KeyError:
                print(f"  ! {inn}: в реестре ФНС карточки нет")
            except (pb_nalog.CaptchaRequired, TimeoutError, httpx.HTTPError) as e:
                # captcha, 403/429, 5xx or a dead network: stop at the first refusal, do not keep knocking
                code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else ""
                print(f"  ! карточка {inn} не получена ({type(e).__name__} {code}) — обогащение остановлено")
                break
        res = discover(query, history, cache=cache, offline=True, predicted_code=predicted)
    _report(query, res, cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
