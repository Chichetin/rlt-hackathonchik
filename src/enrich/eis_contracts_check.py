"""Verification of EIS contract facts (#244) — the enrich layer's check, the pages are read by
src/sources/eis_contracts.py. Kept apart from src/enrich/evidence.py until `contract` returns to its FIELDS
(stage 2 of #244)."""

from __future__ import annotations

import re

from src.contracts import Evidence
from src.enrich import evidence as ev_check
from src.sources import eis_contracts as ec

_FACT_CODE = re.compile(r"ОКПД2 (\d{2}\.\d{2}(?:\.\d{1,2})?(?:\.\d{3})?)")
_FACT_DATE = re.compile(r" от (\d{2}\.\d{2}\.\d{4})")
_FACT_CUSTOMER = re.compile(r", заказчик (.*), позиция ОКПД2 ")


def check_evidence(ev: Evidence, page_text: ev_check.PageText, inn: str | None = None) -> list[str]:
    """evidence.py rules for a contract fact, plus what is specific to it.
    Card fact: the cached page is the card of this very contract, and the OKPD2 code, the date and the
    customer named in the fact are those of the card. Search fact: the contract is an entry of a
    contract search page whose URL is filtered by supplier INN (`inn`, if given, must be that INN).

    `contract` is not in evidence.FIELDS (removed with estore, D-066), so the general rules run with the
    field read as a text field — the same branch `contract` would take there."""
    if ev.field != ec.FIELD or ev.value is None or not ec.NUMBER_RE.fullmatch(ev.value):
        return [f"{ev.url}: не факт о контракте ЕИС (field={ev.field!r}, value={ev.value!r})"]
    errors = ev_check.check_evidence(ev.model_copy(update={"field": "name"}), page_text)
    body = page_text(ev.url)
    if body is None:
        return errors
    if ec.is_search(ev.url):
        supplier = ec.supplier_of(ev.url)
        if supplier is None or (inn is not None and supplier != inn):
            errors.append(f"{ev.url}: выдача не отфильтрована по ИНН поставщика {inn or '(любой)'}")
        if ev.value not in {c.number for c in ec.parse_search(body)[0]}:
            errors.append(f"{ev.url}: контракта {ev.value} нет среди записей выдачи")
        return errors
    card = ec.parse_card(body)
    if card is None or card.number != ev.value:
        errors.append(f"{ev.url}: страница — не карточка контракта {ev.value}")
        return errors
    codes = _FACT_CODE.findall(ev.fact)
    if not codes:
        errors.append(f"{ev.url}: в факте нет кода ОКПД2")
    for code in codes:
        if code not in card.codes:
            errors.append(f"{ev.url}: ОКПД2 {code} нет среди позиций контракта {ev.value}")
    for d in _FACT_DATE.findall(ev.fact):
        if card.signed is None or d != f"{card.signed:%d.%m.%Y}":
            errors.append(f"{ev.url}: дата {d} — не дата заключения контракта {ev.value}")
    for name in _FACT_CUSTOMER.findall(ev.fact):
        if ev_check.normalize(name) != ev_check.normalize(card.customer):
            errors.append(f"{ev.url}: заказчик «{name}» — не заказчик контракта {ev.value}")
    return errors


def check_supply(evidence: list[Evidence], page_text: ev_check.PageText, inn: str) -> list[str]:
    """A True answer as a whole: every fact passes, and every contract has both facts — its card and its
    entry in the search filtered by this supplier INN."""
    errors = [e for ev in evidence for e in check_evidence(ev, page_text, inn)]
    kinds: dict[str | None, set[bool]] = {}
    for ev in evidence:
        kinds.setdefault(ev.value, set()).add(ec.is_search(ev.url))
    for number, k in kinds.items():
        if k != {True, False}:
            errors.append(f"контракт {number}: нужны оба факта — карточка и запись в выдаче по ИНН поставщика")
    return errors
