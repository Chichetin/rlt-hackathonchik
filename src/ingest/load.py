"""Counterparty from organizers' data (data/processed, built by src/ingest/build.py).

Every counterparty gets `Evidence(field="inn", url="data:participants#<lot_id>")` — the latest lot
where this INN took part (the contract in src/contracts.py requires it: without the Evidence
strip_unverified() shows "ИНН не подтверждён"). Organizers' data has no names: name is
"ИНН <inn>" until enrich() (D-019). An INN that fails the checksum becomes inn=None.
Region is not derived here: a KPP/INN prefix is the tax office, not a fact with Evidence
(`geography` in search reads `kpp` itself). Pass a shared `con` from search() per request.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import duckdb

from src.contracts import Counterparty, Evidence, inn_is_valid
from src.ingest.build import DEFAULT_OUT

STATS_KEYS = (
    "lots_participated",
    "lots_won",
    "win_rate",
    "customers",
    "customers_spb",
    "first_seen",
    "last_seen",
    "median_start_price",
    "top_okpd2",
)


def suppliers_path(as_of: date | None, processed: Path = DEFAULT_OUT) -> Path:
    """suppliers.parquet for the product, suppliers_<as_of>.parquet for eval (built for the cutoff)."""
    path = processed / (f"suppliers_{as_of.isoformat()}.parquet" if as_of else "suppliers.parquet")
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not built: run `uv run python -m src.ingest.build` (as_of={as_of}; "
            "only the split cutoff is prebuilt)"
        )
    return path


def built_at(processed: Path = DEFAULT_OUT) -> datetime:
    """fetched_at of data: Evidence — when organizers' CSV were ingested (never invented)."""
    manifest = processed / "manifest.json"
    try:
        return datetime.fromisoformat(json.loads(manifest.read_text(encoding="utf-8"))["built_at"])
    except (OSError, KeyError, ValueError) as e:
        raise FileNotFoundError(f"{manifest}: no built_at — rebuild with `python -m src.ingest.build`") from e


def _stats(row: dict) -> dict[str, float | int | str]:
    out: dict[str, float | int | str] = {}
    for k in STATS_KEYS:
        v = row.get(k)
        if v is None:
            continue  # missing key = not computed, never 0 by default (contract)
        out[k] = v.isoformat() if isinstance(v, date) else v
    return out


def counterparties(
    inns: list[str],
    as_of: date | None = None,
    processed: Path = DEFAULT_OUT,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Counterparty]:
    """Counterparty per INN found in the history before as_of; unknown INNs are absent."""
    if not inns:
        return {}
    own = con is None
    con = con or duckdb.connect()
    try:
        return _counterparties(con, inns, as_of, processed)
    finally:
        if own:
            con.close()


def _counterparties(
    con: duckdb.DuckDBPyConnection, inns: list[str], as_of: date | None, processed: Path
) -> dict[str, Counterparty]:
    sup = str(suppliers_path(as_of, processed)).replace("\\", "/")
    part = str(processed / "participants.parquet").replace("\\", "/")
    cond = "publish_date < $2" if as_of else "TRUE"
    params: list = [inns] + ([as_of] if as_of else [])
    rel = con.execute(
        f"""
        SELECT s.*, last.lot_id AS last_lot, last.channel AS last_channel
        FROM read_parquet('{sup}') s
        LEFT JOIN (
            SELECT supplier_inn, arg_max(lot_id, (publish_date, lot_id)) AS lot_id,
                   arg_max(channel, (publish_date, lot_id)) AS channel
            FROM read_parquet('{part}') WHERE supplier_inn IN (SELECT unnest($1)) AND {cond}
            GROUP BY supplier_inn
        ) last ON last.supplier_inn = s.inn
        WHERE s.inn IN (SELECT unnest($1))
        """,
        params,
    )
    cols = [d[0] for d in rel.description]
    fetched = built_at(processed)
    out: dict[str, Counterparty] = {}
    for values in rel.fetchall():
        row = dict(zip(cols, values, strict=True))
        raw_inn: str = row["inn"]
        valid = inn_is_valid(raw_inn)
        evidence = []
        if valid and row["last_lot"] is not None:
            evidence.append(
                Evidence(
                    source="data:participants",
                    url=f"data:participants#{row['last_lot']}",
                    fetched_at=fetched,
                    fact=f"ИНН {raw_inn} — участник закупки (лот {row['last_lot']}, {row['last_channel']}) "
                    "в данных организаторов",
                    field="inn",
                    value=raw_inn,
                )
            )
        kpp = row.get("kpp")
        out[raw_inn] = Counterparty(
            inn=raw_inn if valid else None,
            kpp=kpp,
            name=f"ИНН {raw_inn}" if valid else f"ИНН не подтверждён ({raw_inn})",
            stats=_stats(row),
            evidence=evidence,
        )
    return out
