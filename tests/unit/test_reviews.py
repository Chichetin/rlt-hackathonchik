"""Reviews of a supplier from 2GIS on demand (#234, D-110): who the card is, what is shown, what is not.

2GIS does not publish the INN, so a card is matched by the brand and then trusted only as far as it can be
checked: a phone / site of the card equal to the company's own (from the registry, with its Evidence) —
«подтверждено»; the card names the same legal entity — «юрлицо по названию»; only the name — the card is
shown without a single quote. Pages are the real recordings of tests/fixtures/gis2 (README there).
"""

import json
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src.contracts import Counterparty, Evidence
from src.enrich import evidence, reviews
from src.sources import gis2
from src.sources.cache import SourceCache
from src.sources.pacing import HostPacer

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "gis2"


def page(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


SEARCH = {  # the query the site is asked → the recorded answer; every other query finds nothing (404)
    "ГРЕЙС ТРЭЙД": page("search_grace_trade.html"),
    "САТУРН": page("search_saturn.html"),
    "АРТ-ТЕХНИКА": page("search_art_tehnika.html"),
}
TABS = {  # card id → the recorded reviews tab
    "70000001065972795": page("reviews_grace_trade.html"),
    "5348552839595094": page("reviews_saturn.html"),
    "70000001027588920": page("reviews_empty.html"),
}
GRACE_TAB = "/spb/firm/70000001065972795/tab/reviews"


class Site:
    def __init__(self, status: int = 200, tabs: dict[str, str] | None = None):
        self.status, self.tabs = status, TABS if tabs is None else tabs
        self.search = SEARCH
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        self.paths.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text=page("robots.txt"))
        if self.status != 200:
            return httpx.Response(self.status, text="")
        if path.startswith("/spb/search/"):
            query = urllib.parse.unquote(path.removeprefix("/spb/search/"))
            return httpx.Response(200, text=self.search[query]) if query in self.search else httpx.Response(404)
        branch = path.split("/")[3] if path.startswith("/spb/firm/") else ""
        return httpx.Response(200, text=self.tabs[branch]) if branch in self.tabs else httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    @property
    def tabs_requested(self) -> list[str]:
        return [p for p in self.paths if p.endswith("/tab/reviews")]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(gis2, "PACER", HostPacer(0.0, gis2.BLOCK_COOLDOWN_S))
    return SourceCache(tmp_path / "sources.sqlite")


def company(name: str, inn: str, *, region: str | None = "Санкт-Петербург", **contacts: str) -> Counterparty:
    return Counterparty(inn=inn, name=name, region=region, contacts=contacts)


GRACE = 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ГРЕЙС ТРЭЙД"'
SATURN = 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ " САТУРН "'
ART = 'ООО "АРТ-ТЕХНИКА"'


# --- the brand of a legal name ------------------------------------------------------------------

@pytest.mark.parametrize(("legal", "form", "brand"), [
    (GRACE, "ООО", "ГРЕЙС ТРЭЙД"),
    ('ООО "ГРЕЙС ТРЭЙД"', "ООО", "ГРЕЙС ТРЭЙД"),
    (SATURN, "ООО", "САТУРН"),  # the register keeps stray spaces inside the quotes
    ("АО «ВЕКТОР»", "АО", "ВЕКТОР"),
    ('ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "ГАЗПРОМ"', "ПАО", "ГАЗПРОМ"),
    ("ООО Ромашка", "ООО", "Ромашка"),  # no quotes: what is left after the form
])
def test_brand_of_a_legal_name(legal, form, brand):
    assert reviews.brand_of(legal) == (form, brand)


@pytest.mark.parametrize("legal", ["ИП Иванов Иван Иванович", "ИНН 7801248119", "ООО", 'ООО "ЮГ"', "", "  "])
def test_no_brand_means_no_search(legal):
    assert reviews.brand_of(legal) is None


# --- the three levels of trust --------------------------------------------------------------------

def test_phone_of_the_card_equal_to_the_registry_phone_confirms_the_card(cache):
    site = Site()
    cp = company(GRACE, "7811777270", phone="+7(812)561-91-10")
    result = reviews.find_reviews(cp, cache=cache, client=site.client())
    assert result.status == "ok" and result.queried == "ГРЕЙС ТРЭЙД"
    card = result.cards[0]
    assert (card.basis, card.verified) == ("phone", True)
    assert card.title == "Грейс трэйд" and card.address == "улица Бабушкина, 3"
    assert card.url == "https://2gis.ru" + GRACE_TAB
    assert (card.rating, card.review_count) == (5.0, 1)
    assert "телефон" in card.note.lower()
    quote = card.quotes[0]
    assert quote.date == "2024-03-15" and quote.rating == 5
    assert quote.text.startswith("Производят термоэтикетки отличного качества")
    assert quote.evidence.url == card.url and quote.evidence.field == "review" and quote.evidence.source == "2ГИС"
    assert {e.field for e in card.evidence} == {"address", "phone"}


