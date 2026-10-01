"""enrich(inn) -> Counterparty with Evidence for every fact. Owner: SOURCES.

Main source: ФНС «Прозрачный бизнес» (src/sources/pb_nalog.py) through the disk cache, so the demo
works on cache without internet. While pb asks for a captcha (#58, D-052) the fallback is the registry
of МСП by INN (src/sources/rmsp.py), then ГИР БО (src/sources/bo_nalog.py): fewer facts, each with its
own source and url. Every fact is an Evidence whose `value` literally occurs in the
cached card text (src/enrich/evidence.py checks it; the API shows only what passes).
Organizers' data stats (`Counterparty.stats`) come from search(), not from here.

`ENRICH_OFFLINE=1` — cache only, no network (KeyError for an INN that is not cached).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import httpx

from src.contracts import Counterparty, Evidence, inn_is_valid
from src.sources import bo_nalog, pb_nalog, rmsp
from src.sources.cache import CachedPage, SourceCache

log = logging.getLogger(__name__)

MSP_CATEGORY = {"1": "микропредприятие", "2": "малое предприятие", "3": "среднее предприятие"}


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
    if "is_active" in row:
        add("msp", f"is_active: {row['is_active']}", "Реестр МСП: запись есть, но не действующая")
        return False
    return None


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
    code = row.get("regioncode")
    add("region", f"regioncode: {code}" if code else None, f"Регион реестра МСП: {_region_by_code(code)}")
    return Counterparty(inn=inn, name=name or f"ИНН {inn}", region=_region_by_code(code), is_msp=is_msp,
                        okved=[okved] if okved else [], evidence=ev)


def from_bo(inn: str, page: CachedPage, msp_page: CachedPage | None = None) -> Counterparty:
    """ГИР БО row → Counterparty. `msp_page`: the empty МСП answer for the same INN (is_msp False)."""
    f = bo_nalog.parse(page.body)
    ev: list[Evidence] = []
    add = _evidence(bo_nalog.SOURCE, page, ev)
    if f:
        add("inn", inn, f"ИНН {inn} найден в ГИР БО")
        add("ogrn", f.get("ogrn"), f"ОГРН {f.get('ogrn')}")
        add("name", f.get("shortName"), f"Наименование: {f.get('shortName')}")
        code = f.get("statusCode")
        add("status", f"statusCode: {code}" if code else None, f"Статус в ГИР БО: {code}")
        add("okved", f.get("okved2"), f"Основной ОКВЭД {f.get('okved2')} (код, ГИР БО)")
    is_msp = None
    if msp_page is not None and not any(r.get("inn") == inn for r in rmsp.parse(msp_page.body).rows):
        add_msp = _evidence(rmsp.SOURCE, msp_page, ev)
        add_msp("msp", "inn_rows: 0", "В реестре МСП не найден (запрос по ИНН)")
        is_msp = False
    status = {"ACTIVE": "действующая"}.get(f.get("statusCode", ""), f.get("statusCode"))
    return Counterparty(inn=inn, name=f.get("shortName") or f"ИНН {inn}", region=None, status=status,
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


def enrich(inn: str, cache: SourceCache | None = None) -> Counterparty:
    """pb card from cache → pb live (unless blocked) → rmsp by INN → bo. The error of pb is raised
    only if no fallback source answered; otherwise the source of every fact is in its Evidence."""
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    cache = cache or SourceCache()
    offline = os.environ.get("ENRICH_OFFLINE") == "1"
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
