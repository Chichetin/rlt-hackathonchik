"""Disk cache of external sources (SQLite, TTL). Owner: SOURCES.

Every answer of an external source goes through here: the demo works on cache even if the
venue internet dies (section 6 of the workflow). The cached page text is also what
src/enrich/evidence.py checks extracted values against.

Location: $SOURCE_CACHE (default cache/sources.sqlite, not in git).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("SOURCE_CACHE", "cache/sources.sqlite"))
DEFAULT_TTL_S = int(os.environ.get("SOURCE_CACHE_TTL_S", str(7 * 24 * 3600)))


@dataclass(frozen=True)
class CachedPage:
    url: str
    body: str
    fetched_at: float  # unix time
    status: int


class SourceCache:
    def __init__(self, path: Path | str = DEFAULT_PATH, ttl_s: int = DEFAULT_TTL_S):
        self.path = Path(path)
        self.ttl_s = ttl_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, url TEXT NOT NULL, "
                "body TEXT NOT NULL, status INTEGER NOT NULL, fetched_at REAL NOT NULL)"
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as c, c:  # commit/rollback, then close
            yield c

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def get(self, url: str, *, allow_stale: bool = False) -> CachedPage | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT url, body, fetched_at, status FROM pages WHERE key = ?", (self.key(url),)
            ).fetchone()
        if row is None:
            return None
        page = CachedPage(url=row[0], body=row[1], fetched_at=row[2], status=row[3])
        if not allow_stale and time.time() - page.fetched_at > self.ttl_s:
            return None
        return page

    def put(self, url: str, body: str, status: int = 200, fetched_at: float | None = None) -> CachedPage:
        ts = time.time() if fetched_at is None else fetched_at
        page = CachedPage(url=url, body=body, fetched_at=ts, status=status)
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO pages (key, url, body, status, fetched_at) VALUES (?, ?, ?, ?, ?)",
                (self.key(url), page.url, page.body, page.status, page.fetched_at),
            )
        return page

    def page_text(self, url: str) -> str | None:
        """For src/enrich/evidence.py: the cached text regardless of TTL (the fact was read then)."""
        page = self.get(url, allow_stale=True)
        return page.body if page else None

    def fetch(self, url: str, fetcher: Callable[[str], tuple[int, str]]) -> CachedPage:
        """Cached fetch: fresh cache → cache; else fetcher; fetcher failed → stale cache if any."""
        hit = self.get(url)
        if hit is not None:
            return hit
        try:
            status, body = fetcher(url)
        except Exception:
            stale = self.get(url, allow_stale=True)
            if stale is not None:
                return stale
            raise
        return self.put(url, body, status)