def test_site_of_the_card_equal_to_the_registry_site_confirms_the_card_too(cache):
    cp = company(GRACE, "7811777270", site="https://www.gracetrade.ru/about")
    card = reviews.find_reviews(cp, cache=cache, client=Site().client()).cards[0]
    assert (card.basis, card.verified) == ("site", True)


def test_a_phone_that_differs_does_not_confirm_and_the_legal_name_is_the_next_level(cache):
    cp = company(GRACE, "7811777270", phone="+7 (812) 000-00-00")
    card = reviews.find_reviews(cp, cache=cache, client=Site().client()).cards[0]
    assert (card.basis, card.verified) == ("legal_name", False)
    assert "ООО Грейс трэйд" in card.note and "ИНН" in card.note
    assert len(card.quotes) == 1  # the card names the same legal entity: the quote is shown, marked


def test_only_the_name_matches_the_chain_is_shown_without_any_quote(cache):
    """ООО «САТУРН» (МСП, канцтовары) vs the building-materials chain «Сатурн»: the very mistake to avoid."""
    site = Site()
    result = reviews.find_reviews(company(SATURN, "7801248119"), cache=cache, client=site.client())
    assert result.status == "ok" and len(result.cards) == 1  # three branches of one organization → one card
    card = result.cards[0]
    assert (card.basis, card.verified) == ("name", False)
    assert card.quotes == [] and card.review_count == 74
    assert "может быть другая организация" in card.note
    assert card.rubrics[:1] == ["Стройматериалы"]  # the sphere is shown: the user sees it is not stationery
    assert site.tabs_requested == []  # no phone or site to compare, no legal entity on the card: nothing to read for


def test_with_a_phone_to_compare_the_tab_of_a_name_only_card_is_read_and_may_confirm_it(cache):
    site = Site()
    cp = company(SATURN, "7801248119", phone="+7 (812) 322-22-66")  # the phone of that very hypermarket card
    card = reviews.find_reviews(cp, cache=cache, client=site.client()).cards[0]
    assert site.tabs_requested == ["/spb/firm/5348552839595094/tab/reviews"]
    assert (card.basis, card.verified) == ("phone", True)  # the contact, not the name, is what confirms


def test_a_card_without_reviews_is_listed_and_its_tab_is_not_requested(cache):
    site = Site()
    card = reviews.find_reviews(company(ART, "7810400592"), cache=cache, client=site.client()).cards[0]
    assert card.title.startswith("Арт-техника") and card.review_count is None and card.quotes == []
    assert site.tabs_requested == []  # nothing to read, nothing to ask


def test_cards_of_other_brands_found_by_the_search_are_ignored(cache):
    result = reviews.find_reviews(company(ART, "7810400592"), cache=cache, client=Site().client())
    assert [c.title for c in result.cards] == ["Арт-техника, магазин товаров для творчества"]


# --- no answer is not an answer -----------------------------------------------------------------

def test_nothing_found_is_said_so(cache):
    result = reviews.find_reviews(company('ООО "ЛЭЙБЛПРИНТПРО"', "7806542555"), cache=cache, client=Site().client())
    assert result.status == "not_found" and result.cards == [] and "2ГИС" in (result.message or "")


def test_an_individual_entrepreneur_is_not_searched_at_all(cache):
    site = Site()
    sole = company("ИП Иванов Иван Иванович", "781100000020")
    result = reviews.find_reviews(sole, cache=cache, client=site.client())
    assert result.status == "unsupported" and site.paths == []


def test_an_unnamed_company_and_a_foreign_region_are_not_searched(cache):
    site = Site()
    unnamed = company("ИНН 7811777270", "7811777270")
    assert reviews.find_reviews(unnamed, cache=cache, client=site.client()).status == "unsupported"
    far = company(GRACE, "7811777270", region="Республика Коми")
    assert reviews.find_reviews(far, cache=cache, client=site.client()).status == "unsupported"
    assert site.paths == []


def test_a_refusal_of_the_site_is_unavailable_not_an_exception_and_not_not_found(cache):
    result = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=Site(status=403).client())
    assert result.status == "unavailable" and result.cards == [] and "недоступен" in (result.message or "")


