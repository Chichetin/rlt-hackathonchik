"""Армения: государственный регистр юридических лиц (e-register.moj.am) by the tax number → a company of the EAEU
(#533, continues #200). Owner: SOURCES.

Protocol (the register's public search, checked 02.10.2026; robots.txt: `Allow: /`), two requests per company:
1. `GET https://e-register.moj.am/hy/search/companies?query=<ՀՎՀՀ>` → the result list: `<a href="/hy/companies/<id>">`
   with the company's name in Armenian (the Russian and English pages leave the name empty);
2. `GET https://e-register.moj.am/ru/companies/<id>` → the card with Russian labels: «Статус компании», «Номер
   регистрации», «Дата регистрации», «ИНН» (the tax number, ՀՎՀՀ, 8 digits), «Уникальный идентификатор», «Адрес».
A search that returns anything but ONE company, or a card whose «ИНН» is another number, is not a company: nothing is
guessed. MIN_INTERVAL_S between two requests, a refusal (403/429) pauses the host for BLOCK_COOLDOWN_S. One company at
a time, never a crawl; a pass over many numbers is a mass request — confirmation №4 (CLAUDE.md).

The register answers WHO the company is (name, address, registration, status), not WHAT it does: no activity codes,
so the role and the product are not established by it. The declarations of beneficial owners linked from the card
are natural persons' data: not read, not stored.

What goes to the cache (src/sources/cache.py): `search_url(tin)` and `card_url(id)` → flat `am.<ՀՎՀՀ>.<key>: <value>`
lines (the values as on the page, whitespace collapsed), so every Evidence value occurs literally
(src/enrich/evidence.py); the page text itself goes under `…#raw` for audit. A company of Armenia has no Russian INN:
`Counterparty(inn=None, country="AM", foreign_id="AM:<ՀՎՀՀ>")`.

    uv run python -m src.sources.am_register 02226764          # live requests, cached
    uv run python -m src.sources.am_register seed <directory>   # recorded answers `<ՀՎՀՀ>.json` → cache
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src.contracts import Counterparty, Evidence
from src.settings import get_settings
from src.sources.cache import CachedPage, SourceCache
from src.sources.pacing import HostPacer

BASE = "https://e-register.moj.am"
SOURCE = "Министерство юстиции Республики Армения: государственный регистр юридических лиц"
COUNTRY = "AM"
UA = "Mozilla/5.0 (compatible; RLTHack-Enrich/0.1; hackathon, cached, paced)"
MIN_INTERVAL_S = 3.0
TIMEOUT_S = 25.0
MAX_AGE_S = 7 * 24 * 3600
BLOCK_COOLDOWN_S = 900.0
DOWN_COOLDOWN_S = 90.0
PACER = HostPacer(lambda: MIN_INTERVAL_S, BLOCK_COOLDOWN_S)
# the card's sentence of a company that is neither in liquidation nor stopped (the Russian page)
ACTIVE = ("В едином государственном реестре отсутствуют сведения о нахождении в процессе ликвидации или прекращении "
          "деятельности (существования).")
LABELS = {"Статус компании": "status", "Номер регистрации": "reg_number", "Дата регистрации": "registered",
          "ИНН": "tax_id", "Уникальный идентификатор": "unique_id", "Адрес": "address"}
_TIN = re.compile(r"\d{8}", re.ASCII)
_RESULT = re.compile(r'<a\s+href="/hy/companies/(\d+)"\s*>\s*<h4>(.*?)</h4>', re.S)
_PAIR = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)
_TAG = re.compile(r"<[^>]+>")


class Blocked(RuntimeError):
    pass


def tin_is_valid(tin: str) -> bool:
    """The shape of the Armenian tax number (ՀՎՀՀ): 8 digits. No control digit is checked — the register decides."""
    return _TIN.fullmatch(tin) is not None


def foreign_id(tin: str) -> str:
    return f"{COUNTRY}:{tin}"


def search_url(tin: str) -> str:
    return f"{BASE}/hy/search/companies?query={tin}"


def card_url(company_id: str) -> str:
    return f"{BASE}/ru/companies/{company_id}"


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", fragment)).split())


def parse_search(page: str) -> list[tuple[str, str]]:
    """(register id, name) of every company in the result list."""
    return [(cid, _text(name)) for cid, name in _RESULT.findall(page)]


def parse_card(page: str) -> dict[str, str]:
    """The card's label → value pairs we use (LABELS); the declarations block is skipped."""
    out: dict[str, str] = {}
    for label, value in _PAIR.findall(page):
        key = LABELS.get(_text(label))
        if key and key not in out and _text(value):
            out[key] = _text(value)
    return out


