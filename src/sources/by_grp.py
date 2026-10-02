"""Беларусь: Государственный реестр плательщиков МНС (grp.nalog.gov.by) by УНП → a company of the EAEU (#200).
Owner: SOURCES.

Protocol (the public API of the registry's own search page https://grp.nalog.gov.by/grp, checked 02.10.2026):
`GET https://grp.nalog.gov.by/api/grp-public/data?unp=<УНП>&charset=UTF-8&type=json` →
`{"row": {"vunp", "vnaimp" (full name), "vnaimk" (short name), "vpadres" (address), "dreg" (registered),
"nmns"/"vmns" (tax office), "ckodsost"/"vkods" (status: 1 / «Действующий»), "dlikv"/"vlikv" (liquidation)}}`;
an unknown УНП → HTTP 400 `{"status": 400, "message": "Нет данных по запросу"}`. No robots.txt on the host (the path
answers with the search page), so the pacing of the other registries applies: MIN_INTERVAL_S between two requests,
a refusal (403/429, an answer that is not the registry's) pauses the host for BLOCK_COOLDOWN_S. One request per УНП,
never a crawl: a pass over many identifiers is a mass request — confirmation №4 (CLAUDE.md).

The registry answers WHO the company is (name, address, status), not WHAT it does: it holds no activity codes, so
the role and the product are not established by it (the Eurasian registry of industrial goods lives in ГИСП, which
refused the venue's address on 02.10 — research/SOURCES.md).

What goes to the cache (src/sources/cache.py): `page_url(unp)` (the API address itself — it opens in a browser) →
the answer as flat `grp.<УНП>.<key>: <value>` lines, JSON escaping removed, so every Evidence value occurs literally
(src/enrich/evidence.py); `raw_url(unp)` → the raw answer for audit. «Not found» is cached too (`grp.<УНП>.found:
false`): the same identifier is not asked twice.

A company of Belarus has no Russian INN: `Counterparty(inn=None, country="BY", foreign_id="BY:<УНП>")`
(src/contracts.py). The УНП is checked by its control digit before any request; a bad one is never sent.

    uv run python -m src.sources.by_grp 100063724            # one live request, cached
    uv run python -m src.sources.by_grp seed <directory>     # recorded answers `<УНП>.json` → cache
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from src.contracts import Counterparty, Evidence
from src.settings import get_settings
from src.sources.cache import CachedPage, SourceCache
from src.sources.pacing import HostPacer

BASE = "https://grp.nalog.gov.by"
API = f"{BASE}/api/grp-public/data"
SOURCE = "МНС Республики Беларусь: Государственный реестр плательщиков"
COUNTRY = "BY"
UA = "Mozilla/5.0 (compatible; RLTHack-Enrich/0.1; hackathon, cached, paced)"
MIN_INTERVAL_S = 3.0
TIMEOUT_S = 20.0
MAX_AGE_S = 7 * 24 * 3600
BLOCK_COOLDOWN_S = 900.0
DOWN_COOLDOWN_S = 90.0
PACER = HostPacer(lambda: MIN_INTERVAL_S, BLOCK_COOLDOWN_S)
ACTIVE_CODE = "1"  # ckodsost of «Действующий»
NOT_FOUND = "Нет данных по запросу"
_UNP = re.compile(r"\d{9}", re.ASCII)
_UNP_W = (29, 23, 19, 17, 13, 7, 5, 3)
# answer key → page key; the rest of the answer is not used
KEYS = {"vunp": "unp", "vnaimp": "name", "vnaimk": "short_name", "vpadres": "address", "dreg": "registered",
        "vmns": "tax_office", "ckodsost": "status_code", "vkods": "status", "dlikv": "liquidated",
        "vlikv": "liquidation_reason"}


class Blocked(RuntimeError):
    pass


def unp_is_valid(unp: str) -> bool:
    """Control digit of the УНП of a legal entity (9 digits): the weighted sum of the first eight mod 11 is the ninth.
    An individual's УНП (letters in it) is not handled: about natural persons only state registries (CLAUDE.md)."""
    if not _UNP.fullmatch(unp):
        return False
    d = [int(c) for c in unp]
    return sum(a * w for a, w in zip(d, _UNP_W, strict=False)) % 11 == d[8]


