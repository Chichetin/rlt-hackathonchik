"""Optional scoring signals for experiments (#22). All are off by default (configs/search.yaml).

incumbent — the supplier already took part in lots of the same customer (`Query.customer_inn`)
strictly before `as_of`. It is not a new component (the contract fixes six names): with
`signals.incumbent = a > 0` the experience component becomes
    experience = (1 - a) · experience + a · sat(lots with this customer, SAT_CUSTOMER_LOTS)
and its explanation says how many lots with the customer were counted. a = 0 — v1 unchanged.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import duckdb

from src.match.index import pq_path

SAT_CUSTOMER_LOTS = 5  # incumbent saturates at 5 lots with the same customer


def signal_shares(cfg: dict) -> dict[str, float]:
    """`signals:` block of the config; unknown names or shares outside [0, 1] are an error."""
    raw = cfg.get("signals") or {}
    known = {"incumbent"}
    if unknown := set(raw) - known:
        raise ValueError(f"неизвестные сигналы: {', '.join(sorted(unknown))}")
    out = {name: float(raw.get(name, 0.0)) for name in known}
    for name, v in out.items():
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"signals.{name} = {v}: доля должна быть в [0, 1]")
    return out


def customer_lots(con: duckdb.DuckDBPyConnection, inns: list[str], customer_inn: str,
                  processed: Path, as_of: date | None) -> dict[str, int]:
    """Distinct lots of `customer_inn` each supplier took part in, strictly before as_of."""
    cond = "publish_date < $as_of" if as_of else "TRUE"
    params: dict = {"inns": inns, "cust": customer_inn}
    if as_of:
        params["as_of"] = as_of
    rows = con.execute(
        f"""
        SELECT supplier_inn, count(DISTINCT lot_id)
        FROM read_parquet({pq_path(processed / "participants.parquet")})
        WHERE supplier_inn IN (SELECT unnest($inns)) AND customer_inn = $cust AND {cond}
        GROUP BY supplier_inn
        """,
        params,
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def incumbent_value(n_lots: int) -> float:
    return min(1.0, math.log1p(n_lots) / math.log1p(SAT_CUSTOMER_LOTS))


def mix_incumbent(exp: float, why: str, n_lots: int, share: float) -> tuple[float, str]:
    if share <= 0.0:
        return exp, why
    value = (1.0 - share) * exp + share * incumbent_value(n_lots)
    return value, f"{why}; с этим заказчиком: {n_lots} лот(ов)"
