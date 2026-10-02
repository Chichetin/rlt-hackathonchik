"""Role of a counterparty — manufacturer / distributor / dealer — by its main OKVED2. Owner: SOURCES.

Rule v1 (D-057, research/METHODOLOGY.md «Роль контрагента»), no LLM, by the first digits of the code:
10–32 → manufacturer; 33 (repair and installation) and 45.2 (vehicle maintenance) → unknown; 46.1 → dealer
(agents); other 46 → distributor; other 45 and 47 → dealer; anything else → unknown; no code → None.
The main OKVED is what the company declared to the registry, not a proof of production. The role is NOT
part of the score.

The basis is an Evidence of its own: `field="okved"`, `value` = the whole page line with the main OKVED of
this INN — only the keys of BASIS_KEYS (`vyp.КодОКВЭД: 33.12`, `row.<inn>.okved1: 17.12`).
src/enrich/evidence.py keeps `kind` only when that line is on the cached page as a whole line and the rule
applied to its code gives the same kind.

A service or works purchase (#188, the type of src/match/purchase_type.py): the roles manufacturer / distributor /
dealer do not apply, `for_purchase()` makes the kind `unknown` and rewrites the fact of the same basis line —
«роли … к услугам (работам) не применяются; исполнитель по основному ОКВЭД XX.XX «…»». The check accepts `unknown`
on a verified main-OKVED line with that fact (`is_service_basis`), whatever kind the rule gives the code.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from src.contracts import Counterparty, Kind

LABEL: dict[Kind, str] = {"manufacturer": "производитель", "distributor": "дистрибьютор", "dealer": "дилер",
                          "unknown": "не определена"}
_CODE = re.compile(r"\d{2}(?:\.\d{1,2}){0,3}", re.ASCII)
# Page keys of the MAIN OKVED: pb card (one company per page), and the INN's row of the МСП registry
# (`okved1`) and of ГИР БО (`okved2`) — a search page holds many rows and the query's own `query.okved1`.
CARD_KEYS = ("vyp.КодОКВЭД", "search.okved2main")
ROW_KEYS = ("okved1", "okved2")
# #188: purchase types (src/match/purchase_type.PurchaseType) the roles do not apply to -> the dative of the basis
SERVICE_DATIVE = {"services": "услугам", "remote": "услугам", "works": "работам"}
SERVICE_HEAD = "Роль не определена: роли производитель / дистрибьютор / дилер к "
_NAMED = re.compile(r"«(.*)» — ")  # the OKVED name in the fact of assign()


def classify(okved: str | None) -> tuple[Kind, str] | None:
    """Main OKVED2 code → (kind, why); None when there is no code to judge by."""
    code = (okved or "").strip()
    if not _CODE.fullmatch(code):
        return None
    group = int(code[:2])
    if 10 <= group <= 32:
        return "manufacturer", "обрабатывающее производство (раздел C, коды 10–32)"
    if group == 33:
        return "unknown", "ремонт и монтаж машин и оборудования — услуги, не производство (класс 33)"
    if code.startswith("46.1"):
        return "dealer", "торговля оптовая за вознаграждение или на договорной основе — агент, посредник"
    if group == 46:
        return "distributor", "оптовая торговля"
    if code.startswith("45.2"):
        return "unknown", "техническое обслуживание и ремонт автотранспортных средств"
    if group == 45:
        return "dealer", "торговля автотранспортными средствами, мотоциклами и их деталями — перепродавец"
    if group == 47:
        return "dealer", "розничная торговля — перепродавец"
    return "unknown", "вне обрабатывающих производств и торговли — правило v1 роль не определяет"


def basis_keys(inn: str | None) -> set[str]:
    """Page keys whose line may be the basis of the role of this INN."""
    return {*CARD_KEYS, *(f"row.{inn}.{k}" for k in ROW_KEYS if inn)}


def basis_kind(value: str | None, inn: str | None) -> Kind | None:
    """Kind backed by a basis value `<key>: <code>` with a key of the main OKVED of this INN; else None.
    Whether the line is on the page is checked by src/enrich/evidence.py."""
    key, sep, code = (value or "").strip().partition(": ")
    role = classify(code) if sep and key in basis_keys(inn) else None
    return role[0] if role else None


def assign(code: str | None, name: str | None, line: str, add: Callable[[str, str, str], bool]) -> Kind | None:
    """Kind by the main OKVED and its basis added as an Evidence (`add(field, value, fact)` of the
    caller, `line` — the page line holding the code). None: no code, or the fact was not added."""
    role = classify(code)
    if role is None:
        return None
    kind, why = role
    head = f"Роль — {LABEL[kind]}" if kind != "unknown" else "Роль не определена"
    named = f" «{name}»" if name else ""
    return kind if add("okved", line, f"{head}: заявленный основной ОКВЭД {code}{named} — {why}") else None


def is_service_basis(fact: str, value: str | None) -> bool:
    """The fact of a basis line rewritten by for_purchase(): it backs `unknown` whatever the code's rule kind —
    only when it names the code of its own line (`ОКВЭД 17.12` for `row.<inn>.okved1: 17.12`)."""
    code = (value or "").partition(": ")[2].strip()
    return bool(code) and fact.startswith(SERVICE_HEAD) and re.search(
        rf"ОКВЭД {re.escape(code)}(?![\d.])", fact) is not None


def for_purchase(cp: Counterparty, purchase: str | None) -> Counterparty:
    """The role for the purchase type (#188): for services and works `unknown`, every role basis line of the card
    keeps its page line and gets the service fact. Goods, an unknown type, no role or no basis — `cp` as is."""
    dative = SERVICE_DATIVE.get(purchase or "")
    if dative is None or cp.kind is None:
        return cp
    evidence, based = [], False
    for e in cp.evidence:
        if e.field == "okved" and basis_kind(e.value, cp.inn) is not None:
            code = (e.value or "").partition(": ")[2].strip()
            named = f" «{m.group(1)}»" if (m := _NAMED.search(e.fact)) else ""
            fact = f"{SERVICE_HEAD}{dative} не применяются; исполнитель по основному ОКВЭД {code}{named}"
            e, based = e.model_copy(update={"fact": fact}), True
        evidence.append(e)
    return cp.model_copy(update={"kind": "unknown", "evidence": evidence}) if based else cp
