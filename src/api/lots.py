"""Lot lookup: notice number (lot_id or reqnum) -> Query built from organizers' data.

Reads the store search() reads (src/match/store.py): Postgres schema `data` or ingest parquet — one SQL for
both (#293: no second copy of the lookup on raw CSV — `make demo` builds the ingest, `make up` reads Postgres
loaded from it by `make db-load`; without them search answers 503 as well).
Data absent -> LotDataUnavailable (API answers 503 with the command that builds it, the UI falls back
to free text); lot absent -> LotNotFound (404). Nothing is invented.

Lookup order: the test export uploaded in the UI (#207, src/api/testset.py) -> TEST_DATA_DIR (#121): a
folder with the organizers' test export (same CSV format, src/ingest/testset.py) -> organizers' data, so a
test procedure opens by its number like a training lot; a broken export is logged and the next place is
still searched.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import duckdb

from src.contracts import Query
from src.ingest.notice import notice_query
from src.match.store import StoreUnavailable, get_store

ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)
_testsets: dict[tuple[str, float], object] = {}


class LotDataUnavailable(RuntimeError):
    pass


class LotNotFound(KeyError):
    pass


def _dir(env: str, default: str) -> Path:
    d = Path(os.environ.get(env, ROOT / default))
    return d if d.is_absolute() else ROOT / d


def _test_lot(ident: str) -> Query | None:
    """Lot from TEST_DATA_DIR (re-read when the folder changes); None when unset, absent or broken."""
    from src.ingest.testset import TestDataError, TestSet, load_testset

    folder = os.environ.get("TEST_DATA_DIR")
    if not folder:
        return None
    path = _dir("TEST_DATA_DIR", folder)
    try:
        key = (str(path), max((p.stat().st_mtime for p in path.glob("*.csv")), default=0.0))
        ts = _testsets.get(key)
        if ts is None:
            _testsets.clear()
            ts = _testsets[key] = load_testset(path)
    except (TestDataError, OSError) as e:
        log.warning("TEST_DATA_DIR=%s не читается: %s", folder, e)
        return None
    assert isinstance(ts, TestSet)
    lot = ts.find(ident)
    return lot.query if lot else None


def _uploaded_lot(ident: str) -> Query | None:
    """Lot from the test export uploaded in the UI; None when none is active (src/api/testset.py)."""
    from src.api.testset import active_testset

    ts = active_testset()
    lot = ts.find(ident) if ts else None
    return lot.query if lot else None


def find_lot(ident: str) -> Query:
    ident = ident.strip()
    for lookup in (_uploaded_lot, _test_lot):
        test = lookup(ident)
        if test is not None:
            return test
    try:
        store = get_store()
        row = store.lot(ident)
        okpd2 = store.lot_okpd2(row[0]) if row else []
        names = store.lot_names(row[0]) if row else []
    except StoreUnavailable as e:  # not loaded (the message names the command), or the database went down
        raise LotDataUnavailable(str(e)) from e
    except duckdb.Error as e:  # a broken parquet: 503, not a crash
        raise LotDataUnavailable(f"не удалось прочитать данные: {e}") from e
    if row is None:
        raise LotNotFound(ident)
    return notice_query(*row, okpd2=okpd2, names=names)

