"""Hard filters (methodology step 3): user filters + legal status. Owner: MATCH.

Filters the user switches on (docs/ui.md): only_msp, only_manufacturers, region.
Always: an entity in liquidation or in RNP is dropped (a missing value is kept and shown as
"не проверено" — we do not guess).

A user filter keeps only confirmed matches (D-030): МСП / type unknown (None) -> dropped, and the UI
says how many were hidden because the attribute is not known yet. Region: `Counterparty.region`
(enrich, with Evidence) when known, else the KPP prefix from organizers' data (78 / 47), the same
rule as the `geography` component.
"""

from __future__ import annotations

from src.contracts import Counterparty, CounterpartyMatch, Query

_DEAD = ("ликвид", "банкрот", "прекращ")
REGION_CODE = {"Санкт-Петербург": "78", "Ленинградская область": "47"}


def in_region(cp: Counterparty, region: str) -> bool | None:
    """True / False when known; None when neither the registry region nor a KPP is known."""
    if cp.region:
        return cp.region == region
    if cp.kpp and len(cp.kpp) >= 2:
        return cp.kpp[:2] == REGION_CODE.get(region)
    return None


def apply_filters(matches: list[CounterpartyMatch], query: Query) -> list[CounterpartyMatch]:
    f = query.filters
    out = []
    for m in matches:
        cp = m.counterparty
        if cp.in_rnp is True or (cp.status and any(s in cp.status.lower() for s in _DEAD)):
            continue
        if f.get("only_msp") and cp.is_msp is not True:
            continue
        if f.get("only_manufacturers") and cp.kind != "manufacturer":
            continue
        region = f.get("region")
        if isinstance(region, str) and region and in_region(cp, region) is not True:
            continue
        out.append(m)
    return out