def foreign_id(unp: str) -> str:
    return f"{COUNTRY}:{unp}"


def page_url(unp: str) -> str:
    return f"{API}?unp={unp}&charset=UTF-8&type=json"


def raw_url(unp: str) -> str:
    return f"{page_url(unp)}#raw"


def _text(v: Any) -> str:
    return " ".join(str(v).split()) if v is not None else ""


def flatten(answer: dict[str, Any], unp: str) -> str:
    row = answer.get("row")
    lines = [f"query.unp: {unp}", f"grp.{unp}.found: {'true' if isinstance(row, dict) else 'false'}"]
    if isinstance(row, dict):
        lines += [f"grp.{unp}.{key}: {_text(row[src])}" for src, key in KEYS.items() if _text(row.get(src))]
    return "\n".join(lines) + "\n"


def parse(text: str, unp: str) -> dict[str, str]:
    """Inverse of flatten(): the lines of THIS УНП as {key: value}."""
    head = f"grp.{unp}."
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.startswith(head):
            out[key[len(head):]] = value
    return out


def _get(client: httpx.Client, unp: str) -> dict[str, Any]:
    with PACER.slot():
        try:
            r = client.get(API, params={"unp": unp, "charset": "UTF-8", "type": "json"})
        except httpx.TransportError:  # timeout, refused connection, TLS: the host is down, not refusing
            PACER.block(DOWN_COOLDOWN_S)
            raise
    if r.status_code in (403, 429):
        PACER.block()
        raise Blocked(f"grp.nalog.gov.by ответил {r.status_code} — останавливаемся")
    try:
        answer = r.json()
    except ValueError:
        answer = None
    if r.status_code == 400 and isinstance(answer, dict) and answer.get("message") == NOT_FOUND:
        return {"row": None}  # the registry's own «not found»
    if r.status_code >= 500:  # the host is failing: leave it alone as after a network error
        PACER.block(DOWN_COOLDOWN_S)
    r.raise_for_status()
    if not isinstance(answer, dict) or not isinstance(answer.get("row"), dict) \
            or _text(answer["row"].get("vunp")) != unp:
        PACER.block()
        raise Blocked("grp.nalog.gov.by вернул не ответ реестра по этому УНП — останавливаемся")
    return answer


def fetch(unp: str, cache: SourceCache | None = None, client: httpx.Client | None = None, *,
          offline: bool | None = None) -> CachedPage:
    """Cached answer for the УНП. `offline=True` (None — by ENRICH_OFFLINE) — cache only, KeyError when not cached."""
    if not unp_is_valid(unp):
        raise ValueError(f"УНП {unp!r} не проходит контрольную цифру — запрос не отправлен")
    cache = cache or SourceCache()
    url = page_url(unp)
    hit = cache.get(url, allow_stale=True)
    if get_settings().enrich_offline if offline is None else offline:
        if hit is None:
            raise KeyError(url)
        return hit
    if hit is not None and time.time() - hit.fetched_at <= MAX_AGE_S:
        return hit
    if PACER.blocked():
        if hit is not None:
            return hit  # a stale answer beats none; its date goes with the fact
        raise Blocked("grp.nalog.gov.by недавно отказал или не ответил — пауза, запрос не отправлен")
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        answer = _get(client, unp)
    except Exception:
        if hit is not None:
            return hit
        raise
    finally:
        if own:
            client.close()
    cache.put(raw_url(unp), json.dumps(answer, ensure_ascii=False))
    return cache.put(url, flatten(answer, unp))


