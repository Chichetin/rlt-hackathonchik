"""Web leads (#108, src/sources/web_leads.py): statistics and the warm-up of their ФНС cards. Owner: SOURCES.

Orchestration lives in enrich, not in the source (#294, D-106): it needs organizers' data (novelty of a lead —
src/enrich/discover.py known_inns) and enrich() for the cards.

CLI (on a machine with access to the registries; pauses, the captcha stops the run as in enrich):
  python -m src.enrich.web_leads_warm --stats             # per class: with INN, new, ФНС card cached
  python -m src.enrich.web_leads_warm --enrich [--max N]  # ФНС cards of the new leads into the cache
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter

import httpx

from src.enrich.discover import known_inns
from src.enrich.enrich import enrich
from src.match.index import processed_dir
from src.sources import pb_nalog, web_leads
from src.sources.cache import SourceCache
from src.sources.web_leads import Lead

log = logging.getLogger(__name__)


def _new_inns(leads: list[Lead]) -> tuple[list[str], set[str] | None]:
    """All lead INNs and those already in organizers' data (None: the data is absent)."""
    inns = sorted({lead.inn for lead in leads if lead.inn})
    return inns, known_inns(inns, None, processed_dir())


def stats(leads: list[Lead]) -> dict[str, Counter[str]]:
    """Per OKPD2 class: rows, with INN, without INN, already in organizers' data, new, new with a cached card."""
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
    cache = SourceCache()
    inns, known = _new_inns(leads)
    todo = [i for i in inns if i not in (known or set())
            and cache.get(pb_nalog.card_url(i), allow_stale=True) is None][:max_n]
    done = 0
    for n, inn in enumerate(todo, start=1):
        try:
            # ФНС cards only: a live РНП check here would add its 60 s Crawl-delay to every lead (D-152);
            # РНП is the separate pass of the shortlist (`python -m src.worker.prefetch --rnp`)
            enrich(inn, cache, rnp_offline=True)
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
    leads = web_leads.load()
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
