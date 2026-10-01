"""Lot lookup: notice number (lot_id or reqnum) -> Query built from organizers' data.

Reads ingest parquet (data/processed/notices.parquet + items.parquet) when present, else `Извещения*.csv`
(and `ТРУ*.csv` for OKPD2) from DATA_RAW_DIR (default data/raw), `;`-separated (research/DATA_DICTIONARY.md).
Data absent -> LotDataUnavailable (API answers 503, the UI falls back
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


def _dir(env: str, default: str) -> Path:
    d = Path(os.environ.get(env, ROOT / default))
    return d if d.is_absolute() else ROOT / d


def _lit(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _sources() -> tuple[str, str | None]:
    """SQL table expressions for notices and items: ingest parquet if both exist, else raw CSV."""
    proc = _dir("DATA_PROCESSED_DIR", "data/processed")
    n_pq, i_pq = proc / "notices.parquet", proc / "items.parquet"
    if n_pq.exists() and i_pq.exists():
        return f"read_parquet({_lit(n_pq)})", f"read_parquet({_lit(i_pq)})"
    raw = _dir("DATA_RAW_DIR", "data/raw")
    notices = sorted(raw.glob("Извещения*.csv"))
    if not notices:
        raise LotDataUnavailable("данные организаторов не подключены (нет parquet и Извещения*.csv в data/raw)")
    # all_varchar: reqnum has leading zeros, ids are compared as text
    csv = "read_csv({}, delim=';', header=true, all_varchar=true)"
    tru = sorted(raw.glob("ТРУ*.csv"))
    return csv.format(_lit(notices[0])), (csv.format(_lit(tru[0])) if tru else None)


def _is_template(subject: str | None) -> bool:
    # DATA_DICTIONARY: in АИС ГЗ subject is sometimes «Закупка … частью 12 статьи 93» instead of the item
    s = (subject or "").lower()
    return not s.strip() or ("закупка" in s and "статьи 93" in s)


def find_lot(ident: str) -> Query:
    ident = ident.strip()
    n_src, i_src = _sources()
    con = duckdb.connect(":memory:")
    try:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {n_src}").fetchall()}
        chan = "channel" if "channel" in cols else "is_eshop_or_aisgz"
        pname = "procedure_name" if "procedure_name" in cols else "NULL"
        row = con.execute(
            f"SELECT CAST(lot_id AS VARCHAR), subject, {pname}, start_price, customer_inn, {chan} "
            f"FROM {n_src} WHERE CAST(lot_id AS VARCHAR) = ? OR CAST(reqnum AS VARCHAR) = ? LIMIT 1",
            [ident, ident],
        ).fetchone()
        if row is None:
            raise LotNotFound(ident)
        lot_id, subject, procedure_name, price, customer_inn, channel = row
        if _is_template(subject) and procedure_name and str(procedure_name).strip():
            subject = procedure_name
        okpd2: list[str] = []
        if i_src:
            okpd2 = [
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT okpd2_code FROM {i_src} "
                    f"WHERE CAST(lot_id AS VARCHAR) = ? AND okpd2_code IS NOT NULL AND okpd2_code <> '' ORDER BY 1",
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
