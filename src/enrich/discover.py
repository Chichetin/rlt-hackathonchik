"""discover(query) -> companies from open sources that are NOT in organizers' data. Owner: SOURCES.

Block «Новые из открытых источников» (`origin="discovered"`, #16, D-036, D-037):
1. OKPD2 class of the query (first 4 digits): `Query.okpd2`, or — when empty — the class search()
   itself predicted and wrote into its `product_fit` explanations (so both blocks speak about the
   same class); if it wrote none — the class voted by the lots of the top history matches
   (`data:items#<lot>` Evidence of search()).
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
5. Every shown fact is an Evidence whose value literally occurs in the cached answer. The ФНС card
   (status, KPP, address) is added only from the cache; an INN without a cached card goes to the
   enrichment queue (`enqueue`), so the request itself makes no pb.nalog.ru calls.

`DISCOVERY_OFFLINE=1` or `ENRICH_OFFLINE=1` — cache only, no network.
`DISCOVERY_PREDICTED_CLASS=1` — also build the block for a query WITHOUT OKPD2, by the class search()
predicts. Off by default: until #63 the prediction is wrong on real data («бумага офисная» → paints),
and a wrong class would show irrelevant companies with product_fit 1.0. Without the flag such a query
gets an empty block and no network request.
CLI (live check, cache warm-up): `python -m src.enrich.discover "бумага офисная" --okpd2 17.12.14`;
without `--okpd2` the class is the one search() predicts on organizers' data (as in /api/search).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import duckdb
import httpx
import yaml

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query, ScoreComponent
from src.enrich import role
from src.enrich.enrich import MSP_CATEGORY, enrich, from_card
from src.enrich.evidence import check_counterparty, strip_unverified, value_in_page
from src.ingest.load import suppliers_path
from src.match import new_fit
from src.match import search as search_mod
from src.match.filters import REGION_CODE, apply_filters, apply_legal
from src.match.index import processed_dir
from src.sources import pb_nalog, rmsp
from src.sources.cache import CachedPage, SourceCache

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
SEARCH_CONFIG = ROOT / "configs" / "search.yaml"

MAX_SHOWN = 10  # companies in the block and INNs sent to enrichment per query
MAX_REQUESTS = 6  # rmsp requests per query (cached or live): class + up to 2 wholesale codes × 2 regions
MAX_CLASSES = 2  # OKPD2 classes of one query that get their own requests
LIVE_BUDGET_S = 4.0  # all live rmsp requests of one /api/search end within this wall time
VOTE_MATCHES = 10  # top history matches whose lots vote for the OKPD2 class
FIT = {"class": 1.0, "wholesale": 0.6}  # product_fit of a discovered company (D-036)
NEIGHBOUR = {"78": "47", "47": "78"}
NEIGHBOUR_VALUE = 0.8  # as in src/match/search.py
REGION_NAME = {v: k for k, v in REGION_CODE.items()}
COMPONENT_ORDER = ("product_fit", "experience", "reliability", "activity", "geography", "price")

# OKPD2 prefix (longest wins) → OKVED2 wholesale codes for these goods, the main one first. A hand-made
# table, not an official key (D-036; second codes and new rows — #108). The name of the code shown to the
# user always comes from the registry row. Every code here is «46.x except 46.1» — a distributor by
# src/enrich/role.py.
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
    notes: list[str] = field(default_factory=list)
    # product_fit_new (#77, src/match/new_fit.py): off — not computed; shadow — computed here only;
    # on — it IS product_fit of every match (experiment only, confirmation №3 to make it the default)
    fit_mode: str = "off"
    fit_new: dict[str, new_fit.NewFit] = field(default_factory=dict)  # INN → product_fit_new
    profile: new_fit.Profile | None = None  # signal 8: words of the full OKPD2 code of the query
    profile_gap: str | None = None  # why `profile` is None in shadow / on: «not computed», not «0»


def predicted_class_allowed() -> bool:
    return os.environ.get("DISCOVERY_PREDICTED_CLASS") == "1"


def offline_mode() -> bool:
    return os.environ.get("DISCOVERY_OFFLINE") == "1" or os.environ.get("ENRICH_OFFLINE") == "1"


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

    TODO(#63, area:match): search() returns its predicted code only inside a Russian sentence of the
    explanation; parsing it is fragile (a reworded sentence silently switches us to the lot vote —
    tests/unit/test_discover.py::test_class_is_predicted_from_history_lots pins the wording). When
    search() exposes the predicted code as data, read it from there. The prediction itself is weak
    on real data (#63: «бумага офисная» → 20.30), so the class is always shown as «определён
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


def predict_class(history: Sequence[CounterpartyMatch], processed: Path) -> str | None:
    """OKPD2 class of a query without codes: stated by search(), else voted by the lots behind the
    top history matches; None when neither can be read."""
    return stated_class(history) or _lots_class(history, processed)


def _lots_class(history: Sequence[CounterpartyMatch], processed: Path) -> str | None:
    lots: dict[int, float] = {}
    for m in history[:VOTE_MATCHES]:
        for e in m.counterparty.evidence:
            if e.url.startswith(_LOT_URL) and e.url[len(_LOT_URL):].isdigit():
                lot = int(e.url[len(_LOT_URL):])
                lots[lot] = max(lots.get(lot, 0.0), m.score)
    items = processed / "items.parquet"
    if not lots or not items.exists():
        return None
    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT DISTINCT lot_id, okpd2_l4 FROM read_parquet(?) "
            "WHERE lot_id IN (SELECT unnest(?)) AND okpd2_l4 IS NOT NULL",
            [str(items), list(lots)],
        ).fetchall()
    except duckdb.Error as e:
        log.warning("discovery: items.parquet не прочитан: %s", e)
        return None
    finally:
        con.close()
    by_lot: dict[int, list[str]] = {}
    for lot, cls in rows:
        if _OKPD_CLASS.fullmatch(cls):
            by_lot.setdefault(lot, []).append(cls)
    votes: dict[str, float] = {}
    for lot, classes in by_lot.items():
        for cls in classes:  # a lot with several classes splits its vote
            votes[cls] = votes.get(cls, 0.0) + lots[lot] / len(classes)
    return min(votes, key=lambda c: (-votes[c], c)) if votes else None


_SUPPLIERS: dict[tuple[str, int], frozenset[str]] = {}  # (file, mtime) → all INNs of organizers' data


def _all_inns(path: Path) -> frozenset[str] | None:
    """Every INN of a suppliers parquet, read once per file version (a rebuilt file is read again)."""
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _SUPPLIERS:
        con = duckdb.connect()
        try:
            rows = con.execute("SELECT DISTINCT inn FROM read_parquet(?) WHERE inn IS NOT NULL", [str(path)]).fetchall()
        except duckdb.Error as e:
            log.warning("discovery: %s не прочитан: %s", path.name, e)
            return None
        finally:
            con.close()
        for old in [k for k in _SUPPLIERS if k[0] == key[0]]:
            del _SUPPLIERS[old]
        _SUPPLIERS[key] = frozenset(r[0] for r in rows)
    return _SUPPLIERS[key]


def known_inns(inns: Sequence[str], as_of: date | None, processed: Path) -> set[str] | None:
    """INNs present in organizers' data (before as_of); None — the data is absent, nothing checked."""
    try:
        path = suppliers_path(as_of, processed)
        everyone = _all_inns(path)
    except OSError:  # FileNotFoundError from suppliers_path or a file removed meanwhile
        return None
    return None if everyone is None else set(inns) & everyone


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
    if ch.relation == "class":  # the same rule as the role of the card (src/enrich/role.py, D-057)
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
    where = REGION_NAME.get(code, f"регион с кодом {code}")
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


