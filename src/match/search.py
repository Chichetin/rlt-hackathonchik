"""search(query) -> ranked counterparties. Owner: MATCH (docs/project-structure.md).

# STUB — the whole module until MATCH replaces it with BM25 + OKPD2 over organizers' data
(methodology step 2, research/METHODOLOGY.md). The stub returns schema-valid synthetic
counterparties (INN with region code 00, which does not exist) so that APP builds the UI and
the e2e demo against the contract from the first hour. Every call logs a warning.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query, ScoreComponent

log = logging.getLogger(__name__)

# Starting expert weights (research/DECISIONS.md D-004). Tuned after backtest — T-15.
DEFAULT_WEIGHTS: dict[str, float] = {
    "product_fit": 0.35,
    "experience": 0.2,
    "reliability": 0.2,
    "activity": 0.1,
    "geography": 0.1,
    "price": 0.05,
}

_STUB_TS = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _stub_party(i: int, inn: str | None, kind: str, msp: bool, region: str, origin: str) -> Counterparty:
    src = "data:contracts" if origin == "history" else "сайт компании"
    url = f"data:contracts#stub-{i}" if origin == "history" else f"https://example.org/stub-{i}"
    return Counterparty(
        inn=inn,
        name=f"ООО «Тестовый поставщик {i}» (STUB)",
        kind=kind,  # type: ignore[arg-type]
        region=region,
        status="действующее",
        is_msp=msp,
        in_rnp=False,
        okved=["46.49"],
        contacts={"email": f"stub{i}@example.org"},
        stats={"contracts_12m": 12 - i} if origin == "history" else {},
        evidence=[
            Evidence(
                source=src,
                url=url,
                fetched_at=_STUB_TS,
                fact=f"Заглушка: синтетический факт {i}, не данные",
                field="email",
                value=f"stub{i}@example.org",
            )
        ],
    )


# (inn, kind, is_msp, region, origin); INN None — "ИНН не подтверждён".
_STUB_ROWS = [
    ("0000100012", "distributor", True, "Санкт-Петербург", "history"),
    ("0000100020", "manufacturer", False, "Санкт-Петербург", "history"),
    ("0000100037", "dealer", True, "Ленинградская область", "history"),
    ("0000100044", "distributor", True, "Москва", "history"),
    ("0000100051", "manufacturer", True, "Санкт-Петербург", "discovered"),
    (None, "unknown", None, "Санкт-Петербург", "discovered"),
    ("0000100069", "manufacturer", False, "Ленинградская область", "discovered"),
]


def _components(i: int, weights: dict[str, float]) -> list[ScoreComponent]:
    base = max(0.1, 1.0 - i * 0.1)
    return [
        ScoreComponent(name=name, value=round(base, 2), weight=w, explanation=f"Заглушка: {name} = {base:.2f}")  # type: ignore[arg-type]
        for name, w in weights.items()
    ]


def search(query: Query, k: int = 20, weights: dict[str, float] | None = None) -> list[CounterpartyMatch]:
    log.warning("STUB search() called for query_id=%s — synthetic data, not a result", query.query_id)
    w = weights or DEFAULT_WEIGHTS
    out: list[CounterpartyMatch] = []
    for i, (inn, kind, msp, region, origin) in enumerate(_STUB_ROWS, start=1):
        comps = _components(i, w)
        out.append(
            CounterpartyMatch(
                counterparty=_stub_party(i, inn, kind, bool(msp), region, origin),
                score=round(sum(c.value * c.weight for c in comps), 4),
                components=comps,
                origin=origin,  # type: ignore[arg-type]
            )
        )
    return out[:k]


IS_STUB = True
