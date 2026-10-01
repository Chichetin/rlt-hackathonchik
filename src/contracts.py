"""Contracts v0 — the only junction between DATA, MATCH, SOURCES and APP.

Source: section 2 of docs/source/hackathon-research-workflow.md ("Контракты").
Frozen on 01.10 at 11:30 by human confirmation #1 (CLAUDE.md); after that a change is a
separate PR with the list of consumers (`grep -rn "<Type>" src tests`).

v1 — FROZEN 01.10 (human confirmation #1, research/DECISIONS.md D-022). Fields refined by the organizers' data — notices (Извещения),
participants with INN and winner flag (Поставщики), lot positions with OKPD2 (ТРУ); see
research/DATA_DICTIONARY.md. The data has no supplier names, contacts or contract prices.

OpenAPI is generated from these models (`make openapi` -> docs/openapi.json): the same file
is the "integration into AIS GZ" slide.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ComponentName = Literal["product_fit", "experience", "reliability", "activity", "geography", "price"]
Origin = Literal["history", "discovered"]
Kind = Literal["manufacturer", "distributor", "dealer", "unknown"]
# Извещения.is_eshop_or_aisgz: Электронный магазин / АИС ГЗ (порталы 44, 223 [ПРОВЕРИТЬ])
Channel = Literal["ЭМ", "АИС ГЗ"]

_INN10_W = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_INN12_W2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _control(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(d * w for d, w in zip(digits, weights, strict=False)) % 11 % 10


def inn_is_valid(inn: str) -> bool:
    """Checksum of a Russian INN: 10 digits (legal entity) or 12 digits (individual)."""
    if not re.fullmatch(r"\d{10}|\d{12}", inn, re.ASCII):  # isdigit() accepts "²" and Arabic digits
        return False
    d = [int(c) for c in inn]
    if len(inn) == 10:
        return _control(d, _INN10_W) == d[9]
    return _control(d, _INN12_W1) == d[10] and _control(d, _INN12_W2) == d[11]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Query(_Model):
    """Customer need: an item name, a description or a fragment of the spec.

    Mirrors one notice (lot) of organizers' data (research/DATA_DICTIONARY.md): in the product
    the user fills `text` (and optionally the rest); in backtest every field comes from the lot.
    """

    query_id: str
    text: str = Field(min_length=1)  # Извещения.subject / ТРУ.product_name
    okpd2: list[str] = Field(default_factory=list)  # ТРУ.okpd2_code of the lot positions, any level
    ktru: str | None = None
    region: str = "Санкт-Петербург"
    budget: float | None = None  # НМЦК, ₽ = Извещения.start_price
    customer_inn: str | None = None  # Извещения.customer_inn: repeat relations, customer region
    channel: Channel | None = None  # Извещения.is_eshop_or_aisgz
    # History strictly before this date. Eval passes as_of = split cutoff (features only before
    # cutoff, one index — research/EVAL.md, D-018); None in the product (all data).
    as_of: date | None = None
    # only_msp: bool, only_manufacturers: bool, region: str — see docs/ui.md
    filters: dict[str, str | bool | float] = Field(default_factory=dict)


class Evidence(_Model):
    """One fact and where it came from. A fact without Evidence is not shown (CLAUDE.md).

    `url` is required: organizers' data uses `data:<table>#<row key>`, the web uses http(s).
    The DATA loader MUST add `Evidence(field="inn", value=<inn>, url="data:…")` for every
    counterparty from organizers' data: without it strip_unverified() shows "ИНН не подтверждён".
    `field`/`value` name the extracted value (inn, phone, email, address, ...); for web
    sources `value` must literally occur in the cached page text (src/enrich/evidence.py).
    """

    source: str  # "data:contracts", "ЕГРЮЛ", "сайт компании", ...
    url: str = Field(min_length=1)
    fetched_at: datetime
    fact: str  # one line for the user
    field: str | None = None
    value: str | None = None


class Counterparty(_Model):
    inn: str | None  # key; None — INN is not confirmed yet ("ИНН не подтверждён" in UI)
    # Поставщики.supplier_kpp — the most frequent one (some INNs have several); 2 digits — region
    kpp: str | None = None
    name: str  # organizers' data has no names: until enrich(), the loader puts "ИНН <inn>"
    kind: Kind | None = None
    region: str | None = None
    status: str | None = None  # действующее / в процессе ликвидации / ...
    is_msp: bool | None = None
    in_rnp: bool | None = None
    okved: list[str] = Field(default_factory=list)
    contacts: dict[str, str] = Field(default_factory=dict)  # corporate contacts from sources only
    # From organizers' data (as of Query.as_of): lots_participated, lots_won, win_rate,
    # customers, customers_spb, first_seen, last_seen (ISO date), median_start_price,
    # top_okpd2 (comma-separated). Missing key = not computed, never 0 by default.
    stats: dict[str, float | int | str] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)

    @field_validator("inn")
    @classmethod
    def _inn_checksum(cls, v: str | None) -> str | None:
        if v is not None and not inn_is_valid(v):
            raise ValueError(f"INN {v!r} fails the checksum; unresolved company must have inn=None")
        return v


class ScoreComponent(_Model):
    name: ComponentName
    value: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=0.0)
    explanation: str = Field(min_length=1)  # one human line, e.g. "12 контрактов по ОКПД2 32.50.13 за 2 года"


class CounterpartyMatch(_Model):
    counterparty: Counterparty
    score: float
    components: list[ScoreComponent]
    origin: Origin  # history — in organizers' data; discovered — found in open sources


class SearchResponse(_Model):
    """API envelope: two blocks of the result (the incumbent trap, methodology step 5)."""

    query: Query
    history: list[CounterpartyMatch]
    discovered: list[CounterpartyMatch]
    stub: bool = False  # true while any part of the path is a # STUB — shown in UI


# def search(query: Query, k: int = 20, weights: dict | None = None) -> list[CounterpartyMatch]
#   -> src/match/search.py
# def enrich(inn: str) -> Counterparty
#   -> src/enrich/enrich.py
