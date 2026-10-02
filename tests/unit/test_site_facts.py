"""Facts of a company site (#186, src/enrich/site_facts.py): phone, email, address and role phrases as Evidence
that passes src/enrich/evidence.py; a made-up fact does not; no personal contacts; the INN checksum.

`tests/fixtures/site/*.html` are synthetic pages (no real company). No network, no LLM.
"""

from pathlib import Path

import pytest

from src.contracts import Counterparty, Evidence, inn_is_valid
from src.enrich import site_facts as F
from src.enrich.evidence import check_counterparty, check_evidence, strip_unverified
from src.sources import site
from src.sources.cache import CachedPage, SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "site"
URL = "https://sintetika-karton.example/"
DEALER_URL = "https://sintetika-trade.example/about/"
INN = "0000100012"  # synthetic, valid checksum (tests/fixtures/evidence)


def _page(name: str, url: str, cache: SourceCache) -> CachedPage:
    """The page exactly as src/sources/site.py caches it: visible text + the [links] block."""
    html = (FIX / name).read_text(encoding="utf-8")
    return cache.put(url, site.page_body(html, url))


@pytest.fixture
def cache(tmp_path) -> SourceCache:
    return SourceCache(tmp_path / "c.sqlite")


@pytest.fixture
def company(cache) -> CachedPage:
    return _page("company.html", URL, cache)


# --- contacts ---------------------------------------------------------------------------------------------

def test_contacts_of_the_organisation_only(company):
    facts = F.extract(company)
    assert [e.value for e in facts.phones] == ["+7 (812) 000-00-02"]  # once, though the page has it twice
    assert [e.value for e in facts.emails] == ["info@sintetika-karton.example", "Zakaz@mail.example"]  # own first
    assert [e.value for e in facts.addresses] == ["190000, г. Санкт-Петербург, ул. Тестовая, д. 5, лит. А"]
    assert facts.contacts() == {"phone": "+7 (812) 000-00-02", "email": "info@sintetika-karton.example"}
    for e in facts.evidence():
        assert e.url == URL and e.fetched_at is not None and e.source == site.SOURCE


def test_no_personal_data(company):
    facts = F.extract(company)
    shown = " ".join(f"{e.value} {e.fact}" for e in facts.evidence())
    for personal in ("000-00-03", "000-00-04", "ivanova@", "petrov@", "Иванова", "Петров", "Мария"):
        assert personal not in shown
    assert "111-22-33" not in shown and "hidden@script" not in shown  # a script is not the page text


@pytest.mark.parametrize(("line", "person"), [
    ("Менеджер по продажам: +7 (921) 000-00-03", True),
    ("Иванова Мария Петровна", True),
    ("Петров П. П.", True),
    ("И. И. Сидоров", True),
    ("Генеральный директор", True),
    ("моб. 8 921 000 00 05", True),
    ("Отдел продаж: +7 (812) 000-00-02", False),
    ("Телефон: +7 (812) 000-00-02", False),
    ("Адрес: Санкт-Петербург, ул. Тестовая, д. 5", False),
    ("Общество Синтетика Картон", False),
])
def test_names_person(line, person):
    assert F.names_person(line) is person


def test_individual_entrepreneur_gives_no_contacts(company):
    facts = F.extract(company, individual=True)
    assert facts.phones == [] and facts.emails == [] and facts.addresses == [] and facts.contacts() == {}
    assert facts.roles  # what the site says about the business is not personal data


def test_address_under_a_label_and_cut_before_other_fields(cache):
    facts = F.extract(_page("dealer.html", DEALER_URL, cache))
    assert [e.value for e in facts.addresses] == [
        "Ленинградская область, г. Гатчина, проспект Тестовый, дом 10, офис 2"]
    assert facts.phones == [] and facts.emails == []


@pytest.mark.parametrize("text", [
    "Адрес: Санкт-Петербург",  # no street
    "Адрес: ул. Тестовая",  # no house number
    "Адрес электронной почты: info@x.example",
    "Доставка по адресу заказчика",
    "190000 единиц продукции в год",
    "Адрес: 190000, Санкт-Петербург, ул. Тестовая, д. 5, кв. 7 (Иванов Иван Иванович)",  # a person's address
    "Адрес директора: Санкт-Петербург, ул. Тестовая, д. 5",
])
def test_not_an_address(text, cache):
    page = cache.put("https://x.example/", text)
    assert F.extract(page).addresses == []


def test_bare_digits_are_not_a_phone(cache):
    page = cache.put("https://x.example/", "ОГРН 1027800000012\nИНН 0000100012\nартикул 8123456789")
    assert F.extract(page).phones == []


# --- role phrases -----------------------------------------------------------------------------------------

def test_role_phrases_are_literal_and_shown_with_context(company):
    roles = F.extract(company).roles
    assert [(r.kind, r.phrase) for r in roles] == [("manufacturer", "российский производитель"),
                                                   ("manufacturer", "Собственное производство")]
    text = site.text_of(company.body)
    for r in roles:
        assert r.phrase in text  # literally, as written on the page
        assert r.evidence.field == "site" and r.evidence.value == r.phrase and r.phrase in r.evidence.fact
        assert check_evidence(r.evidence, lambda u: company.body if u == URL else None) == []
    assert "российский производитель упаковки" in roles[0].context
    assert F.extract(company).role() == "manufacturer"


def test_denied_and_invited_roles_are_not_the_sites_role(company):
    # «не являемся официальным дилером», «стать официальным дилером» are on the page
    assert "официальным дилером" in company.body
    assert all(r.kind != "dealer" for r in F.extract(company).roles)


