"""Lot lookup: notice number (lot_id or reqnum) -> Query built from organizers' data.

Reads `Извещения*.csv` (and `ТРУ*.csv` for OKPD2) from DATA_RAW_DIR (default data/raw), `;`-separated
(research/DATA_DICTIONARY.md). Data absent -> LotDataUnavailable (API answers 503, the UI falls back
to free text); lot absent -> LotNotFound (404). Nothing is invented.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from src.contracts import Query

ROOT = Path(__file__).resolve().parents[2]


class LotDataUnavailable(RuntimeError):
    pass


class LotNotFound(KeyError):
    pass


def _raw_dir() -> Path:
    d = Path(os.environ.get("DATA_RAW_DIR", ROOT / "data" / "raw"))
    return d if d.is_absolute() else ROOT / d


def _csv(con: duckdb.DuckDBPyConnection, path: Path) -> str:
    # all_varchar: reqnum has leading zeros, ids are compared as text
    return f"read_csv('{path.as_posix().replace(chr(39), chr(39) * 2)}', delim=';', header=true, all_varchar=true)"


def find_lot(ident: str) -> Query:
    ident = ident.strip()
    notices = sorted(_raw_dir().glob("Извещения*.csv"))
    if not notices:
        raise LotDataUnavailable("данные организаторов не подключены (нет Извещения*.csv в data/raw)")
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            f"SELECT lot_id, reqnum, subject, start_price, customer_inn, is_eshop_or_aisgz "
            f"FROM {_csv(con, notices[0])} WHERE lot_id = ? OR reqnum = ? LIMIT 1",
            [ident, ident],
        ).fetchone()
        if row is None:
            raise LotNotFound(ident)
        lot_id, _reqnum, subject, price, customer_inn, channel = row
        okpd2: list[str] = []
        tru = sorted(_raw_dir().glob("ТРУ*.csv"))
        if tru:
            okpd2 = [
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT okpd2_code FROM {_csv(con, tru[0])} "
                    f"WHERE lot_id = ? AND okpd2_code IS NOT NULL AND okpd2_code <> '' ORDER BY 1",
                    [lot_id],
                ).fetchall()
            ]
    except duckdb.Error as e:
        raise LotDataUnavailable(f"не удалось прочитать данные: {e}") from e
    finally:
        con.close()
    try:
        budget = float(price) if price not in (None, "") else None
    except ValueError:
        budget = None
    return Query(
        query_id=f"lot-{lot_id}",
        text=(subject or "").strip() or f"Закупка {lot_id}",
        okpd2=okpd2,
        budget=budget,
        customer_inn=customer_inn or None,
        channel=channel if channel in ("ЭМ", "АИС ГЗ") else None,  # type: ignore[arg-type]
    )
