"""Hard filters (methodology step 3): user filters + legal status. Owner: MATCH.

Always (legal status, `apply_legal`): an entity in liquidation or in RNP is dropped (a missing value is
kept and shown as "не проверено" — we do not guess).

User filters (`Query.filters`, docs/ui.md; the contract is a plain dict — keys are read here), ANDed:
- `only_msp: bool` — МСП confirmed by the registry;
- `kinds: str` — roles, comma-separated, any of manufacturer / distributor / dealer / unknown (#232).
  "unknown" is «роль не определена»: the role rule said unknown (D-057) or there is no role yet (None).
  Empty or absent — no filter;
- `only_manufacturers: bool` — the old switch, a synonym of `kinds="manufacturer"` (the e2e and old clients
  send it); with `kinds` both apply (AND), as any two filters;
- `entity: "ul" | "ip"` — ЮЛ / ИП: `stats.entity_type` ("ЮЛ"/"ИП", organizers' data) for history, else the
  INN length (10 — ЮЛ, 12 — ИП); no INN — unknown, never passes (not guessed);
- `min_score: float` — drops score < min_score, on the server weights of the answer (local weights of
  «Настройки», D-079, re-rank on the client only);
- `region: str` — `Counterparty.region` (enrich, with Evidence) when known, else the KPP prefix from
  organizers' data (78 / 47), the same rule as the `geography` component.

A user filter keeps only confirmed matches (D-030): МСП / role / ЮЛ-ИП unknown -> dropped, and the UI says how
many were hidden because the attribute is not known yet; `facets()` gives those counts (X-Facets of
/api/search). A bad value (a typo in kinds, entity "fl", min_score "high") is a ValueError — the API answers
422 — not a silently disabled filter. Unknown keys are ignored.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from src.contracts import Counterparty, CounterpartyMatch, Query

_DEAD = ("ликвид", "банкрот", "прекращ")
REGION_CODE = {"Санкт-Петербург": "78", "Ленинградская область": "47"}
KINDS = ("manufacturer", "distributor", "dealer", "unknown")
Entity = Literal["ul", "ip"]
ENTITY_TYPE: dict[str, Entity] = {"ЮЛ": "ul", "ИП": "ip"}  # stats.entity_type of src/ingest/build.py
ENTITIES: dict[str, Entity] = {"ul": "ul", "ip": "ip"}
_INN_ENTITY: dict[int, Entity] = {10: "ul", 12: "ip"}


@dataclass(frozen=True)
class UserFilters:
    only_msp: bool = False
    kinds: frozenset[str] | None = None  # None — no role filter
    region: str | None = None
    entity: Entity | None = None
    min_score: float | None = None


def in_region(cp: Counterparty, region: str) -> bool | None:
    """True / False when known; None when neither the registry region nor a KPP is known."""
    if cp.region:
        return cp.region == region
    if cp.kpp and len(cp.kpp) >= 2:
        return cp.kpp[:2] == REGION_CODE.get(region)
    return None


def kind_of(cp: Counterparty) -> str:
    """The role chip of a counterparty: its kind, or "unknown" when the role is unknown or not determined yet."""
    return cp.kind if cp.kind in KINDS else "unknown"


def entity_of(cp: Counterparty) -> Entity | None:
    """ЮЛ / ИП: organizers' stats.entity_type first, else the INN length; None when neither is known."""
    stated = cp.stats.get("entity_type")
    if isinstance(stated, str) and stated in ENTITY_TYPE:
        return ENTITY_TYPE[stated]
    return _INN_ENTITY.get(len(cp.inn)) if cp.inn else None


def legal_ok(cp: Counterparty) -> bool:
    return cp.in_rnp is not True and not (cp.status and any(s in cp.status.lower() for s in _DEAD))


def _text(f: Mapping[str, object], key: str) -> str | None:
    v = f.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, str):
        raise ValueError(f"filters.{key}: ожидается строка, получено {v!r}")
    return v.strip().lower() or None


def parse_filters(f: Mapping[str, object]) -> UserFilters:
    """`Query.filters` -> UserFilters; ValueError on a bad value of a known key."""
    kinds: set[str] | None = None
    raw = _text(f, "kinds")
    if raw is not None:
        tokens = {t.strip() for t in raw.split(",") if t.strip()}
        bad = sorted(tokens - set(KINDS))
        if bad:
            raise ValueError(f"filters.kinds: неизвестная роль {', '.join(bad)} — допустимы {', '.join(KINDS)}")
        kinds = tokens or None
    if f.get("only_manufacturers"):  # synonym of kinds="manufacturer", ANDed with kinds
        kinds = {"manufacturer"} & kinds if kinds is not None else {"manufacturer"}

    raw = _text(f, "entity")
    entity = ENTITIES.get(raw) if raw is not None else None
    if raw is not None and entity is None:
        raise ValueError(f"filters.entity: ожидается ul (ЮЛ) или ip (ИП), получено {raw!r}")

    min_score: float | None = None
    v = f.get("min_score")
    if v is not None and v != "":
        if isinstance(v, bool) or not isinstance(v, int | float | str):
            raise ValueError(f"filters.min_score: ожидается число, получено {v!r}")
        try:
            min_score = float(v)
        except ValueError:
            raise ValueError(f"filters.min_score: ожидается число, получено {v!r}") from None
        if not math.isfinite(min_score):
            raise ValueError(f"filters.min_score: ожидается конечное число, получено {v!r}")

    region = f.get("region")
    return UserFilters(
        only_msp=bool(f.get("only_msp")),
        kinds=frozenset(kinds) if kinds is not None else None,
        region=region if isinstance(region, str) and region else None,
        entity=entity,
        min_score=min_score,
    )


def keeps(m: CounterpartyMatch, uf: UserFilters) -> bool:
    """Does the match pass every user filter (legal status is apply_legal's)."""
    cp = m.counterparty
    if uf.only_msp and cp.is_msp is not True:
        return False
    if uf.kinds is not None and kind_of(cp) not in uf.kinds:
        return False
    if uf.entity is not None and entity_of(cp) != uf.entity:
        return False
    if uf.min_score is not None and m.score < uf.min_score:
        return False
    return not (uf.region and in_region(cp, uf.region) is not True)


def apply_legal(matches: Iterable[CounterpartyMatch]) -> list[CounterpartyMatch]:
    """Liquidated / bankrupt / in RNP are dropped whatever the user chose."""
    return [m for m in matches if legal_ok(m.counterparty)]


def apply_user_filters(matches: Iterable[CounterpartyMatch], query: Query) -> list[CounterpartyMatch]:
    uf = parse_filters(query.filters)
    return [m for m in matches if keeps(m, uf)]


def apply_filters(matches: list[CounterpartyMatch], query: Query) -> list[CounterpartyMatch]:
    return apply_user_filters(apply_legal(matches), query)


def facets(matches: Iterable[CounterpartyMatch]) -> dict[str, dict[str, int]]:
    """Counts for the chips: role and ЮЛ/ИП of every match given (call it before the user filters)."""
    out = {"kind": dict.fromkeys(KINDS, 0), "entity": {"ul": 0, "ip": 0, "unknown": 0}}
    for m in matches:
        out["kind"][kind_of(m.counterparty)] += 1
        out["entity"][entity_of(m.counterparty) or "unknown"] += 1
    return out
