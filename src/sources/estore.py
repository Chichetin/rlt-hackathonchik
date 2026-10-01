"""Электронный магазин СПб (estore.gz-spb.ru), реестр контрактов. Owner: SOURCES.

One public HTML table (research/SOURCES.md, checked 01.10): each row is a contract with the cell
«Наименование и ИНН участника» = `<name> (<INN>)`. Two uses:
- discover(okpd2, words): participants of ЭМ contracts for a product (item 4 of the task) —
  name and INN in one cell, so no name → INN matching is needed;
- lookup(inn): name and the number of ЭМ contracts by INN (item 5) — independent of ФНС
  (pb.nalog.ru asks for a captcha since 01.10 ≈ 14:50).

Only legal entities (10-digit INN) are returned: the registry also lists individuals (12 digits,
full name; ИП and самозанятые alike) and we can not tell them apart without ЕГРИП — 152-ФЗ,
research/SOURCES.md. An INN failing the checksum gives inn=None («ИНН не подтверждён»).

Every page goes through SourceCache as is: Evidence values (name, INN, contract subject,
«Всего: N») literally occur in it, src/enrich/evidence.py checks that. No robots.txt (404) and
pages take 2–15 s: one page (limit=50) per query, MIN_INTERVAL_S between requests.
`ENRICH_OFFLINE=1` or `DISCOVERY_OFFLINE=1` — cache only (KeyError if not cached).
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from src.contracts import Counterparty, Evidence, inn_is_valid
from src.sources.cache import CachedPage, SourceCache
from src.sources.tls import gov_ssl_context

BASE = "https://estore.gz-spb.ru"
REGISTRY = BASE + "/contracts/catalog/common/eshop"
SOURCE = "Электронный магазин СПб, реестр контрактов"
UA = "Mozilla/5.0 (compatible; RLTHack-Enrich/0.1; hackathon, cached, paced)"
MIN_INTERVAL_S = float(os.environ.get("ESTORE_MIN_INTERVAL_S", "2.0"))
TIMEOUT_S = 40.0
LIMIT = 50  # the largest page size the registry offers

_lock = threading.Lock()
_last_request = 0.0

_ROW = re.compile(r"<tr\s+id=\"rowId-\d+\".*?</tr>", re.S)
_CELL = re.compile(r"<td\s+class=\"row-(\w+)[^\"]*\"\s*>(.*?)</td>", re.S)
_TOTAL = re.compile(r"Всего:\s*(\d[\d\s]*)")
_PARTY = re.compile(r"^(?P<name>.*?)\s*\((?P<inn>\d{10}|\d{12})\)$")


class Blocked(RuntimeError):
    """403/429 or not a registry page: stop, never bypass (research/SOURCES.md)."""


@dataclass(frozen=True)
class Row:
    subject: str
    customer: str
    supplier: str  # the cell as is: `<name> (<INN>)`
    supplier_name: str
    supplier_inn: str | None  # digits from the cell; None if the cell has no INN
    price: str


@dataclass(frozen=True)
class Page:
    total: int | None  # «Всего: N» — all rows of the query, not only this page
    total_text: str | None
    rows: list[Row]


def supplier_url(inn: str) -> str:
    return f"{REGISTRY}?{urlencode({'supplier': inn, 'limit': LIMIT})}"


def search_url(okpd2: str | None, words: str | None) -> str:
    params = {k: v for k, v in (("productOKPD2", okpd2), ("productName", words)) if v}
    return f"{REGISTRY}?{urlencode({**params, 'limit': LIMIT})}"


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def parse(body: str) -> Page:
    rows = []
    for tr in _ROW.findall(body):
        cells = {k: _text(v) for k, v in _CELL.findall(tr)}
        supplier = cells.get("supplier", "")
        m = _PARTY.match(supplier)
        rows.append(Row(subject=cells.get("name", ""), customer=cells.get("customer", ""),
                        supplier=supplier, supplier_name=m["name"] if m else supplier,
                        supplier_inn=m["inn"] if m else None, price=cells.get("priceRur", "")))
    total = _TOTAL.search(re.sub(r"<[^>]+>", " ", body))
    total_text = _text(total.group(0)) if total else None
    return Page(total=int(re.sub(r"\D", "", total[1])) if total else None, total_text=total_text, rows=rows)


def _get(client: httpx.Client, url: str) -> tuple[int, str]:
    global _last_request
    with _lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            r = client.get(url)
        finally:
            _last_request = time.monotonic()
    if r.status_code in (403, 429):
        raise Blocked(f"estore.gz-spb.ru ответил {r.status_code} — останавливаемся (защиту не обходим)")
    r.raise_for_status()
    if "mte-grid-table" not in r.text:
        raise Blocked("estore.gz-spb.ru вернул не страницу реестра — останавливаемся")
    return r.status_code, r.text


def fetch(url: str, cache: SourceCache | None = None, client: httpx.Client | None = None) -> CachedPage:
    cache = cache or SourceCache()
    if os.environ.get("ENRICH_OFFLINE") == "1" or os.environ.get("DISCOVERY_OFFLINE") == "1":
        page = cache.get(url, allow_stale=True)
        if page is None:
            raise KeyError(url)
        return page
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, verify=gov_ssl_context(), headers={"User-Agent": UA})
    try:
        return cache.fetch(url, lambda u: _get(client, u))
    finally:
        if own:
            client.close()


def _evidence(page: CachedPage, field: str, value: str, fact: str) -> Evidence:
    return Evidence(source=SOURCE, url=page.url, fetched_at=datetime.fromtimestamp(page.fetched_at, tz=UTC),
                    fact=fact, field=field, value=value)


def discover(okpd2: str | None, words: str | None, cache: SourceCache | None = None,
             client: httpx.Client | None = None) -> list[Counterparty]:
    """Legal entities that signed ЭМ contracts for the product, in registry order (one page)."""
    page = fetch(search_url(okpd2, words), cache, client)
    out: dict[str, Counterparty] = {}
    for row in parse(page.body).rows:
        digits = row.supplier_inn
        if digits is None or len(digits) != 10 or digits in out:
            continue  # no INN, or an individual (152-ФЗ), or already seen
        inn = digits if inn_is_valid(digits) else None
        ev = [_evidence(page, "name", row.supplier_name, f"Участник контракта в Эл. магазине: {row.supplier_name}"),
              _evidence(page, "contract", row.subject,
                        f"Контракт в Эл. магазине: «{row.subject}», цена {row.price} руб., заказчик {row.customer}")]
        if inn is not None:
            ev.insert(0, _evidence(page, "inn", inn, f"ИНН {inn} в реестре контрактов Эл. магазина"))
        out[digits] = Counterparty(inn=inn, name=row.supplier_name, evidence=ev)
    return list(out.values())


def lookup(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None) -> Counterparty | None:
    """Name and ЭМ contract count by INN; None for individuals or when the registry has no such INN."""
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    if len(inn) != 10:
        return None  # individuals: not from this registry (152-ФЗ)
    page = fetch(supplier_url(inn), cache, client)
    parsed = parse(page.body)
    rows = [r for r in parsed.rows if r.supplier_inn == inn]
    if not rows:
        return None
    name = rows[0].supplier_name
    ev = [_evidence(page, "inn", inn, f"ИНН {inn} в реестре контрактов Эл. магазина"),
          _evidence(page, "name", name, f"Наименование в реестре контрактов Эл. магазина: {name}")]
    if parsed.total_text and len(rows) == len(parsed.rows):  # every row is this INN → the total is its count
        ev.append(_evidence(page, "contracts", parsed.total_text,
                            f"Контрактов в реестре Эл. магазина СПб: {parsed.total}"))
    return Counterparty(inn=inn, name=name, evidence=ev)
