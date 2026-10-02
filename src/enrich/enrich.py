"""enrich(inn) -> Counterparty with Evidence for every fact. Owner: SOURCES.

Main source: ФНС «Прозрачный бизнес» (src/sources/pb_nalog.py) through the disk cache, so the demo
works on cache without internet. While pb asks for a captcha (#58, D-052) the fallback is the registry
of МСП by INN (src/sources/rmsp.py), then ГИР БО (src/sources/bo_nalog.py): fewer facts, each with its
own source and url. On top of any of them: offline facts from ФНС open data (src/sources/fns_opendata.py:
headcount, income, tax regime, tax debt) when its parquet is built. Every fact is an Evidence whose `value`
literally occurs in the cached card text (src/enrich/evidence.py checks it; the API shows only what passes).
Organizers' data stats (`Counterparty.stats`) come from search(), not from here.

After the ФНС card, `_merge_rnp` adds РНП ЕИС (`Counterparty.in_rnp`, src/sources/eis_rnp.py),
D-141 (#111): a failure or a captcha never turns into a guessed fact, only a logged warning.
The Электронный магазин СПб registry is NOT a source of enrich() or discover() (D-061: an expert's
feedback after checkpoint 1 — АИС ГЗ is the primary source, "обогащение вашими данными" reads as
circular to the jury); the estore.py client was removed (D-067).

`ENRICH_OFFLINE=1` — cache only, no network (KeyError for an INN that is not cached).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import httpx

from src.contracts import Counterparty, Evidence, Kind, inn_is_valid
from src.sources import bo_nalog, eis_rnp, fns_opendata, pb_nalog, rmsp, role
from src.sources.cache import CachedPage, SourceCache
from src.sources.rmsp import MSP_CATEGORY

log = logging.getLogger(__name__)


def _region(kpp: str) -> str | None:
    return {"78": "Санкт-Петербург", "47": "Ленинградская область"}.get(kpp[:2])


def from_card(inn: str, page: CachedPage) -> Counterparty:
    """Card text → Counterparty. Only what the card says; every shown fact has an Evidence."""
    f = pb_nalog.parse(page.body)
    at = datetime.fromtimestamp(page.fetched_at, tz=UTC)
    ev: list[Evidence] = []

    def add(field: str, value: str | None, fact: str) -> bool:
        if not value:
            return False
        ev.append(Evidence(source=pb_nalog.SOURCE, url=page.url, fetched_at=at, fact=fact,
                           field=field, value=value))
        return True

    is_ul = len(inn) == 10
    add("inn", inn, f"ИНН {inn} найден в реестре ФНС")
    ogrn = f.get("vyp.ОГРН") or f.get("vyp.ОГРНИП") or f.get("search.ogrn")
    add("ogrn", ogrn, f"{'ОГРН' if is_ul else 'ОГРНИП'} {ogrn}")

    if is_ul:
        short = f.get("vyp.НаимЮЛСокр") or f.get("search.namec")
        full = f.get("vyp.НаимЮЛПолн") or f.get("search.namep")
        name = short or full
        add("name", name, f"Наименование: {name}")
        kpp = f.get("vyp.КПП")
        add("kpp", kpp, f"КПП {kpp}")
        address = f.get("vyp.АдресРФ")
        add("address", address, f"Адрес: {address}")
    else:  # ИП: only what the public registry shows (CLAUDE.md, 152-ФЗ) — no address
        fio = f.get("search.namec")
        name = f"ИП {fio}" if fio else None
        add("name", fio, f"Индивидуальный предприниматель {fio}")
        kpp = None

    status = f.get("vyp.sulst_name_ex") or f.get("search.sulst_name_ex")
    add("status", status, f"Статус: {status}")

    okved_code = f.get("vyp.КодОКВЭД") or f.get("search.okved2main")
    okved_name = f.get("vyp.НаимОКВЭД") or f.get("search.okved2mainname")
    add("okved", okved_name, f"Основной ОКВЭД {okved_code} — {okved_name}")
    key = "vyp.КодОКВЭД" if f.get("vyp.КодОКВЭД") else "search.okved2main"
    kind = role.assign(okved_code, okved_name, f"{key}: {okved_code}", add)

    # МСП only from the card's own value: the line `vyp.rsmpcategory: <c>` is the Evidence value.
    # No key (empty card, ИП without it) → None («не проверено»), never a guessed «нет».
    category = f.get("vyp.rsmpcategory")
    is_msp: bool | None = None
    if category in MSP_CATEGORY:
        is_msp = True
        since = f.get("vyp.rsmpdate", "").split(" ")[0]
        add("msp", f"rsmpcategory: {category}",
            f"Реестр МСП: {MSP_CATEGORY[category]}" + (f", включено {since}" if since else ""))
    elif category == "0":
        is_msp = False
        add("msp", "rsmpcategory: 0", "В реестре МСП не состоит (категория МСП в карточке ФНС: 0)")

    return Counterparty(
        inn=inn,
        kpp=kpp,
        name=name or f"ИНН {inn}",
        kind=kind,
        region=_region(kpp) if kpp else None,  # KPP is evidenced; INN prefix is not a fact
        status=status,
        is_msp=is_msp,
        okved=[okved_code] if okved_code else [],
        evidence=ev,
    )


def _evidence(source: str, page: CachedPage, ev: list[Evidence]):
    at = datetime.fromtimestamp(page.fetched_at, tz=UTC)

    def add(field: str, value: str | None, fact: str) -> bool:
        if not value:
            return False
        ev.append(Evidence(source=source, url=page.url, fetched_at=at, fact=fact, field=field, value=value))
        return True

    return add


def _region_by_code(code: str | None) -> str | None:
    return {"78": "Санкт-Петербург", "47": "Ленинградская область"}.get(code or "") or (
        f"регион {code}" if code else None)


def _msp(row: dict[str, str], add) -> bool | None:  # type: ignore[no-untyped-def]
    """Registry row → is_msp. In the registry = a subject of МСП; value is the line on the page."""
    cat = row.get("category")
    label = MSP_CATEGORY.get(cat or "")
    if row.get("is_active") == "1" and label:
        add("msp", f"category: {cat}", f"Реестр МСП: {label}, запись действующая")
        return True
    if row.get("is_active") == "0":
        add("msp", "is_active: 0", "Реестр МСП: запись есть, но не действующая")
        return False
    return None  # active with an unknown category, or no flag: the page does not say


def from_rmsp(inn: str, page: CachedPage) -> Counterparty:
    """Quick-search answer of the МСП registry → Counterparty. Empty answer: not in the registry (is_msp
    False, backed by `answer.inn_rows: 0` on the page) — not «the company does not exist»."""
    row = rmsp.parse(page.body).rows
    row = next((r for r in row if r.get("inn") == inn), None)  # type: ignore[assignment]
    ev: list[Evidence] = []
    add = _evidence(rmsp.SOURCE, page, ev)
    if row is None:
        add("msp", "inn_rows: 0", "В реестре МСП не найден (запрос по ИНН); это не значит, что компании нет")
        return Counterparty(inn=inn, name=f"ИНН {inn}", is_msp=False, evidence=ev)
    add("inn", inn, f"ИНН {inn} найден в реестре МСП")
    add("ogrn", row.get("ogrn"), f"{'ОГРН' if len(inn) == 10 else 'ОГРНИП'} {row.get('ogrn')}")
    name = row.get("name_ex")
    add("name", name, f"Наименование: {name}")
    is_msp = _msp(row, add)
    okved = row.get("okved1")
    add("okved", row.get("okved1name"), f"Основной ОКВЭД {okved} — {row.get('okved1name')}")
    kind = role.assign(okved, row.get("okved1name"), f"row.{inn}.okved1: {okved}", add)
    code = row.get("regioncode")
    add("region", f"regioncode: {code}" if code else None, f"Регион реестра МСП: {_region_by_code(code)}")
    return Counterparty(inn=inn, name=name or f"ИНН {inn}", kind=kind, region=_region_by_code(code), is_msp=is_msp,
                        okved=[okved] if okved else [], evidence=ev)


# ГИР БО statusCode is an English code; the hard filter (src/match/filters.py, _DEAD) looks for Russian
# stems. Codes of a stopped company are mapped by stem; the list of real codes is not confirmed
# [ПРОВЕРИТЬ] — an unknown code is kept as is (shown, not filtered), never guessed.
_BO_DEAD = (("BANKRUPT", "банкротство"), ("LIQUIDAT", "ликвидация"), ("TERMINAT", "деятельность прекращена"),
            ("DISSOLV", "деятельность прекращена"), ("CLOSED", "деятельность прекращена"))


def bo_status(code: str | None) -> str | None:
    """ГИР БО statusCode -> status text: ACTIVE -> «действующая», a stop code -> a Russian stem + the code."""
    if not code:
        return None
    if code.upper() == "ACTIVE":
        return "действующая"
    for stem, text in _BO_DEAD:
        if stem in code.upper():
            return f"{text} (ГИР БО: {code})"
    return code


def from_bo(inn: str, page: CachedPage, msp_page: CachedPage | None = None) -> Counterparty:
    """ГИР БО row → Counterparty. `msp_page`: the empty МСП answer for the same INN (is_msp False)."""
    f = bo_nalog.parse(page.body)
    ev: list[Evidence] = []
    add = _evidence(bo_nalog.SOURCE, page, ev)
    kind: Kind | None = None
    if f:
        add("inn", inn, f"ИНН {inn} найден в ГИР БО")
        add("ogrn", f.get("ogrn"), f"ОГРН {f.get('ogrn')}")
        add("name", f.get("shortName"), f"Наименование: {f.get('shortName')}")
        code = f.get("statusCode")
        add("status", f"statusCode: {code}" if code else None, f"Статус в ГИР БО: {code}")
        add("okved", f.get("okved2"), f"Основной ОКВЭД {f.get('okved2')} (код, ГИР БО)")
        kind = role.assign(f.get("okved2"), None, f"row.{inn}.okved2: {f.get('okved2')}", add)
    is_msp = None
    if msp_page is not None and not any(r.get("inn") == inn for r in rmsp.parse(msp_page.body).rows):
        add_msp = _evidence(rmsp.SOURCE, msp_page, ev)
        add_msp("msp", "inn_rows: 0", "В реестре МСП не найден (запрос по ИНН)")
        is_msp = False
    status = bo_status(f.get("statusCode"))
    return Counterparty(inn=inn, name=f.get("shortName") or f"ИНН {inn}", kind=kind, region=None, status=status,
                        is_msp=is_msp, okved=[f["okved2"]] if f.get("okved2") else [], evidence=ev)


def _fallback(inn: str, cache: SourceCache, offline: bool) -> Counterparty | None:
    """rmsp by INN, then bo when rmsp has no row. None: neither source gave an answer at all."""
    msp: CachedPage | None = None
    try:
        msp = rmsp.fetch_quick(inn, cache, offline=offline)
    except (rmsp.Blocked, httpx.HTTPError, ValueError) as e:
        log.warning("enrich %s: реестр МСП недоступен: %s", inn, e)
    if msp is not None and any(r.get("inn") == inn for r in rmsp.parse(msp.body).rows):
        return from_rmsp(inn, msp)
    bo: CachedPage | None = None
    try:
        bo = bo_nalog.fetch(inn, cache, offline=offline)
    except (bo_nalog.Blocked, httpx.HTTPError, ValueError) as e:
        log.warning("enrich %s: ГИР БО недоступен: %s", inn, e)
    if bo is not None and bo_nalog.parse(bo.body):
        return from_bo(inn, bo, msp)
    if msp is not None:
        return from_rmsp(inn, msp)
    return None


def _merge_rnp(inn: str, cp: Counterparty, cache: SourceCache, offline: bool) -> Counterparty:
    """ЕИС РНП → `Counterparty.in_rnp` + Evidence (D-141, #111). `offline` is passed through
    explicitly (never the `ENRICH_OFFLINE` environment variable) so the caller — not a process-wide
    flag — decides whether a live request (paced, under lock, Crawl-delay 60 s) may happen at all:
    the API always passes `offline=True` via `rnp_offline` below, the worker does not, and only the
    worker is where a live РНП request is allowed to happen (research/SOURCES.md).
    `KeyError` (offline, nothing cached yet) is the normal state of a not-yet-checked INN — quiet.
    Any other failure (`Blocked`, HTTP, a bad INN) leaves `in_rnp` as is (None, "не проверено"),
    never guesses `False`: a logged warning is the only effect."""
    try:
        res = eis_rnp.check(inn, cache, offline=offline)
    except KeyError:
        return cp
    except (eis_rnp.Blocked, httpx.HTTPError, ValueError) as e:
        log.warning("enrich %s: РНП ЕИС недоступен: %s", inn, e)
        return cp
    update: dict[str, object] = {}
    if res.evidence:
        update["evidence"] = [*cp.evidence, *res.evidence]
    if res.in_rnp is not None:
        update["in_rnp"] = res.in_rnp
    return cp.model_copy(update=update) if update else cp


def add_open_data(cards: list[Counterparty], cache: SourceCache) -> list[Counterparty]:
    """The cards plus the offline facts of ФНС open data, each with its own url (one query for all, no network)."""
    facts = fns_opendata.lookup_many([c.inn for c in cards if c.inn and inn_is_valid(c.inn)], cache)

    def merged(c: Counterparty) -> Counterparty:
        have = {(e.url, e.field, e.value) for e in c.evidence}  # a card that already has them gets them once
        new = [e for e in facts.get(c.inn or "", []) if (e.url, e.field, e.value) not in have]
        return c.model_copy(update={"evidence": [*c.evidence, *new]}) if new else c
    return [merged(c) for c in cards]


def enrich(inn: str, cache: SourceCache | None = None, *, offline: bool | None = None,
           open_data: bool = True, rnp_offline: bool | None = None) -> Counterparty:
    """The base card (below), then РНП ЕИС merged in (D-141, #111), plus add_open_data(); `open_data=False` —
    no ФНС open-data facts (the search loop over 200 candidates needs none: the page and the export add them to
    what they show). Электронный магазин СПб (its client was removed, D-067) is NOT a source here (D-061).
    `rnp_offline` governs the РНП step only; None follows `offline`. The API passes `rnp_offline=True`
    explicitly (src/api/app.py `api_counterparty`), so a direct card lookup never waits on the 60 s
    Crawl-delay pause — only the worker (`src/worker/worker.py`, `src/worker/prefetch.py`), which calls
    `enrich(inn)` with neither flag set, may do a live РНП request."""
    cache = cache or SourceCache()
    cp = _enrich(inn, cache, offline, rnp_offline)
    return add_open_data([cp], cache)[0] if open_data else cp


def _enrich(inn: str, cache: SourceCache, offline: bool | None, rnp_offline: bool | None) -> Counterparty:
    """The base card, then РНП ЕИС. `offline=True` — cache only (KeyError when nothing is cached); None — by
    `ENRICH_OFFLINE`."""
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    if offline is None:
        offline = os.environ.get("ENRICH_OFFLINE") == "1"
    if rnp_offline is None:
        rnp_offline = offline
    result = _primary(inn, cache, offline)
    return _merge_rnp(inn, result, cache, rnp_offline)


def _primary(inn: str, cache: SourceCache, offline: bool) -> Counterparty:
    """pb card from cache → pb live (unless blocked) → rmsp by INN → bo (unchanged from before #111:
    the error of pb is raised only if no fallback source answered)."""
    err: Exception
    if offline:
        page = cache.get(pb_nalog.card_url(inn), allow_stale=True)
        if page is not None:
            return from_card(inn, page)
        err = KeyError(inn)
    else:
        fresh = cache.get(pb_nalog.card_url(inn))
        if fresh is not None:
            return from_card(inn, fresh)
        try:
            return from_card(inn, pb_nalog.fetch_card(inn, cache))
        except (pb_nalog.CaptchaRequired, pb_nalog.NotFound, TimeoutError, httpx.HTTPError) as e:
            err = e
            log.warning("enrich %s: pb.nalog.ru не ответил (%s) — запасные источники", inn, e)
    fallback = _fallback(inn, cache, offline)
    if fallback is None:
        raise err
    return fallback
