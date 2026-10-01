"""Web leads: suppliers found by a web search outside the МСП registry (#108). Owner: SOURCES.

`configs/leads/*.csv` (`;`, UTF-8): one row per company a web search returned for an OKPD2 class —
name, INN as the search result showed it, site, the role the page claims, the result URL and the query.
A lead is a CANDIDATE, not a fact: nothing of the row is shown as a fact. The block «Новые» takes a
lead only when its INN passes the checksum, is absent from organizers' data and its ФНС card is in the
cache (src/enrich/discover.py); name, status, OKVED, role and region then come from that card with
its own Evidence. Without a cached card the INN goes to the enrichment queue. A row without an INN
(the search did not show one) stays a lead for a human: «ИНН не подтверждён», never a guessed INN.

CLI (on a machine with access to the registries; pauses, the captcha stops the run as in enrich):
  python -m src.sources.web_leads --stats             # per class: with INN, new, ФНС card cached
  python -m src.sources.web_leads --enrich [--max N]  # ФНС cards of the new leads into the cache
"""

from __future__ import annotations

import argparse
import csv
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

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


def _new_inns(leads: list[Lead]) -> tuple[list[str], set[str] | None]:
    """All lead INNs and those already in organizers' data (None: the data is absent)."""
    from src.enrich.discover import known_inns
    from src.match.index import processed_dir

    inns = sorted({lead.inn for lead in leads if lead.inn})
    return inns, known_inns(inns, None, processed_dir())


def stats(leads: list[Lead]) -> dict[str, Counter[str]]:
    """Per OKPD2 class: rows, with INN, without INN, already in organizers' data, new, new with a cached card."""
    from src.sources import pb_nalog
    from src.sources.cache import SourceCache

    cache = SourceCache()
    _, known = _new_inns(leads)
    out: dict[str, Counter[str]] = {}
    for lead in leads:
        c = out.setdefault(lead.okpd2_class, Counter())
        c["всего"] += 1
        if lead.inn is None:
            c["без ИНН"] += 1
            continue
        c["с ИНН"] += 1
        if known is not None and lead.inn in known:
            c["уже в данных"] += 1
            continue
        c["новые"] += 1
        c["карточка ФНС в кеше"] += cache.get(pb_nalog.card_url(lead.inn), allow_stale=True) is not None
    if known is None:
        log.warning("данные организаторов не загружены — новизна лидов не проверена")
    return out


def warm(leads: list[Lead], max_n: int, pause_s: float) -> int:
    """ФНС cards of the new leads into the cache (idempotent: a cached card is skipped); stops on a refusal."""
    from src.enrich.enrich import enrich
    from src.sources import pb_nalog
    from src.sources.cache import SourceCache

    cache = SourceCache()
    inns, known = _new_inns(leads)
    todo = [i for i in inns if i not in (known or set())
            and cache.get(pb_nalog.card_url(i), allow_stale=True) is None][:max_n]
    done = 0
    for n, inn in enumerate(todo, start=1):
        try:
            enrich(inn, cache)
            done += 1
            log.info("лид %s: карточка получена (%d/%d)", inn, n, len(todo))
        except KeyError:
            log.warning("лид %s: в реестрах ФНС карточки нет", inn)
        except (pb_nalog.CaptchaRequired, TimeoutError, httpx.HTTPError) as e:
            log.error("лид %s: источник отказал (%s) — остановлено, повторный запуск продолжит", inn, type(e).__name__)
            break
        time.sleep(pause_s)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Лиды из веб-поиска (#108): статистика и прогрев карточек ФНС")
    ap.add_argument("--stats", action="store_true", help="счётчики по классам ОКПД2 (по умолчанию)")
    ap.add_argument("--enrich", action="store_true", help="получить карточки ФНС новых лидов в кеш")
    ap.add_argument("--max", type=int, default=200, help="не больше N карточек за запуск")
    ap.add_argument("--pause", type=float, default=2.0, help="пауза между запросами, с")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    leads = load()
    if args.enrich:
        print(f"получено карточек: {warm(leads, args.max, args.pause)}")
    total: Counter[str] = Counter()
    for cls, c in sorted(stats(leads).items()):
        total.update(c)
        print(f"ОКПД2 {cls}: " + ", ".join(f"{k} {v}" for k, v in c.items()))
    print("ИТОГО: " + ", ".join(f"{k} {v}" for k, v in total.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