def flatten_search(tin: str, found: list[tuple[str, str]]) -> str:
    lines = [f"query.tax_id: {tin}", f"am.{tin}.results: {len(found)}"]
    if len(found) == 1:
        lines += [f"am.{tin}.id: {found[0][0]}", f"am.{tin}.name: {found[0][1]}"]
    return "\n".join(lines) + "\n"


def flatten_card(tin: str, card: dict[str, str]) -> str:
    return "\n".join(f"am.{tin}.{key}: {card[key]}" for key in LABELS.values() if card.get(key)) + "\n"


def parse(text: str, tin: str) -> dict[str, str]:
    """Inverse of the flatten functions: the lines of THIS tax number as {key: value}."""
    head = f"am.{tin}."
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.startswith(head):
            out[key[len(head):]] = value
    return out


def _get(client: httpx.Client, url: str) -> str:
    with PACER.slot():
        try:
            r = client.get(url)
        except httpx.TransportError:  # timeout, refused connection, TLS: the host is down, not refusing
            PACER.block(DOWN_COOLDOWN_S)
            raise
    if r.status_code in (403, 429):
        PACER.block()
        raise Blocked(f"e-register.moj.am ответил {r.status_code} — останавливаемся")
    if r.status_code >= 500:
        PACER.block(DOWN_COOLDOWN_S)
    r.raise_for_status()
    return r.text


def cached(tin: str, cache: SourceCache) -> tuple[CachedPage, CachedPage | None] | None:
    """The cached search page and — when the search named one company — its cached card; None: not cached."""
    found = cache.get(search_url(tin), allow_stale=True)
    if found is None:
        return None
    company_id = parse(found.body, tin).get("id")
    if company_id is None:
        return found, None
    card = cache.get(card_url(company_id), allow_stale=True)
    return None if card is None else (found, card)


def fetch(tin: str, cache: SourceCache | None = None, client: httpx.Client | None = None, *,
          offline: bool | None = None) -> tuple[CachedPage, CachedPage | None]:
    """Cached (search page, card) for the tax number; the card is None when the register names no single company.
    `offline=True` (None — by ENRICH_OFFLINE) — cache only, KeyError when not cached."""
    if not tin_is_valid(tin):
        raise ValueError(f"налоговый номер Армении {tin!r} — не 8 цифр, запрос не отправлен")
    cache = cache or SourceCache()
    hit = cached(tin, cache)
    if get_settings().enrich_offline if offline is None else offline:
        if hit is None:
            raise KeyError(search_url(tin))
        return hit
    if hit is not None and time.time() - hit[0].fetched_at <= MAX_AGE_S:
        return hit
    if PACER.blocked():
        if hit is not None:
            return hit  # a stale answer beats none; its date goes with the fact
        raise Blocked("e-register.moj.am недавно отказал или не ответил — пауза, запрос не отправлен")
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": UA}, follow_redirects=False)
    try:
        page = _get(client, search_url(tin))
        found = parse_search(page)
        card_page, card = None, {}
        if len(found) == 1:
            card_page = _get(client, card_url(found[0][0]))
            card = parse_card(card_page)
            if card.get("tax_id") != tin:  # the search matched another field: not this company
                found, card_page, card = [], None, {}
    except Exception:
        if hit is not None:
            return hit
        raise
    finally:
        if own:
            client.close()
    cache.put(f"{search_url(tin)}#raw", _text(page))
    stored = cache.put(search_url(tin), flatten_search(tin, found))
    if card_page is None:
        return stored, None
    cache.put(f"{card_url(found[0][0])}#raw", _text(card_page))
    return stored, cache.put(card_url(found[0][0]), flatten_card(tin, card))


def record(tin: str, pages: tuple[CachedPage, CachedPage | None]) -> dict | None:
    """What `seed` reads back: the fields of a found company with the time of the request; None — not found."""
    found, card = pages
    if card is None:
        return None
    head = parse(found.body, tin)
    return {"recorded_at": int(card.fetched_at), "search_url": search_url(tin), "card_url": card.url,
            "id": head["id"], "name": head["name"], "card": parse(card.body, tin)}


