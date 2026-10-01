"""Counterparty from organizers' data (data/processed, built by src/ingest/build.py).

Every counterparty gets `Evidence(field="inn", url="data:participants#<lot_id>")` — the latest lot
where this INN took part (the contract in src/contracts.py requires it: without the Evidence
strip_unverified() shows "ИНН не подтверждён"). Organizers' data has no names: name is
"ИНН <inn>" until enrich() (D-019). An INN that fails the checksum becomes inn=None.
Region is not derived here: a KPP/INN prefix is the tax office, not a fact with Evidence
(`geography` in search reads `kpp` itself). Rows come from a store (src/match/store.py): search()
passes the Postgres or parquet rows to `from_rows`, `counterparties` reads parquet.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

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
    # profile (D-064)
    "okpd2_classes",
    "top_class",
    "okpd2_groups",
    "top_class_share",
    "assortment_breadth",
    "distinct_codes",
    "items_per_lot",
    "p90_start_price",
    "top_customer_share",
    "repeat_win_share",
    "active_months",
    "lots_last_90d",
    "em_offers",
    "em_wins",
    "aisgz_contracts",
    "avg_rivals",
    "entity_type",
    "kpp_count",
    "smp_contracts",
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
    inns: list[str], as_of: date | None = None, processed: Path = DEFAULT_OUT
) -> dict[str, Counterparty]:
    """Counterparty per INN found in the history before as_of (ingest parquet); unknown INNs are absent."""
    from src.match.store import ParquetStore

    if not inns:
        return {}
    suppliers_path(as_of, processed)  # the error names the command that builds it
    return from_rows(ParquetStore(processed).supplier_rows(inns, as_of), built_at(processed))


def from_rows(rows: list[dict], fetched: datetime) -> dict[str, Counterparty]:
    """Store rows (suppliers.* + last_lot, last_channel) -> Counterparty with the INN Evidence."""
    out: dict[str, Counterparty] = {}
    for row in rows:
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