def test_a_second_request_for_the_same_company_is_answered_from_the_cache(cache):
    site = Site()
    cp = company(GRACE, "7811777270", phone="+78125619110")
    first = reviews.find_reviews(cp, cache=cache, client=site.client())
    sent = len(site.paths)
    again = reviews.find_reviews(cp, cache=cache, client=site.client())
    assert again.model_dump() == first.model_dump()
    assert len(site.paths) == sent


# --- what a quote is ---------------------------------------------------------------------------

def _tab_with(items: list[tuple[str, str, str]]) -> str:
    """The recorded Grace tab with other reviews, (id, day, text): same structure, same escaping as the site."""
    html = page("reviews_grace_trade.html")
    initial, queries = gis2._state(html, gis2._INITIAL), gis2._state(html, gis2._QUERY_STATE)
    assert initial is not None and queries is not None
    queries["queries"][0]["state"]["data"]["pages"][0]["items"] = [
        {"id": rid, "date_created": f"{day}T10:00:00.000000+03:00", "rating": 4, "text": text,
         "is_hidden": False, "provider": "2gis"} for rid, day, text in items
    ]

    def lit(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("\\", "\\\\").replace("'", "\\'")

    return (f"<html><script>var initialState = JSON.parse('{lit(initial)}'); "
            f"var __REACT_QUERY_STATE__ = JSON.parse('{lit(queries)}');</script></html>")


def test_newest_reviews_first_at_most_three_long_ones_cut_at_a_word_with_a_literal_prefix(cache):
    long = "Очень длинный отзыв " * 30
    tab = _tab_with([("1", "2023-01-01", "Самый старый"), ("2", "2026-05-05", long), ("3", "2025-02-02", "Средний"),
                     ("4", "2024-04-04", "Ещё один"), ("5", "2022-02-02", "Совсем старый")])
    site = Site(tabs={"70000001065972795": tab})
    card = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=site.client()).cards[0]
    assert [q.date for q in card.quotes] == ["2026-05-05", "2025-02-02", "2024-04-04"]
    cut = card.quotes[0]
    assert cut.text.endswith("…") and len(cut.text) <= reviews.MAX_QUOTE_CHARS + 1
    value = cut.evidence.value
    assert value is not None and long.startswith(value) and not value.endswith(" ")
    assert evidence.check_evidence(cut.evidence, cache.page_text) == []


def test_reviews_with_a_phone_or_an_email_in_the_text_are_not_quoted(cache):
    tab = _tab_with([("1", "2025-01-01", "Звоните на 8 (812) 123-45-67, там отвечают"),
                     ("2", "2025-01-02", "Пишите ivan@example.org"), ("3", "2025-01-03", "Нормальный отзыв про срок")])
    card = reviews.find_reviews(
        company(GRACE, "7811777270"), cache=cache, client=Site(tabs={"70000001065972795": tab}).client()).cards[0]
    assert [q.text for q in card.quotes] == ["Нормальный отзыв про срок"]


def test_every_quote_passes_the_evidence_check_and_a_made_up_one_does_not(cache):
    cp = company(GRACE, "7811777270", phone="+78125619110")
    card = reviews.find_reviews(cp, cache=cache, client=Site().client()).cards[0]
    quote = card.quotes[0]
    assert evidence.check_evidence(quote.evidence, cache.page_text) == []
    for ev in card.evidence:
        assert evidence.check_evidence(ev, cache.page_text) == []
    fake = Evidence(source="2ГИС", url=card.url, fetched_at=datetime.now(UTC), fact="Отзыв", field="review",
                    value="Производят термоэтикетки отличного качества и ещё возят пиццу")
    assert evidence.check_evidence(fake, cache.page_text)  # the invented tail is not on the page
    nowhere = fake.model_copy(update={"url": "https://2gis.ru/spb/firm/70000001065972796/tab/reviews"})
    assert evidence.check_evidence(nowhere, cache.page_text)  # a page that is not in the cache proves nothing


def test_a_quote_the_page_does_not_contain_is_dropped_before_it_reaches_the_user(cache, monkeypatch):
    real = reviews._excerpt

    def invented(text: str) -> tuple[str, str]:
        shown, value = real(text)
        return shown, value + " и ещё возят пиццу"

    monkeypatch.setattr(reviews, "_excerpt", invented)
    card = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=Site().client()).cards[0]
    assert card.quotes == []  # the card stays, the unverifiable quote does not


def test_fetched_at_of_the_evidence_is_the_time_the_page_was_read(cache):
    before = datetime.now(UTC)
    card = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=Site().client()).cards[0]
    got = card.quotes[0].evidence.fetched_at
    assert before.replace(microsecond=0) <= got <= datetime.now(UTC)
