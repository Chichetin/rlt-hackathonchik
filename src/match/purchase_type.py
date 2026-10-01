"""Purchase type by OKPD2 (#242, rule of #188): goods / works / services. Owner: MATCH.

One point for the default of the filter «Может работать у заказчика» (src/match/filters.py, X-Search-Context of
/api/search) and for the scoring profiles of #104: both must call the same type the same way.

Rule (#188): the OKPD2 class — the first two digits — 01–33 is goods (sections A–C), 41–43 is works (construction,
section F), anything else is services. Several codes: the type of most of them, the list as given (positions of a
lot, or the distinct codes of Query.okpd2); a tie — works > services > goods (on-site work is the stricter case for
the customer's region). A code without two leading digits (or class 00) does not vote; no voting code — None, the
type is not guessed.

Why the type matters (#104 comment 5941229260, winners of organizers' data): a winner from Moscow or the Moscow
region — goods 6.8 %, services 5.3 %, works 1.1 %: goods travel, work on site does not.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Literal

PurchaseType = Literal["goods", "works", "services"]
Near = Literal["worked", "near", "any"]  # applied values of filters.near_customer ("auto" resolves to one of them)

PURCHASE_LABEL: dict[PurchaseType, str] = {"goods": "товар", "works": "работа", "services": "услуга"}
TIE_ORDER: tuple[PurchaseType, ...] = ("works", "services", "goods")  # first wins a tie
GOODS_LAST, WORKS = 33, (41, 43)  # class ranges of the rule
BASIS_CODES = 3  # codes shown as the basis of the type
# «Может работать у заказчика» by default (filters.near_customer="auto"): on-site works and services — the customer's
# region or the neighbour one; goods and an unknown type — any region.
DEFAULT_NEAR: dict[PurchaseType | None, Near] = {"works": "near", "services": "near", "goods": "any", None: "any"}


def code_type(code: str) -> PurchaseType | None:
    """Type of one OKPD2 code by its class; None when the code has no class (empty, not digits, 00)."""
    head = code.strip()[:2]
    if len(head) < 2 or not head.isdigit() or head == "00":
        return None
    n = int(head)
    if n <= GOODS_LAST:
        return "goods"
    if WORKS[0] <= n <= WORKS[1]:
        return "works"
    return "services"


def purchase_basis(okpd2: Iterable[str]) -> tuple[PurchaseType | None, list[str]]:
    """The type of a purchase and its basis: up to BASIS_CODES distinct codes of that type, in the given order."""
    typed = [(c.strip(), t) for c in okpd2 if (t := code_type(c)) is not None]
    if not typed:
        return None, []
    votes = Counter(t for _, t in typed)
    kind = max(TIE_ORDER, key=lambda t: votes[t])  # max keeps the first of equal counts: the tie order
    return kind, list(dict.fromkeys(c for c, t in typed if t == kind))[:BASIS_CODES]


def purchase_type(okpd2: Iterable[str]) -> PurchaseType | None:
    """goods / works / services of the codes (majority, tie works > services > goods); None — no code with a class."""
    return purchase_basis(okpd2)[0]


def default_near(kind: PurchaseType | None) -> Near:
    """The default of «Может работать у заказчика» for a purchase type: near for works and services, else any."""
    return DEFAULT_NEAR[kind]