def seed(directory: Path, cache: SourceCache | None = None, *, overwrite: bool = False) -> int:
    """Recorded answers `<УНП>.json` (`{"recorded_at": <unix time>, "answer": <the registry's answer>}`) → cache,
    fetched_at = the time of the recording. A page already in the cache is kept unless `overwrite`."""
    cache = cache or SourceCache()
    n = 0
    for path in sorted(directory.glob("*.json")):
        unp = path.stem
        if not unp_is_valid(unp) or (not overwrite and cache.page_text(page_url(unp)) is not None):
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        at = float(rec["recorded_at"])
        cache.put(raw_url(unp), json.dumps(rec["answer"], ensure_ascii=False), fetched_at=at)
        cache.put(page_url(unp), flatten(rec["answer"], unp), fetched_at=at)
        n += 1
    return n


def _ru(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def counterparty(unp: str, page: CachedPage) -> Counterparty | None:
    """The company of the cached answer: identity, name, address and status, every fact an Evidence whose value is
    a line value of the page. None: the registry has no such УНП, or the page is of another УНП. The role is not
    set: the registry holds no activity codes."""
    row = parse(page.body, unp)
    if row.get("found") != "true" or row.get("unp") != unp or not (row.get("short_name") or row.get("name")):
        return None
    at = datetime.fromtimestamp(page.fetched_at, tz=UTC)
    fid = foreign_id(unp)

    def ev(field: str, value: str, fact: str) -> Evidence:
        return Evidence(source=SOURCE, url=page.url, fetched_at=at, fact=fact, field=field, value=value)

    name = row.get("short_name") or row["name"]
    since = _ru(row.get("registered", ""))
    evidence = [
        ev("foreign_id", fid, f"Беларусь, УНП {unp} — состоит в Государственном реестре плательщиков МНС Республики "
                              "Беларусь" + (f" с {since}" if since else "")),
        ev("name", name, f"Наименование по реестру плательщиков Беларуси: {name}"),
    ]
    contacts: dict[str, str] = {}
    if row.get("address"):
        contacts["address"] = row["address"]
        evidence.append(ev("address", row["address"], f"Адрес по реестру плательщиков Беларуси: {row['address']}"))
    status = None
    if row.get("status"):
        status = row["status"]
        day = at.strftime("%d.%m.%Y")
        evidence.append(ev("status", f"grp.{unp}.status: {status}",
                           f"Статус по реестру плательщиков Беларуси на {day}: {status}"))
    return Counterparty(inn=None, country=COUNTRY, foreign_id=fid, name=name, region="Беларусь", status=status,
                        contacts=contacts, evidence=evidence)


def is_active(cp: Counterparty, page: CachedPage) -> bool:
    return cp.foreign_id is not None and parse(page.body, cp.foreign_id.partition(":")[2]).get(
        "status_code") == ACTIVE_CODE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Реестр плательщиков Беларуси (МНС): компания по УНП")
    ap.add_argument("args", nargs="+", help="УНП […] — живой запрос с кешем; seed <каталог> — записанные ответы в кеш")
    ap.add_argument("--record", type=Path, default=None, help="каталог: записать ответ найденной компании <УНП>.json")
    ns = ap.parse_args(argv)
    args = ns.args
    if args[0] == "seed":
        if len(args) != 2:
            ap.error("seed <каталог>")
        print(f"by_grp: в кеш загружено ответов: {seed(Path(args[1]), overwrite=True)}")
        return 0
    cache, code = SourceCache(), 0
    for unp in args:
        try:
            page = fetch(unp, cache, offline=False)
        except (ValueError, Blocked, httpx.HTTPError) as e:
            print(f"{unp}: {e}", file=sys.stderr)
            code = 1
            continue
        cp = counterparty(unp, page)
        if cp is None:
            print(f"{unp}: в реестре плательщиков Беларуси не найден")
            continue
        print(f"{cp.foreign_id}  {cp.name}  | {cp.status} | {cp.contacts.get('address', '')}")
        raw = cache.page_text(raw_url(unp))
        if ns.record is not None and raw is not None:
            ns.record.mkdir(parents=True, exist_ok=True)
            rec = {"recorded_at": int(page.fetched_at), "url": page_url(unp), "answer": json.loads(raw)}
            (ns.record / f"{unp}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n",
                                                   encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
