"""Единый реестр членов СРО НОСТРОЙ (reestr.nostroy.ru) by INN → Evidence «член СРО» (#368, #188). Owner: SOURCES.

Protocol (the registry's own web app, checked 02.10.2026): `POST https://reestr.nostroy.ru/api/sro/all/member/list`
with `{"filters": {"inn": <ИНН>}, "page": 1, "pageCount": "20", "sortBy": {}}` →
`{"success": true, "data": {"data": [record, …], "count": N}}`. One record per SRO the company has ever belonged to;
`member_status.title` is «Является членом» or «Исключен» (`api/dictionaries/get`, `member_statuses`). A record of an SRO
with a `deactivate_message` (the SRO itself is excluded) is shown by the registry without a status — not a membership.
robots.txt: none (the web app answers the path), so the pacing of the other registries applies: MIN_INTERVAL_S between
any two requests, a refusal (403/429, an answer that is not the registry's) pauses the host for BLOCK_COOLDOWN_S.

What goes to the cache (src/sources/cache.py): `page_url(inn)` → the answer as flat `key: value` lines
(`query.inn`, `count`, `record.<i>.<key>`), JSON escaping removed, so every Evidence value occurs literally
(src/enrich/evidence.py);
`raw_url(inn)` → the raw answer for audit. The director's name is dropped from both: a natural person, not needed for
the fact (152-ФЗ, CLAUDE.md).

Answer (member, evidence):
- a record with this INN, «Является членом», SRO not deactivated → True, one Evidence per such record (SRO, its
  registry number, the date of entry);
- only «Исключен» records, all on the page → False, Evidence «был членом СРО …, исключён»;
- `count: 0` on a successful answer → False, «не найдена в реестре НОСТРОЙ на <дата>»;
- anything else (more records than one page, an unknown status, a deactivated SRO only) → None, never guessed.

The card reads the cache only (`enrich.add_open_data`); live requests come from the command line, one INN after
another: `uv run python -m src.sources.nostroy <ИНН> […]`. A pass over all candidates of works lots is a mass request
to the registry — confirmation №4 (CLAUDE.md), not done here. НОПРИЗ (designers, surveyors) is a separate registry.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from src.contracts import Evidence, inn_is_valid
from src.settings import get_settings
from src.sources.cache import CachedPage, SourceCache
from src.sources.pacing import HostPacer

BASE = "https://reestr.nostroy.ru"
API = f"{BASE}/api/sro/all/member/list"
SOURCE = "НОСТРОЙ: единый реестр членов СРО"
UA = "Mozilla/5.0 (compatible; RLTHack-Enrich/0.1; hackathon, cached, paced)"
MEMBER, EXCLUDED = "Является членом", "Исключен"
PAGE_COUNT = 20  # the web app's own page size
MIN_INTERVAL_S = 3.0
TIMEOUT_S = 30.0
MAX_AGE_S = 7 * 24 * 3600  # membership changes slowly; an older answer is asked again
BLOCK_COOLDOWN_S = 900.0
DOWN_COOLDOWN_S = 90.0
PACER = HostPacer(lambda: MIN_INTERVAL_S, BLOCK_COOLDOWN_S)
MSK = ZoneInfo("Europe/Moscow")
_KEYS = ("inn", "short_description", "full_description", "registration_number", "registry_registration_date",
         "ogrnip", "inventory_number")


class Blocked(RuntimeError):
    pass


@dataclass(frozen=True)
class SroResult:
    member: bool | None
    evidence: list[Evidence]


def page_url(inn: str) -> str:
    # the registry's search page; its filter lives in the web app's state, not in the address
    return f"{BASE}/sro/all/member/list#inn={inn}"


def raw_url(inn: str) -> str:
    return f"{API}?inn={inn}#raw"


def _clean(answer: dict[str, Any]) -> dict[str, Any]:
    """The answer without the director's name (a natural person)."""
    data = dict(answer.get("data") or {})
    data["data"] = [{k: v for k, v in r.items() if k != "director"} for r in data.get("data") or []]
    return {**answer, "data": data}


def flatten(answer: dict[str, Any], inn: str) -> str:
    data = answer.get("data") or {}
    lines = [f"query.inn: {inn}", f"success: {str(bool(answer.get('success'))).lower()}", f"count: {data.get('count')}"]
    for i, r in enumerate(data.get("data") or []):
        for k in _KEYS:
            if r.get(k) not in (None, ""):
                lines.append(f"record.{i}.{k}: {r[k]}")
        lines.append(f"record.{i}.member_status: {(r.get('member_status') or {}).get('title', '')}")
        sro = r.get("sro") or {}
        lines.append(f"record.{i}.sro: {sro.get('full_description', '')}")
        lines.append(f"record.{i}.sro_registration_number: {sro.get('registration_number', '')}")
        if sro.get("deactivate_message"):
            lines.append(f"record.{i}.sro_deactivated: {sro['deactivate_message']}")
    return "\n".join(lines) + "\n"


