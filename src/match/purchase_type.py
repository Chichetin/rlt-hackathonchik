"""Purchase type by OKPD2 (#242, rule of #188): goods / works / services / remote services. Owner: MATCH.

One point for the default of the filter «Может работать у заказчика» (src/match/filters.py, X-Search-Context of
/api/search) and for the scoring profiles of #104: both must call the same type the same way.

Rule (#188): the OKPD2 class — the first two digits — 01–33 is goods (sections A–C), 41–43 is works (construction,
section F), anything else is services. Exceptions of #188 (decision of the human, 01.10) — goods: printed editions
58.11 / 58.14 / 58.19 and the trade classes 46, 47. Services are split (#104, item 4: on-site services keep the
customer's region, remote ones do not): a class of REMOTE_CLASSES is a remote service.

Several codes: the type of most of them, the list as given (positions of a lot, or the distinct codes of
Query.okpd2); a tie — works > services > remote > goods (on-site work is the stricter case for the customer's
region). A code without two leading digits (or class 00) does not vote; no voting code — None, the type is not
guessed.

REMOTE_CLASSES — the service classes whose real winners travel like goods: «near» would hide at least 20 % of the
wins (a winner outside the customer's region and its neighbour 78 <-> 47, or of an unknown region — e.g. KPP 99, the
largest taxpayers: energy, telecom), goods have 22.3 % (wins of customers 78 / 47 in the organizers' data, the class
of a lot is the mode of its positions, the supplier's region by filters.supplier_region; classes with >= 100 wins;
`uv run python scripts/near_by_class.py`). The table is over all of 2024–2025 and only sets a UI filter default (eval
uses no filters); before the scoring profiles of #104 use it, recompute it on the data before the cutoff
(configs/split.yaml) — else the choice of classes sees the holdout.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Literal

PurchaseType = Literal["goods", "works", "services", "remote"]
Near = Literal["worked", "near", "any"]  # applied values of filters.near_customer ("auto" resolves to one of them)

PURCHASE_LABEL: dict[PurchaseType, str] = {
    "goods": "товар", "works": "работа", "services": "услуга", "remote": "услуга не на месте заказчика",
}
TIE_ORDER: tuple[PurchaseType, ...] = ("works", "services", "remote", "goods")  # first wins a tie
GOODS_LAST, WORKS = 33, (41, 43)  # class ranges of the rule
GOODS_CLASSES = frozenset({46, 47})  # #188: trade — the goods themselves are bought
GOODS_SUBCLASSES = frozenset({"58.11", "58.14", "58.19"})  # #188: printed books, periodicals, other editions
# class -> % of wins «near» would hide (scripts/near_by_class.py, 02.10)
REMOTE_SHARE: dict[int, float] = {
    35: 24.0, 53: 22.6, 55: 34.1, 58: 43.1, 59: 35.5, 62: 26.5, 63: 42.8, 64: 49.1, 65: 80.6, 66: 31.9,
    69: 24.6, 70: 32.3, 72: 25.4, 73: 28.7, 74: 26.5, 78: 50.4, 79: 21.8, 82: 25.4, 85: 21.2,
}
REMOTE_CLASSES = frozenset(REMOTE_SHARE)
BASIS_CODES = 3  # codes shown as the basis of the type
# «Может работать у заказчика» by default (filters.near_customer="auto"): works and on-site services — the customer's
# region or the neighbour one; goods, remote services and an unknown type — any region.
DEFAULT_NEAR: dict[PurchaseType | None, Near] = {
    "works": "near", "services": "near", "remote": "any", "goods": "any", None: "any",
}


def code_type(code: str) -> PurchaseType | None:
    """Type of one OKPD2 code by its class; None when the code has no class (empty, not digits, 00)."""
    code = code.strip()
    head = code[:2]
    if len(head) < 2 or not head.isdigit() or head == "00":
        return None
    n = int(head)
    if n <= GOODS_LAST or n in GOODS_CLASSES or code[:5] in GOODS_SUBCLASSES:
        return "goods"
    if WORKS[0] <= n <= WORKS[1]:
        return "works"
    return "remote" if n in REMOTE_CLASSES else "services"


def purchase_basis(okpd2: Iterable[str]) -> tuple[PurchaseType | None, list[str]]:
    """The type of a purchase and its basis: up to BASIS_CODES distinct codes of that type, in the given order."""
    typed = [(c.strip(), t) for c in okpd2 if (t := code_type(c)) is not None]
    if not typed:
        return None, []
    votes = Counter(t for _, t in typed)
    kind = max(TIE_ORDER, key=lambda t: votes[t])  # max keeps the first of equal counts: the tie order
    return kind, list(dict.fromkeys(c for c, t in typed if t == kind))[:BASIS_CODES]


def purchase_type(okpd2: Iterable[str]) -> PurchaseType | None:
    """goods / works / services / remote of the codes (majority, tie works > services > remote > goods); None — no
    code with a class."""
    return purchase_basis(okpd2)[0]


def default_near(kind: PurchaseType | None) -> Near:
    """The default of «Может работать у заказчика» for a purchase type: near for works and on-site services."""
    return DEFAULT_NEAR[kind]
