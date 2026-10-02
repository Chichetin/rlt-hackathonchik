"""Live-fetch rate from the source cache: pages by host with fetched_at in a UTC window.

`python scripts/cache_rate.py cache/sources.sqlite 2026-10-01T18:03 2026-10-01T18:37`
Cache hits write nothing, so every page in the window is a live answer. Live INN ~ pb cards + rmsp queries
(rmsp is asked for every INN that pb did not answer; bo only after an empty rmsp answer).
"""
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime


def main(path: str, start: str, end: str) -> None:
    lo, hi = (datetime.fromisoformat(x).replace(tzinfo=UTC).timestamp() for x in (start, end))
    with sqlite3.connect(path) as c:
        urls = [r[0] for r in c.execute("SELECT url FROM pages WHERE fetched_at >= ? AND fetched_at < ?", (lo, hi))]
    by = Counter(u.split("/")[2] for u in urls if "company-proc" not in u)  # pb: one card = search + company page
    minutes = (hi - lo) / 60
    live_inn = by.get("pb.nalog.ru", 0) + by.get("rmsp.nalog.ru", 0)
    print(dict(by), f"window {minutes:.0f} min, live INN ~ {live_inn} = {live_inn / minutes:.1f} INN/min")


if __name__ == "__main__":
    main(*sys.argv[1:4])