def seed(directory: Path, cache: SourceCache | None = None, *, overwrite: bool = False) -> int:
    """Recorded answers `<ՀՎՀՀ>.json` (record()) → cache, fetched_at = the time of the recording. A company already in
    the cache is kept unless `overwrite`."""
    cache = cache or SourceCache()
    n = 0
    for path in sorted(directory.glob("*.json")):
        tin = path.stem
        if not tin_is_valid(tin) or (not overwrite and cached(tin, cache) is not None):
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        at = float(rec["recorded_at"])
        cache.put(search_url(tin), flatten_search(tin, [(str(rec["id"]), rec["name"])]), fetched_at=at)
        cache.put(card_url(str(rec["id"])), flatten_card(tin, rec["card"]), fetched_at=at)
        n += 1
    return n


def counterparty(tin: str, pages: tuple[CachedPage, CachedPage | None]) -> Counterparty | None:
    """The company of the cached answer: identity, name, address and status, every fact an Evidence whose value is
    a line value of its page. None: the register names no single company with this tax number. No role is set."""
    found, card = pages
    if card is None:
        return None
    head, row = parse(found.body, tin), parse(card.body, tin)
    name = head.get("name", "")
    if row.get("tax_id") != tin or not name.strip('"«» '):
        return None
    at = datetime.fromtimestamp(card.fetched_at, tz=UTC)
    fid = foreign_id(tin)

    def ev(page: CachedPage, field: str, value: str, fact: str) -> Evidence:
        return Evidence(source=SOURCE, url=page.url, fetched_at=datetime.fromtimestamp(page.fetched_at, tz=UTC),
                        fact=fact, field=field, value=value)

    since = f", зарегистрирована {row['registered']}" if row.get("registered") else ""
    evidence = [
        ev(card, "foreign_id", fid, f"Армения, ИНН {tin} — состоит в государственном регистре юридических лиц "
                                    f"Республики Армения{since}"),
        ev(found, "name", name, f"Наименование по регистру юридических лиц Армении: {name}"),
    ]
    contacts: dict[str, str] = {}
    if row.get("address"):
        contacts["address"] = row["address"]
        evidence.append(ev(card, "address", row["address"], f"Адрес по регистру юридических лиц Армении: "
                                                             f"{row['address']}"))
    status = None
    if row.get("status") == ACTIVE:
        status = "Действующая"
        evidence.append(ev(card, "status", row["status"],
                           f"Статус по регистру юридических лиц Армении на {at.strftime('%d.%m.%Y')}: сведений о "
                           "ликвидации или прекращении деятельности нет"))
    elif row.get("status"):
        status = row["status"]
        evidence.append(ev(card, "status", row["status"],
                           f"Статус по регистру юридических лиц Армении на {at.strftime('%d.%m.%Y')}: {status}"))
    return Counterparty(inn=None, country=COUNTRY, foreign_id=fid, name=name, region="Армения", status=status,
                        contacts=contacts, evidence=evidence)


def is_active(tin: str, pages: tuple[CachedPage, CachedPage | None]) -> bool:
    return pages[1] is not None and parse(pages[1].body, tin).get("status") == ACTIVE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Регистр юридических лиц Армении: компания по налоговому номеру")
    ap.add_argument("args", nargs="+", help="ՀՎՀՀ […] — живой запрос с кешем; seed <каталог> — записанные ответы в кеш")
    ap.add_argument("--record", type=Path, default=None, help="каталог: записать ответ найденной компании <ՀՎՀՀ>.json")
    ns = ap.parse_args(argv)
    args = ns.args
    if args[0] == "seed":
        if len(args) != 2:
            ap.error("seed <каталог>")
        print(f"am_register: в кеш загружено ответов: {seed(Path(args[1]), overwrite=True)}")
        return 0
    cache, code = SourceCache(), 0
    for tin in args:
        try:
            pages = fetch(tin, cache, offline=False)
        except (ValueError, Blocked, httpx.HTTPError) as e:
            print(f"{tin}: {e}", file=sys.stderr)
            code = 1
            continue
        cp = counterparty(tin, pages)
        if cp is None:
            print(f"{tin}: в регистре юридических лиц Армении одна компания с таким номером не найдена")
            continue
        print(f"{cp.foreign_id}  {cp.name}  | {cp.status} | {cp.contacts.get('address', '')}")
        if ns.record is not None:
            ns.record.mkdir(parents=True, exist_ok=True)
            (ns.record / f"{tin}.json").write_text(
                json.dumps(record(tin, pages), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
