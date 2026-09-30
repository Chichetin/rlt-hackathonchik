"""Evidence test on two fixtures: an honest card and one with an INN the LLM made up."""

import json
from pathlib import Path

import pytest

from src.contracts import Counterparty, Evidence, inn_is_valid
from src.enrich.evidence import check_counterparty, check_evidence, strip_unverified, value_in_page

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "evidence"
PAGE = (FIX / "page.html").read_text(encoding="utf-8")
URL = "https://example.org/rekvizity"


def page_text(url: str) -> str | None:
    return PAGE if url == URL else None


def load(name: str) -> Counterparty:
    return Counterparty(**json.loads((FIX / name).read_text(encoding="utf-8")))


@pytest.mark.parametrize("inn,ok", [
    ("7707083893", True), ("7830002293", True), ("500100732259", True),
    ("7707083894", False), ("500100732258", False), ("12345", False), ("77070838a3", False),
])
def test_inn_checksum(inn, ok):
    assert inn_is_valid(inn) is ok


def test_contract_rejects_inn_with_bad_checksum():
    with pytest.raises(ValueError):
        Counterparty(inn="7707083894", name="x")


def test_honest_fixture_passes():
    assert check_counterparty(load("honest.json"), page_text) == []


def test_fabricated_inn_fixture_is_caught():
    errors = check_counterparty(load("fabricated_inn.json"), page_text)
    assert any("inn='0000100020'" in e for e in errors), errors
    assert any("phone=" in e for e in errors), errors


def test_bad_checksum_in_evidence_is_caught():
    ev = Evidence(source="сайт", url=URL, fetched_at="2026-09-30T12:00:00Z", fact="x",  # type: ignore[arg-type]
                  field="inn", value="0000100013")
    assert any("контрольные цифры" in e for e in check_evidence(ev, page_text))


def test_page_missing_from_cache_is_caught():
    ev = Evidence(source="сайт", url="https://example.org/other", fetched_at="2026-09-30T12:00:00Z",  # type: ignore[arg-type]
                  fact="x", field="email", value="sales@example.org")
    assert any("нет в кеше" in e for e in check_evidence(ev, page_text))


def test_inn_without_inn_evidence_is_caught():
    cp = Counterparty(inn="0000100012", name="x", evidence=[])
    assert check_counterparty(cp, page_text) == ["ИНН 0000100012 без Evidence с field='inn'"]


def test_inn_is_matched_as_whole_number():
    assert not value_in_page("inn", "000010001", PAGE)  # prefix of the real INN on the page


def test_organizers_data_evidence_skips_page_check():
    ev = Evidence(source="data:contracts", url="data:contracts#42", fetched_at="2026-09-30T12:00:00Z",  # type: ignore[arg-type]
                  fact="12 контрактов", field="inn", value="0000100012")
    assert check_evidence(ev, lambda _u: None) == []


def ev(field, value, fact="x", url=URL, source="сайт компании"):
    return Evidence(source=source, url=url, fetched_at="2026-09-30T12:00:00Z",  # type: ignore[arg-type]
                    fact=fact, field=field, value=value)


def test_fabricated_contacts_and_fact_text_are_caught():
    errors = check_counterparty(load("fabricated_contact.json"), page_text)
    assert any("контакт phone" in e for e in errors), errors
    assert any("контакт email" in e for e in errors), errors
    assert any("телефон 9991112233 из текста факта" in e for e in errors), errors


def test_strip_unverified_drops_fabricated_contacts():
    cp = strip_unverified(load("fabricated_contact.json"), page_text)
    assert cp.contacts == {} and cp.inn == "0000100012"
    assert all(e.value != "sales@example.org" for e in cp.evidence)  # its fact text had a fake phone
    honest = strip_unverified(load("honest.json"), page_text)
    assert len(honest.evidence) == 4


def test_phone_does_not_match_across_neighbouring_numbers():
    page = "ИНН 7707083893, ОГРН 1027700132195"
    assert not value_in_page("phone", "+7 (893) 102-77-00", page)


@pytest.mark.parametrize("value", ["o@example.org", "sales@example.o", "", "д. 1"])
def test_email_and_short_text_are_not_substring_matches(value):
    field = "address" if value.startswith("д.") or value == "" else "email"
    assert not value_in_page(field, value, PAGE)


def test_web_fact_without_field_is_rejected():
    assert any("без field/value" in e for e in check_evidence(ev(None, None, "Телефон +7 812 000-00-01"), page_text))


def test_data_url_with_web_source_is_rejected():
    errors = check_evidence(ev("inn", "0000100012", url="data:x#1"), page_text)
    assert any("не соответствует схеме" in e for e in errors)


def test_unknown_field_is_rejected():
    assert any("неизвестное поле" in e for e in check_evidence(ev("ceo", "Иванов"), page_text))


@pytest.mark.parametrize("inn", ["²²²²²²²²²²", "٧٧٠٧٠٨٣٨٩٣"])
def test_non_ascii_digits_are_not_an_inn(inn):
    assert not inn_is_valid(inn)


def test_bare_inn_is_not_a_phone():
    assert not value_in_page("phone", "7707083893", "ИНН 7707083893")
    assert value_in_page("phone", "88120000001", PAGE)  # 11 digits with prefix 8, page has +7 (812) 000-00-01
