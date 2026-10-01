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
- `entity: "ul" | "ip"` — ЮЛ / ИП (ИП or a natural person): `stats.entity_type` ("ЮЛ"/"ИП" — derived from the
  INN length in src/ingest/build.py, not a field of the export) for history, else the
  INN length (10 — ЮЛ, 12 — ИП); no INN — unknown, never passes (not guessed);
- `min_score: float` — drops score < min_score, on the server weights of the answer (local weights of
  «Настройки», D-079, re-rank on the client only);
- `region: str` — `Counterparty.region` (enrich, with Evidence) when known, else the KPP prefix from
  organizers' data (78 / 47), the same rule as the `geography` component;
- `near_customer: "worked" | "near" | "any" | "auto"` — «Может работать у заказчика» (#242). The customer's region
  is `search.customer_region` (D-083: the customer's INN 78 / 47, else Query.region); the supplier's region is
  `supplier_region` (registry, else KPP, an ИП without a KPP — the INN). "near": the supplier is in the customer's
  region or the neighbour one (78 <-> 47). "worked": the supplier is in the customer's region, or the customer is in
  St Petersburg and the supplier has bid for its customers (`stats.customers_spb > 0`, organizers' data). "any" or
  empty — no filter. "auto" — the default of the purchase type (`purchase_type.default_near`: near for works and
  services, any for goods and without OKPD2), any when the customer's region has no code. An unknown supplier region
  never passes (not guessed); "worked" / "near" with a customer's region without a code is an error.

A user filter keeps only confirmed matches (D-030): МСП / role / ЮЛ-ИП unknown -> dropped, and the UI says how
many were hidden because the attribute is not known yet; `chip_facets()` gives those counts (X-Facets of
/api/search): each chip group is counted after the filters of the OTHER groups (review of #240), so the number on a
chip is the block size its choice gives with the other filters already on. A bad value (a typo in kinds, entity
"fl", min_score "high") is a ValueError — the API answers 422 — not a silently disabled filter. Unknown keys are
ignored.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Literal

from src.contracts import Counterparty, CounterpartyMatch, Query
from src.match.purchase_type import Near, default_near, purchase_type

_DEAD = ("ликвид", "банкрот", "прекращ")
REGION_CODE = {"Санкт-Петербург": "78", "Ленинградская область": "47"}
KINDS = ("manufacturer", "distributor", "dealer", "unknown")
Entity = Literal["ul", "ip"]
ENTITY_TYPE: dict[str, Entity] = {"ЮЛ": "ul", "ИП": "ip"}  # stats.entity_type of src/ingest/build.py
ENTITIES: dict[str, Entity] = {"ul": "ul", "ip": "ip"}
_INN_ENTITY: dict[int, Entity] = {10: "ul", 12: "ip"}
NEAR_CHOICES = ("worked", "near", "any", "auto")  # filters.near_customer (#242)
SPB = "78"  # stats.customers_spb counts customers with INN 78 (src/ingest/build.py)
NOT_A_REGION = ("00", "99")  # KPP prefixes: a stub; the inspectorate of the largest taxpayers
_REGION_N = re.compile(r"регион (\d{2})")  # Counterparty.region of a region without a name (enrich._region_by_code)


@dataclass(frozen=True)
class UserFilters:
    only_msp: bool = False
    kinds: frozenset[str] | None = None  # None — no role filter
    region: str | None = None
    entity: Entity | None = None
    min_score: float | None = None
    near_customer: str | None = None  # worked | near | auto; None — any


@dataclass(frozen=True)
class NearCustomer:
    """«Может работать у заказчика» of one query, resolved (#242)."""

    choice: Near  # applied: worked | near | any ("auto" already resolved)
    region: str  # the customer's region: search.customer_region (D-083)
    code: str | None  # its code (78 / 47); None — no code, nothing to compare with
    neighbour: str | None  # the code of the neighbour region


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


def supplier_region(cp: Counterparty) -> str | None:
    """Region of the supplier for «Может работать у заказчика»: the registry region (`Counterparty.region`, enrich /
    discovery, with Evidence) as a code; a registry region without a code stays its name (a known region that is
    not 78 / 47); else the KPP of organizers' data or the ФНС card (a 00… stub is not a region, nor is 99… — the
    inspectorate of the largest taxpayers: a telecom or a utility of any region); else — an ИП only, it has no KPP —
    the first two digits of its INN. None when unknown: such a row never passes (not guessed).
    """
    if cp.region:
        if cp.region in REGION_CODE:
            return REGION_CODE[cp.region]
        numbered = _REGION_N.fullmatch(cp.region)
        return numbered.group(1) if numbered else cp.region
    kpp = (cp.kpp or "").strip()
    if kpp[:2].isdigit() and kpp[:2] not in NOT_A_REGION:
        return kpp[:2]
    if entity_of(cp) == "ip" and cp.inn and cp.inn[:2].isdigit():
        return cp.inn[:2]
    return None


def near_marks(cp: Counterparty, nc: NearCustomer) -> tuple[bool, bool]:
    """(worked, near) of a supplier for the customer of `nc`; both False when either region is unknown."""
    if nc.code is None:
        return False, False
    own = supplier_region(cp)
    spb = cp.stats.get("customers_spb")
    bid_spb = nc.code == SPB and isinstance(spb, int | float) and not isinstance(spb, bool) and spb > 0
    return own == nc.code or bid_spb, own is not None and own in (nc.code, nc.neighbour)


def near_of(query: Query, uf: UserFilters | None = None) -> NearCustomer:
    """Resolve filters.near_customer of the query: the customer's region and the applied choice.

    ValueError: "worked" / "near" chosen while the customer's region has no code (nothing to compare with — an
    empty list would look like «nobody nearby»).
    """
    from src.match.search import NEIGHBOUR, customer_region  # search imports this module: not at the top

    region, _ = customer_region(query)
    code = REGION_CODE.get(region)
    choice = (uf if uf is not None else parse_filters(query.filters)).near_customer or "any"
    if choice == "auto":
        choice = default_near(purchase_type(query.okpd2)) if code is not None else "any"
    elif choice != "any" and code is None:
        raise ValueError(f"filters.near_customer: регион заказчика «{region}» не сопоставлен с кодом "
                         f"(известны {', '.join(REGION_CODE)}) — выберите any")
    return NearCustomer(choice=choice, region=region, code=code,  # type: ignore[arg-type]
                        neighbour=NEIGHBOUR.get(code or ""))


def passes_near(cp: Counterparty, nc: NearCustomer) -> bool:
    if nc.choice == "any":
        return True
    worked, near = near_marks(cp, nc)
    return worked if nc.choice == "worked" else near


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

    raw = _text(f, "near_customer")
    if raw is not None and raw not in NEAR_CHOICES:
        raise ValueError(f"filters.near_customer: ожидается worked, near, any или auto, получено {raw!r}")

    region = f.get("region")
    return UserFilters(
        only_msp=bool(f.get("only_msp")),
        kinds=frozenset(kinds) if kinds is not None else None,
        region=region if isinstance(region, str) and region else None,
        entity=entity,
        min_score=min_score,
        near_customer=None if raw in (None, "any") else raw,
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
    nc = near_of(query, uf)
    return [m for m in matches if keeps(m, uf) and passes_near(m.counterparty, nc)]


def apply_filters(matches: list[CounterpartyMatch], query: Query) -> list[CounterpartyMatch]:
    return apply_user_filters(apply_legal(matches), query)


def facets(matches: Iterable[CounterpartyMatch]) -> dict[str, dict[str, int]]:
    """Counts for the chips: role and ЮЛ/ИП of every match given (call it before the user filters)."""
    out = {"kind": dict.fromkeys(KINDS, 0), "entity": {"ul": 0, "ip": 0, "unknown": 0}}
    for m in matches:
        out["kind"][kind_of(m.counterparty)] += 1
        out["entity"][entity_of(m.counterparty) or "unknown"] += 1
    return out


def near_facets(matches: Iterable[CounterpartyMatch], nc: NearCustomer) -> dict[str, int]:
    """Counts for «Может работать у заказчика»: how many rows each of worked / near / any keeps (call before the
    filters); any - <applied> is what the applied choice hides (the UI says it, D-030)."""
    out = {"worked": 0, "near": 0, "any": 0}
    for m in matches:
        worked, near = near_marks(m.counterparty, nc)
        out["worked"] += worked
        out["near"] += near
        out["any"] += 1
    return out


def chip_facets(matches: Iterable[CounterpartyMatch], query: Query) -> dict[str, dict[str, int]]:
    """X-Facets of one block: every chip group counted after the user filters of the OTHER groups (call it on the
    rows after the legal status). kind — every filter but kinds / only_manufacturers; entity — every filter but
    entity; near — every filter but near_customer. A chip count = the rows its choice gives with the rest on."""
    uf = parse_filters(query.filters)
    nc = near_of(query, uf)
    rows = [(m, passes_near(m.counterparty, nc)) for m in matches]
    no_kind, no_entity = replace(uf, kinds=None), replace(uf, entity=None)
    return {
        "kind": facets(m for m, near in rows if near and keeps(m, no_kind))["kind"],
        "entity": facets(m for m, near in rows if near and keeps(m, no_entity))["entity"],
        "near": near_facets((m for m, _ in rows if keeps(m, uf)), nc),
    }
