"""«Источник → запросов → найдено → проверено → отсеяно → дата данных», one command over the source cache (#122).

    uv run python -m src.eval.source_table S-sources-v1      # -> eval/results/S-sources-v1.json

Reads ONLY the SQLite cache of src/sources/cache.py (read-only, no network): every row of the table
is something the cache holds, with the fetched_at of the answers (min / max) and, for the МСП registry,
the registry's own timestamp of the answer (`answer.dtQueryEnd`: when it answered, not when the registry was updated).
An empty cache of a source is a row of zeros
with a note, not an invented number.

Columns:
- requests: cached answers of the source (one per request);
- found: entities the answers contain (rmsp: distinct INN rows; pb / bo: cards with a row);
- verified: found AND INN checksum valid AND the INN literally on the cached page (the Evidence rule of
  src/enrich/evidence.py) AND the source says «действующее» (rmsp is_active, pb status, bo ACTIVE);
- dropped: the rest by reason; `new`: verified and absent from organizers' data (rmsp search only).
estore is the primary source of the organizers' data: a row «сверка» (pages counted, not parsed),
never a source of new companies (D-061).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.contracts import inn_is_valid
from src.enrich.evidence import value_in_page
from src.eval.run import RESULTS, git, tree_dirty
from src.eval.split import ROOT
from src.sources import bo_nalog, eis_rnp, pb_nalog, rmsp
from src.sources.cache import SourceCache

ROLE_NEW = "поиск новых"
ROLE_CHECK = "проверка"
ROLE_RECON = "сверка (первоисточник данных, не источник новых)"
ESTORE_BASE = "https://estore.gz-spb.ru"  # the client was removed from the code (D-061): only cached pages are counted


@dataclass
class Row:
    source: str
    role: str
    requests: int = 0
    found: int = 0
    verified: int = 0
    new: int | None = None
    dropped: Counter[str] = field(default_factory=Counter)
    fetched_min: float | None = None
    fetched_max: float | None = None
    data_date: str | None = None
    note: str = ""

    def seen(self, at: float) -> None:
        self.fetched_min = at if self.fetched_min is None else min(self.fetched_min, at)
        self.fetched_max = at if self.fetched_max is None else max(self.fetched_max, at)


@dataclass(frozen=True)
class Page:
    url: str
    body: str
    status: int
    fetched_at: float


def read_pages(cache_path: Path) -> list[Page]:
    """Every cached answer, stale ones too, without touching the cache (read-only connection)."""
    if not cache_path.exists():
        return []
    with closing(sqlite3.connect(f"file:{cache_path.as_posix()}?mode=ro", uri=True)) as c:
        try:
            rows = c.execute("SELECT url, body, status, fetched_at FROM pages ORDER BY url").fetchall()
        except sqlite3.DatabaseError:
            return []
    return [Page(*r) for r in rows]


def _inn_of(url: str) -> str:
    q = parse_qs(urlparse(url.replace("#queryAll=", "?queryAll=")).query)
    for key in ("queryAll", "query", "searchString", "supplier"):
        if key in q:
            return q[key][0]
    return ""


def _rmsp_search(pages: list[Page], known: set[str] | None) -> Row:
    row = Row("rmsp.nalog.ru — поиск по ОКВЭД и региону", ROLE_NEW)
    seen: dict[str, bool] = {}
    dates: list[str] = []
    for p in pages:
        row.requests += 1
        row.seen(p.fetched_at)
        if p.status != 200:
            row.dropped[f"ответ HTTP {p.status}"] += 1
            continue
        ans = rmsp.parse(p.body)
        if ans.meta.get("answer.dtQueryEnd"):
            dates.append(ans.meta["answer.dtQueryEnd"])
        for r in ans.rows:
            inn = r.get("inn", "")
            if inn in seen:
                continue
            row.found += 1
            if not inn_is_valid(inn):
                reason = "ИНН не проходит контрольные цифры"
            elif r.get("is_active") != "1":
                reason = "исключён из реестра МСП (не действующий)"
            elif not value_in_page("inn", inn, p.body):
                reason = "ИНН нет в тексте кешированной страницы"
            else:
                reason = ""
            seen[inn] = not reason
            if reason:
                row.dropped[reason] += 1
            else:
                row.verified += 1
    if known is not None:
        row.new = sum(1 for inn, ok in seen.items() if ok and inn not in known)
        row.dropped["уже есть в данных организаторов (проверено, но не «новая»)"] = row.verified - row.new
    row.data_date = max(dates) if dates else None
    return row


def _rmsp_quick(pages: list[Page]) -> Row:
    row = Row("rmsp.nalog.ru — по ИНН (статус МСП)", ROLE_CHECK)
    for p in pages:
        row.requests += 1
        row.seen(p.fetched_at)
        ans = rmsp.parse(p.body)
        inn = _inn_of(p.url)
        if not ans.rows:
            row.dropped["ИНН нет в реестре МСП"] += 1
            continue
        row.found += 1
        r = ans.rows[0]
        if not inn_is_valid(inn) or r.get("is_active") != "1" or not value_in_page("inn", inn, p.body):
            row.dropped["запись не действующая или ИНН не подтверждён страницей"] += 1
        else:
            row.verified += 1
    return row


def _pb(pages: list[Page]) -> Row:
    row = Row("pb.nalog.ru — карточка ФНС «Прозрачный бизнес»", ROLE_CHECK)
    for p in pages:
        row.requests += 1
        row.seen(p.fetched_at)
        f = pb_nalog.parse(p.body)
        inn = _inn_of(p.url)
        if p.status != 200 or not f:
            row.dropped["карточки нет"] += 1
            continue
        row.found += 1
        status = f.get("vyp.sulst_name_ex") or f.get("search.sulst_name_ex") or ""
        if not inn_is_valid(inn):
            row.dropped["ИНН не проходит контрольные цифры"] += 1
        elif not status.lower().startswith("действующ"):
            row.dropped[f"статус не «действующее»: {status or 'не указан'}"] += 1
        else:
            row.verified += 1
    return row


def _bo(pages: list[Page]) -> Row:
    row = Row("bo.nalog.gov.ru — ГИР БО", ROLE_CHECK)
    for p in pages:
        row.requests += 1
        row.seen(p.fetched_at)
        f = bo_nalog.parse(p.body)
        inn = _inn_of(p.url)
        if not f:
            row.dropped["организации нет в ГИР БО"] += 1
            continue
        row.found += 1
        code = f.get("statusCode", "")
        if not inn_is_valid(inn):
            row.dropped["ИНН не проходит контрольные цифры"] += 1
        elif code.upper() != "ACTIVE":
            row.dropped[f"статус не ACTIVE: {code or 'не указан'}"] += 1
        else:
            row.verified += 1
    return row


def _rnp(pages: list[Page]) -> Row:
    row = Row("zakupki.gov.ru — РНП ЕИС", ROLE_CHECK)
    for p in pages:
        row.requests += 1
        row.seen(p.fetched_at)
        inn = _inn_of(p.url)
        row.found += 1
        if not inn_is_valid(inn):
            row.dropped["ИНН не проходит контрольные цифры"] += 1
        elif eis_rnp.mentions(p.body, inn):
            row.dropped["ИНН в реестре недобросовестных поставщиков"] += 1
        elif eis_rnp.NO_RESULTS in p.body:
            row.verified += 1  # «not in the RNP» is the clean answer the page itself states
        else:
            row.dropped["страница не разобрана"] += 1
    return row


def _estore(pages: list[Page]) -> Row:
    """Reconciliation only: the registry of Электронный магазин is the organizers' own data (D-061). No client
    in the code any more, so cached pages (if an older run left some) are counted, not parsed."""
    row = Row("estore.gz-spb.ru — реестр контрактов ЭМ", ROLE_RECON)
    for p in pages:
        row.requests += 1
        row.seen(p.fetched_at)
    if row.requests:
        row.note = "страницы не разбираются: ЭМ — первоисточник данных организаторов, не источник новых (D-061)"
    return row


def build_table(pages: list[Page], known: set[str] | None = None) -> list[Row]:
    """`known`: INNs of organizers' data among the rmsp rows (None — data absent, «new» is not computed)."""
    def pick(prefix: str, *, skip_raw: bool = True) -> list[Page]:
        return [p for p in pages if p.url.startswith(prefix) and not (skip_raw and p.url.endswith("#raw"))]

    rows = [
        _rmsp_search(pick(f"{rmsp.BASE}/search.html?mode=extended"), known),
        _rmsp_quick(pick(f"{rmsp.BASE}/search.html?mode=quick")),
        _pb(pick(f"{pb_nalog.BASE}/search.html#queryAll=")),
        _bo(pick(f"{bo_nalog.BASE}/advanced-search/")),
        _rnp(pick(f"{eis_rnp.BASE}/epz/dishonestsupplier/")),
        _estore(pick(ESTORE_BASE)),
    ]
    for r in rows:
        if r.requests == 0:
            r.note = "кеш по источнику пуст — запросов не было"
    return rows