def _mix(ranked: list[CounterpartyMatch], relation: dict[str, Relation]) -> list[CounterpartyMatch]:
    """Top MAX_SHOWN with both kinds present: producers and distributors take slots in turn (best
    first within a kind), so ten producers do not hide every distributor; shown in score order."""
    queues = [[m for m in ranked if relation[m.counterparty.inn or ""] == r] for r in ("class", "wholesale")]
    picked: list[CounterpartyMatch] = []
    while len(picked) < MAX_SHOWN and any(queues):
        for q in queues:
            if q and len(picked) < MAX_SHOWN:
                picked.append(q.pop(0))
    return sorted(picked, key=lambda m: (-m.score, m.counterparty.inn or "~"))


def _regions(query: Query) -> list[str]:
    wanted = query.filters.get("region")
    primary = REGION_CODE.get(wanted if isinstance(wanted, str) and wanted else query.region, "78")
    return [primary, NEIGHBOUR[primary]]


def _np_types() -> list[str]:
    raw = os.environ.get("DISCOVERY_NP_TYPES", "UL")
    return [t for t in (x.strip().upper() for x in raw.split(",")) if t in rmsp.NP_TYPES] or ["UL"]


def discover(
    query: Query,
    history: Sequence[CounterpartyMatch] = (),
    *,
    cache: SourceCache | None = None,
    weights: dict[str, float] | None = None,
    processed: Path | None = None,
    enqueue: Callable[[str], object] | None = None,
    offline: bool | None = None,
    budget_s: float = LIVE_BUDGET_S,
    client: httpx.Client | None = None,
    fit_mode: str | None = None,
) -> Discovery:
    """Companies of the query's OKVED in СПб / ЛО from the МСП registry that are not in organizers' data.

    `fit_mode` (off | shadow | on) overrides new_fit.mode() — for the off/on comparison (#77).
    """
    cache = cache or SourceCache()
    processed = processed or processed_dir()
    offline = offline_mode() if offline is None else offline
    if weights is None:
        weights = yaml.safe_load(SEARCH_CONFIG.read_text(encoding="utf-8"))["weights"]
    assert weights is not None
    res = Discovery()

    classes = okpd_classes(query.okpd2)
    codes: list[str] = [c.strip() for c in query.okpd2]
    if not classes:
        if not predicted_class_allowed():  # D-036: off until search() predicts the class reliably (#63)
            res.notes.append("ОКПД2 в запросе не указан — блок «Новые» не строится "
                             "(DISCOVERY_PREDICTED_CLASS выключен)")
            return res
        guess = predict_class(history, processed)
        if guess is None:
            res.notes.append("ОКПД2 запроса не указан и не определён по истории — поиск по ОКВЭД невозможен")
            return res
        classes, codes, res.predicted = [guess], [guess], True
    res.okpd_classes = classes[:MAX_CLASSES]

    jobs = [(ch, region, np) for region in _regions(query) for ch in okved_plan(codes)
            for np in _np_types()][:MAX_REQUESTS]
    deadline = time.monotonic() + budget_s  # every live request of this query ends before it
    net_ok = True  # False after the first network failure: the rest of the query is cache only
    found: dict[str, tuple[dict[str, str], Channel, CachedPage]] = {}
    for ch, region, np_type in jobs:
        label = f"ОКВЭД {ch.okved}, регион {region}, {np_type}"
        url = rmsp.search_url(ch.okved, region, np_type)
        # no time left before the deadline → the client raises Paused without a request
        live = not offline and net_ok and cache.get(url) is None and not rmsp.blocked()
        try:
            page = rmsp.fetch_search(ch.okved, region, np_type, cache, client, offline=not live, deadline=deadline)
        except rmsp.Paused:  # no request was made: pause of the source or no time left
            net_ok, page = False, None
        except rmsp.Blocked as e:
            net_ok = False
            log.error("discovery: %s", e)
            res.requests.append(f"{label}: источник отказал — остановлено")
            res.notes.append(str(e))
            continue
        except (httpx.HTTPError, ValueError) as e:
            net_ok = False
            log.warning("discovery: %s: %s", label, e)
            res.requests.append(f"{label}: источник не ответил ({type(e).__name__})")
            continue
        if page is None:
            res.requests.append(f"{label}: нет в кеше, в сеть не ходили")
            continue
        if live and cache.get(url) is None:  # the request failed and an outdated answer was used
            net_ok, live = False, False
        answer = rmsp.parse(page.body)
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

    known = known_inns(list(found), query.as_of, processed)
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
                cfg=fit_cfg, profile_missing=res.profile_gap)
        comps = _components(cp, row, ch, res.predicted, has_card, target, weights, target_note,
                            res.fit_new.get(inn) if res.fit_mode == "on" else None)
        matches.append(CounterpartyMatch(counterparty=cp, origin="discovered", components=comps,
                                         score=round(sum(c.value * c.weight for c in comps), 4)))
    res.candidates = sorted(apply_legal(matches), key=lambda m: (-m.score, m.counterparty.inn or "~"))
    matches = apply_filters(matches, query)  # liquidated by the ФНС card are dropped here
    matches.sort(key=lambda m: (-m.score, m.counterparty.inn or "~"))
    res.pool = matches
    res.matches = _mix(matches, relation)
    if res.fit_new:
        log.info("discovery %s: product_fit_new (%s), профиль ОКПД2 %s: %s", query.query_id, res.fit_mode,
                 res.profile.code if res.profile else "—",
                 "; ".join(f"{m.counterparty.inn} {res.fit_new[m.counterparty.inn or ''].value:.2f}"
                           for m in res.matches if (m.counterparty.inn or "") in res.fit_new))
    if enqueue is not None:
        for m in res.matches:
            if m.counterparty.inn in no_card:
                enqueue(m.counterparty.inn)
    return res


def _report(query: Query, res: Discovery, cache: SourceCache) -> None:
    print(f"\nЗапрос: «{query.text}», ОКПД2 {', '.join(query.okpd2) or '—'} "
          f"→ классы {', '.join(res.okpd_classes) or '—'}"
          f"{' (определён автоматически search() по похожим закупкам)' if res.predicted else ''}")
    for line in res.requests:
        print(f"  rmsp: {line}")
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
    if not args.okpd2:  # the text path of the UI: the class is what search() predicts
        try:
            history = search_mod.search(query)
        except FileNotFoundError:
            print("  ! данные организаторов не загружены — класс ОКПД2 по тексту не определить, укажите --okpd2")
    res = discover(query, history, cache=cache, offline=offline, budget_s=60.0)
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
        res = discover(query, history, cache=cache, offline=True)
    _report(query, res, cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
