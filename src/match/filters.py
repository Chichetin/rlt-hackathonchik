"""Hard filters (methodology step 3): user filters + legal status. Owner: MATCH.

Filters the user switches on (docs/ui.md): only_msp, only_manufacturers, region.
Always: an entity in liquidation or in RNP is dropped (a missing value is kept and shown as
"не проверено" — we do not guess).
"""

from __future__ import annotations

from src.contracts import CounterpartyMatch, Query

_DEAD = ("ликвид", "банкрот", "прекращ")


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
        if isinstance(region, str) and region and cp.region != region:
            continue
        out.append(m)
    return out