def test_phrases_of_two_roles_give_no_site_role(cache):
    facts = F.extract(_page("dealer.html", DEALER_URL, cache))
    assert [(r.kind, r.phrase) for r in facts.roles] == [("dealer", "официальный дилер"),
                                                         ("distributor", "эксклюзивный дистрибьютор")]
    assert facts.role() is None  # both are shown, none is chosen


@pytest.mark.parametrize(("text", "kind", "phrase"), [
    ("Наш завод-изготовитель работает с 1990 года", "manufacturer", "завод-изготовитель"),
    ("Мы — ЗАВОД – ИЗГОТОВИТЕЛЬ металлоконструкций", "manufacturer", "ЗАВОД – ИЗГОТОВИТЕЛЬ"),
    ("Компания является официальным дистрибьютором бренда", "distributor", "официальным дистрибьютором"),
    ("Мы дистрибьютор расходных материалов", "distributor", "дистрибьютор"),
    ("Авторизованный дилер оборудования", "dealer", "Авторизованный дилер"),
    ("Официальный партнёр и официальный представитель завода", "dealer", "официальный представитель"),
])
def test_dictionary_phrase(text, kind, phrase, cache):
    roles = F.role_phrases(cache.put("https://x.example/", text))
    assert [(r.kind, r.phrase) for r in roles] == [(kind, phrase)]


@pytest.mark.parametrize("text", [
    "Дилерам и дистрибьюторам — специальные условия",  # other word forms: not a statement about the site
    "Ищем дистрибьютор в регионах",
    "Купить у производителя недорого",
    "Требуется официальный представитель в СПб",
    "Производитель: Китай",
])
def test_no_role_phrase(text, cache):
    assert F.role_phrases(cache.put("https://x.example/", text)) == []


def test_every_dictionary_phrase_is_long_enough_for_the_evidence_check():
    from src.enrich.evidence import MIN_TEXT

    phrases = [p for group in F.ROLE_PHRASES.values() for p in group]
    assert all(len(p) >= MIN_TEXT for p in phrases) and len(set(phrases)) == len(phrases)
    assert set(F.ROLE_PHRASES) == {"manufacturer", "distributor", "dealer"}


def test_long_line_context_is_cut_and_still_passes(cache):
    line = "слово " * 60 + "официальный дилер завода, ОГРН 1027800000012345 " + "хвост " * 60
    page = cache.put("https://x.example/", line)
    (r,) = F.role_phrases(page)
    assert len(r.context) <= F.CONTEXT_CHARS + 2 and "официальный дилер" in r.context
    assert check_evidence(r.evidence, lambda u: page.body) == []


# --- Evidence: the honest page and the made-up fact ----------------------------------------------------------

def test_honest_card_passes_evidence(company, cache):
    facts = F.extract(company)
    inns = site.labelled_inns(site.text_of(company.body))
    assert inns == [INN] and inn_is_valid(INN)  # 0000100013 on the page fails the checksum: not an INN
    inn_ev = Evidence(source=site.SOURCE, url=URL, fetched_at=facts.phones[0].fetched_at,
                      fact=f"ИНН {INN} указан на сайте", field="inn", value=INN)
    cp = Counterparty(inn=INN, name="ООО «Синтетика Картон»", contacts=facts.contacts(),
                      evidence=[inn_ev, *facts.evidence()])
    assert check_counterparty(cp, cache.page_text) == []
    assert strip_unverified(cp, cache.page_text) == cp  # nothing is dropped


@pytest.mark.parametrize(("fld", "value", "fact"), [
    ("phone", "+7 (812) 999-99-99", "Телефон на сайте"),
    ("email", "director@sintetika-karton.example", "Email на сайте"),
    ("address", "Санкт-Петербург, Невский проспект, д. 1", "Адрес на сайте"),
    ("site", "официальный дистрибьютор", "На сайте указано: «официальный дистрибьютор»"),
    ("inn", "0000100013", "ИНН на сайте"),  # on the page, but the checksum is wrong
    ("inn", "7707083893", "ИНН на сайте"),  # a valid INN that the page does not hold
])
def test_made_up_fact_fails_evidence(fld, value, fact, company, cache):
    ev = Evidence(source=site.SOURCE, url=URL, fetched_at=F.extract(company).phones[0].fetched_at,
                  fact=fact, field=fld, value=value)
    assert check_evidence(ev, cache.page_text) != []
    cp = Counterparty(inn=None, name="ООО «Выдумка»", evidence=[ev],
                      contacts={fld: value} if fld in ("phone", "email") else {})
    shown = strip_unverified(cp, cache.page_text)
    assert shown.evidence == [] and shown.contacts == {}


def test_extract_never_returns_what_the_page_does_not_hold(cache):
    """Whatever the page looks like, each returned Evidence passes the check against that very page."""
    for name, url in (("company.html", URL), ("dealer.html", DEALER_URL)):
        page = _page(name, url, cache)
        for e in F.extract(page).evidence():
            assert check_evidence(e, cache.page_text) == [], e
    other = cache.put("https://other.example/", "пустая страница")
    for e in F.extract(_page("company.html", URL, cache)).evidence():  # the same facts against another page
        moved = e.model_copy(update={"url": other.url})
        assert check_evidence(moved, cache.page_text) != []


def test_merge_keeps_page_order_without_repeats(company, cache):
    again = cache.put(URL + "contacts/", "Телефон: 8 (812) 000-00-02\nТелефон: +7 (812) 000-00-09\n"
                                         "Мы — официальный дилер завода")
    merged = F.merge([F.extract(company), F.extract(again)])
    assert [e.value for e in merged.phones] == ["+7 (812) 000-00-02", "+7 (812) 000-00-09"]
    assert merged.role() is None and {r.kind for r in merged.roles} == {"manufacturer", "dealer"}
    assert F.merge([]).evidence() == []