def rmsp_inns(pages: list[Page]) -> set[str]:
    out: set[str] = set()
    for p in pages:
        if p.url.startswith(f"{rmsp.BASE}/search.html?mode=extended") and not p.url.endswith("#raw"):
            out |= {r["inn"] for r in rmsp.parse(p.body).rows if r.get("inn")}
    return out


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds") if ts is not None else None


def to_json(row: Row) -> dict:
    return {"source": row.source, "role": row.role, "requests": row.requests, "found": row.found,
            "verified": row.verified, "new": row.new, "dropped": dict(row.dropped),
            "fetched_at_min": _iso(row.fetched_min), "fetched_at_max": _iso(row.fetched_max),
            "registry_data_date": row.data_date, "note": row.note}


def render(rows: list[Row]) -> str:
    lines = ["Источник | роль | запросов | найдено | проверено | новых | отсеяно (причины) | fetched_at min…max"]
    for r in rows:
        span = f"{_iso(r.fetched_min)} … {_iso(r.fetched_max)}" if r.fetched_min is not None else "нет данных"
        if r.data_date:
            span += f"; ответ реестра получен {r.data_date}"
        dropped = "; ".join(f"{k}: {v}" for k, v in r.dropped.items() if v) or "—"
        lines.append(f"{r.source} | {r.role} | {r.requests} | {r.found} | {r.verified} | "
                     f"{'—' if r.new is None else r.new} | {dropped} | {span}{' | ' + r.note if r.note else ''}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Таблица «источник → найдено → проверено» по кешу источников")
    ap.add_argument("exp")
    args = ap.parse_args(argv)
    cache = SourceCache()
    pages = read_pages(cache.path)
    known: set[str] | None = None
    try:
        from src.enrich.discover import known_inns
        known = known_inns(sorted(rmsp_inns(pages)), None)
    except Exception as e:  # data absent: «new» is not computed, never guessed
        print(f"! данные организаторов не прочитаны, «новых» не считаем: {e}", file=sys.stderr)
    rows = build_table(pages, known)
    rec = {
        "exp_id": args.exp,
        "kind": "source-table",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": tree_dirty(),
        "config": {"cache": cache.path.name, "pages_in_cache": len(pages),
                   "organizers_data_checked": known is not None},
        "sources": [to_json(r) for r in rows],
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(render(rows))
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