def parse(text: str) -> tuple[bool, int | None, list[dict[str, str]]]:
    """Inverse of flatten(): (success, count, records as {key: value})."""
    head: dict[str, str] = {}
    records: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            continue
        if key.startswith("record."):
            _, idx, field = key.split(".", 2)
            records.setdefault(int(idx), {})[field] = value
        else:
            head[key] = value
    count = head.get("count", "")
    rows = [records[i] for i in sorted(records)]
    return head.get("success") == "true", (int(count) if count.isdigit() else None), rows


def _post(client: httpx.Client, inn: str) -> dict[str, Any]:
    body = {"filters": {"inn": inn}, "page": 1, "pageCount": str(PAGE_COUNT), "sortBy": {}}
    with PACER.slot():
        try:
            r = client.post(API, json=body)
        except httpx.TransportError:  # timeout, refused connection, TLS: the host is down, not refusing
            PACER.block(DOWN_COOLDOWN_S)
            raise
    if r.status_code in (403, 429):
        PACER.block()
        raise Blocked(f"reestr.nostroy.ru ответил {r.status_code} — останавливаемся")
    r.raise_for_status()
    try:
        answer = r.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("data"), dict):
        PACER.block()
        raise Blocked("reestr.nostroy.ru вернул не ответ реестра — останавливаемся")
    return answer


def fetch(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None, *,
          offline: bool | None = None) -> CachedPage:
    """Cached answer for the INN. `offline=True` (None — by ENRICH_OFFLINE) — cache only, KeyError when not cached."""
    cache = cache or SourceCache()
    url = page_url(inn)
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
        raise Blocked("reestr.nostroy.ru недавно отказал или не ответил — пауза, запрос не отправлен")
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        answer = _clean(_post(client, inn))
    except Exception:
        if hit is not None:
            return hit
        raise
    finally:
        if own:
            client.close()
    cache.put(raw_url(inn), json.dumps(answer, ensure_ascii=False))
    return cache.put(url, flatten(answer, inn))


def _date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def answer(inn: str, page: CachedPage) -> SroResult:
    at = datetime.fromtimestamp(page.fetched_at, tz=UTC)
    day = at.astimezone(MSK).strftime("%d.%m.%Y")

    def ev(value: str, fact: str) -> Evidence:
        return Evidence(source=SOURCE, url=page.url, fetched_at=at, fact=fact, field="sro", value=value)

    success, count, records = parse(page.body)
    if not success or count is None:
        return SroResult(None, [])
    if count == 0 and not records:
        fact = f"Не найдена в реестре членов СРО НОСТРОЙ на {day} (поиск по ИНН {inn})"
        return SroResult(False, [ev("count: 0", fact)])
    own = [r for r in records if r.get("inn") == inn]
    current = [r for r in own if r.get("member_status") == MEMBER and not r.get("sro_deactivated")]
    if current:
        facts = []
        for r in current:
            since = _date(r.get("registry_registration_date", ""))
            facts.append(ev(r["sro_registration_number"], f"Член СРО «{r['sro']}» ({r['sro_registration_number']})"
                            + (f", в реестре с {since}" if since else "") + f" — реестр НОСТРОЙ на {day}"))
        return SroResult(True, facts)
    complete = count <= len(records)
    if own and complete and all(r.get("member_status") == EXCLUDED for r in own):
        r = own[0]
        fact = (f"Был членом СРО «{r['sro']}» ({r['sro_registration_number']}), статус «{EXCLUDED}» — "
                f"реестр НОСТРОЙ на {day}")
        return SroResult(False, [ev(r["sro_registration_number"], fact)])
    return SroResult(None, [])


def check(inn: str, cache: SourceCache | None = None, client: httpx.Client | None = None, *,
          offline: bool | None = None) -> SroResult:
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    return answer(inn, fetch(inn, cache, client, offline=offline))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Членство в СРО по реестру НОСТРОЙ (живой запрос, кеш, паузы) — #368")
    ap.add_argument("inns", nargs="+", help="ИНН, по одному запросу на каждый")
    args = ap.parse_args(argv)
    cache = SourceCache()
    for inn in args.inns:
        try:
            res = check(inn, cache, offline=False)
        except (Blocked, httpx.HTTPError, ValueError) as e:
            print(f"{inn}: не проверено — {e}", file=sys.stderr)
            continue
        state = {True: "член СРО", False: "не член СРО", None: "не определено"}[res.member]
        print(f"{inn}: {state}" + "".join(f"\n  {e.fact}" for e in res.evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
