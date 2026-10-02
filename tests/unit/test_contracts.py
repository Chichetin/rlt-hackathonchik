"""Counterparty.country / foreign_id (#200): a company of an EAEU country has no Russian INN."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.contracts import Counterparty, Evidence, inn_is_valid
from src.enrich.evidence import check_counterparty, strip_unverified

# Synthetic identifiers of the right shape, not real companies: a 9-digit УНП and a 12-digit БИН.
UNP = "190000000"
BIN = "123456789012"
URL = "https://registry.example/by/190000000"
NOW = datetime(2026, 10, 2, tzinfo=UTC)


def test_foreign_company_with_foreign_id_passes():
    cp = Counterparty(inn=None, name="ОАО «Пример»", country="BY", foreign_id=f"BY:{UNP}")
    assert (cp.country, cp.foreign_id, cp.inn) == ("BY", f"BY:{UNP}", None)
    assert Counterparty(inn=None, name="ТОО «Пример»", country="KZ", foreign_id=f"KZ:{BIN}").foreign_id == f"KZ:{BIN}"


def test_russian_company_is_unchanged():
    assert Counterparty(inn="7707083893", name="x").country is None
    assert Counterparty(inn="7707083893", name="x", country="RU").foreign_id is None


def test_bin_in_inn_does_not_pass_without_country():
    assert not inn_is_valid(BIN)
    with pytest.raises(ValidationError):
        Counterparty(inn=BIN, name="x")


def test_foreign_company_never_has_inn():
    with pytest.raises(ValidationError):  # even a number that passes the INN checksum
        Counterparty(inn="7707083893", name="x", country="KZ")


@pytest.mark.parametrize("kwargs", [
    {"inn": None, "foreign_id": f"BY:{UNP}"},  # no country
    {"inn": None, "country": "RU", "foreign_id": f"RU:{UNP}"},  # Russia has an INN
    {"inn": None, "country": "KZ", "foreign_id": f"BY:{UNP}"},  # prefix differs from the country
    {"inn": None, "country": "BY", "foreign_id": UNP},  # no prefix
    {"inn": "7707083893", "foreign_id": f"BY:{UNP}"},  # INN and a foreign id together
    {"inn": None, "country": "Беларусь"},  # not ISO-2
    {"inn": None, "country": "by"},
])
def test_invalid_foreign_combinations(kwargs):
    with pytest.raises(ValidationError):
        Counterparty(name="x", **kwargs)


def _card(value: str) -> Counterparty:
    ev = Evidence(source="реестр", url=URL, fetched_at=NOW, fact=f"УНП {UNP}", field="foreign_id", value=value)
    return Counterparty(inn=None, name="ОАО «Пример»", country="BY", foreign_id=value, evidence=[ev])


def test_foreign_id_needs_evidence_on_the_cached_page():
    pages = {URL: f"by.unp: {UNP}\nby.name: ОАО «Пример»"}
    cp = _card(f"BY:{UNP}")
    assert check_counterparty(cp, pages.get) == []
    shown = strip_unverified(cp, pages.get)
    assert (shown.country, shown.foreign_id) == ("BY", f"BY:{UNP}")

    forged = _card("BY:190000001")  # the page holds another number
    assert check_counterparty(forged, pages.get)
    hidden = strip_unverified(forged, pages.get)
    assert (hidden.country, hidden.foreign_id, hidden.evidence) == (None, None, [])

    longer = {URL: f"by.unp: {UNP}7"}  # the identifier inside a longer number is not the identifier
    assert strip_unverified(cp, longer.get).foreign_id is None

    bare = Counterparty(inn=None, name="x", country="BY", foreign_id=f"BY:{UNP}")
    assert any("foreign_id" in e for e in check_counterparty(bare, pages.get))
    assert strip_unverified(bare, pages.get).country is None
