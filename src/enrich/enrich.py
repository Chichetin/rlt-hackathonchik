"""enrich(inn) -> Counterparty with Evidence for every fact. Owner: SOURCES.

Source: ФНС «Прозрачный бизнес» (src/sources/pb_nalog.py) through the disk cache, so the demo
works on cache without internet. Every fact is an Evidence whose `value` literally occurs in the
cached card text (src/enrich/evidence.py checks it; the API shows only what passes).
Organizers' data stats (`Counterparty.stats`) come from search(), not from here.

`ENRICH_OFFLINE=1` — cache only, no network (KeyError for an INN that is not cached).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from src.contracts import Counterparty, Evidence, inn_is_valid
from src.sources import pb_nalog
from src.sources.cache import CachedPage, SourceCache

log = logging.getLogger(__name__)

MSP_CATEGORY = {"1": "микропредприятие", "2": "малое предприятие", "3": "среднее предприятие"}


def _region(kpp_or_inn: str) -> str | None:
    return {"78": "Санкт-Петербург", "47": "Ленинградская область"}.get(kpp_or_inn[:2])


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

    category = f.get("vyp.rsmpcategory")
    if category in MSP_CATEGORY:  # "0" — not in the МСП registry (e.g. ПАО «Ростелеком»)
        is_msp: bool | None = True
        cat = MSP_CATEGORY[category]
        since = f.get("vyp.rsmpdate", "").split(" ")[0]
        add("msp", f.get("vyp.rsmpdate"), f"В реестре МСП с {since}: {cat}")
    else:
        is_msp = False
        add("inn", inn, "В карточке ФНС нет категории МСП — не субъект МСП")

    return Counterparty(
        inn=inn,
        kpp=kpp,
        name=name or f"ИНН {inn}",
        region=_region(kpp or inn),
        status=status,
        is_msp=is_msp,
        okved=[okved_code] if okved_code else [],
        evidence=ev,
    )


def enrich(inn: str, cache: SourceCache | None = None) -> Counterparty:
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    cache = cache or SourceCache()
    if os.environ.get("ENRICH_OFFLINE") == "1":
        page = cache.get(pb_nalog.card_url(inn), allow_stale=True)
        if page is None:
            raise KeyError(inn)
    else:
        page = pb_nalog.fetch_card(inn, cache)
    return from_card(inn, page)
