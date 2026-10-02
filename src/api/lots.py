"""Lot lookup: notice number (lot_id or reqnum) or procedure number (procedure_id) -> Query from organizers' data.

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
from collections.abc import Callable
from typing import TYPE_CHECKING

import duckdb

from src.contracts import Query
from src.ingest.notice import notice_query
from src.match.store import Store, StoreUnavailable, get_store
from src.settings import get_settings

if TYPE_CHECKING:
    from src.ingest.testset import TestSet

log = logging.getLogger(__name__)
_testsets: dict[tuple[str, float], TestSet] = {}


class LotDataUnavailable(RuntimeError):
    pass


class LotNotFound(KeyError):
    pass


def _test_set() -> TestSet | None:
    """The export in TEST_DATA_DIR (re-read when the folder changes); None when unset, absent or broken."""
    from src.ingest.testset import TestDataError, load_testset

    path = get_settings().test_data_dir  # relative — from the repository root
    if path is None:
        return None
    try:
        key = (str(path), max((p.stat().st_mtime for p in path.glob("*.csv")), default=0.0))
        ts = _testsets.get(key)
        if ts is None:
            _testsets.clear()
            ts = _testsets[key] = load_testset(path)
    except (TestDataError, OSError) as e:
        log.warning("TEST_DATA_DIR=%s не читается: %s", path, e)
        return None
    return ts


def _test_sets() -> list[TestSet]:
    """The export uploaded in the UI (src/api/testset.py), then TEST_DATA_DIR — the ones present."""
    from src.api.testset import active_testset

    return [ts for ts in (active_testset(), _test_set()) if ts is not None]


def _store_lot(lot_of: Callable[[Store], tuple | None], ident: str) -> Query:
    try:
        store = get_store()
        row = lot_of(store)
        okpd2 = store.lot_okpd2(row[0]) if row else []
        names = store.lot_names(row[0]) if row else []
    except StoreUnavailable as e:  # not loaded (the message names the command), or the database went down
        raise LotDataUnavailable(str(e)) from e
    except duckdb.Error as e:  # a broken parquet: 503, not a crash
        raise LotDataUnavailable(f"не удалось прочитать данные: {e}") from e
    if row is None:
        raise LotNotFound(ident)
    return notice_query(*row, okpd2=okpd2, names=names)


def find_lot(ident: str) -> Query:
    ident = ident.strip()
    for ts in _test_sets():
        lot = ts.find(ident)
        if lot is not None:
            return lot.query
    return _store_lot(lambda s: s.lot(ident), ident)


def find_procedure(ident: str) -> Query:
    """Procedure number (Извещения.procedure_id, 1:1 with the lot, #617) -> the Query of its lot, as find_lot."""
    ident = ident.strip()
    for ts in _test_sets():
        lot = ts.find_procedure(ident)
        if lot is not None:
            return lot.query
    if len(ident) > 18:  # longer than bigint: no such procedure, not a database error
        raise LotNotFound(ident)

    def by_procedure(store: Store) -> tuple | None:
        lot_id = store.lot_by_procedure(int(ident))
        return None if lot_id is None else store.lot(str(lot_id))

    return _store_lot(by_procedure, ident)
