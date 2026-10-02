"""Web leads: suppliers found by a web search outside the МСП registry (#108). Owner: SOURCES.

`configs/leads/*.csv` (`;`, UTF-8): one row per company a web search returned for an OKPD2 class —
name, INN as the search result showed it, site, the role the page claims, the result URL and the query.
A lead is a CANDIDATE, not a fact: nothing of the row is shown as a fact. The block «Новые» takes a
lead only when its INN passes the checksum, is absent from organizers' data and its ФНС card is in the
cache (src/enrich/discover.py); name, status, OKVED, role and region then come from that card with
its own Evidence. Without a cached card the INN goes to the enrichment queue. A row without an INN
(the search did not show one) stays a lead for a human: «ИНН не подтверждён», never a guessed INN.

Statistics and the warm-up of the leads' ФНС cards — src/enrich/web_leads_warm.py (a source does not
orchestrate enrichment, #294, D-106).
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from src.contracts import inn_is_valid

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
LEADS_DIR = ROOT / "configs" / "leads"
SOURCE = "веб-поиск"
COLUMNS = ("okpd2_class", "inn", "name", "site", "role_claim", "region", "source_url", "search_query", "found_at")


@dataclass(frozen=True)
class Lead:
    okpd2_class: str
    inn: str | None  # None: the search showed no INN, or it failed the checksum
    name: str
    site: str | None
    role_claim: str  # what the page says (manufacturer / distributor / dealer / unknown) — never shown as a fact
    region: str | None
    source_url: str
    search_query: str
    found_at: date


def parse(path: Path) -> list[Lead]:
    """Rows of one leads file; a bad INN becomes None (with a warning), a row without a URL is dropped."""
    out: list[Lead] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError(f"{path.name}: колонки {reader.fieldnames}, ожидались {list(COLUMNS)}")
        for n, row in enumerate(reader, start=2):
            inn = (row["inn"] or "").strip() or None
            if inn is not None and not inn_is_valid(inn):
                log.warning("%s:%d: ИНН %s не проходит контрольную сумму — лид без ИНН", path.name, n, inn)
                inn = None
            url = (row["source_url"] or "").strip()
            if not url.startswith(("http://", "https://")):
                log.warning("%s:%d: нет ссылки на источник — строка пропущена", path.name, n)
                continue
            out.append(Lead(
                okpd2_class=row["okpd2_class"].strip(), inn=inn, name=row["name"].strip(),
                site=(row["site"] or "").strip() or None, role_claim=(row["role_claim"] or "unknown").strip(),
                region=(row["region"] or "").strip() or None, source_url=url,
                search_query=(row["search_query"] or "").strip(), found_at=date.fromisoformat(row["found_at"]),
            ))
    return out


_LOADED: dict[tuple[str, ...], list[Lead]] = {}  # file versions (path:mtime) → leads, read once per version


def load(directory: Path | None = None) -> list[Lead]:
    """Every lead of `<directory>/*.csv` (default configs/leads); no directory → []."""
    directory = directory or LEADS_DIR
    files = sorted(directory.glob("*.csv")) if directory.is_dir() else []
    key = tuple(f"{p}:{p.stat().st_mtime_ns}" for p in files)
    if key not in _LOADED:
        _LOADED.clear()
        _LOADED[key] = [lead for p in files for lead in parse(p)]
    return _LOADED[key]


def for_classes(classes: list[str], leads: list[Lead]) -> dict[str, Lead]:
    """INN → the first lead of these OKPD2 classes (in the order of `classes`); leads without an INN skipped."""
    out: dict[str, Lead] = {}
    for cls in classes:
        for lead in leads:
            if lead.okpd2_class == cls and lead.inn is not None:
                out.setdefault(lead.inn, lead)
    return out

