"""Regions of the product — one table instead of copies in match, enrich and sources (#292). Layer L0.

A region is compared by strings everywhere (Query.region, Counterparty.region, the KPP / INN prefix): a copy
that drifts silently changes «География» and the region filter. Every Python module reads this table.
Only СПб and ЛО are named (the customers of АИС ГЗ); any other code stays a code («регион 77»).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    code: str  # first two digits of a KPP / INN, regioncode of the МСП registry
    name: str  # Query.region, Counterparty.region, filters
    in_: str  # «в Санкт-Петербурге» — explanations
    prep: str  # «Санкт-Петербурге» — after «выдан в»
    neighbour: str  # code of the neighbour region (geography: NEIGHBOUR_VALUE)


REGIONS: tuple[Region, ...] = (
    Region("78", "Санкт-Петербург", "в Санкт-Петербурге", "Санкт-Петербурге", "47"),
    Region("47", "Ленинградская область", "в Ленинградской области", "Ленинградской области", "78"),
)
NEIGHBOUR_VALUE = 0.8  # geography of a supplier from the neighbour region

REGION_CODE: dict[str, str] = {r.name: r.code for r in REGIONS}  # name -> code
REGION_NAME: dict[str, str] = {r.code: r.name for r in REGIONS}  # code -> name
REGION_IN: dict[str, str] = {r.code: r.in_ for r in REGIONS}
REGION_PREP: dict[str, str] = {r.code: r.prep for r in REGIONS}
NEIGHBOUR: dict[str, str] = {r.code: r.neighbour for r in REGIONS}


def name_by_code(code: str | None) -> str | None:
    """Name of a known region by its two-digit code (a KPP prefix), else None."""
    return REGION_NAME.get((code or "")[:2]) if code else None


def label_by_code(code: str | None) -> str | None:
    """Name of a known region, «регион <code>» for another one, None without a code (src/match/filters.py reads
    «регион NN» back)."""
    return REGION_NAME.get(code or "") or (f"регион {code}" if code else None)
