"""Reviews of a supplier from 2GIS on demand (#234, D-112): who the card is, what is shown, what is not.

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
    def __init__(self, status: int = 200, tabs: dict[str, str] | None = None, search: dict[str, str] | None = None):
        self.status, self.tabs = status, TABS if tabs is None else tabs
        self.search = SEARCH if search is None else search
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
    assert card.quotes == [] and (card.rating, card.review_count) == (None, None)  # a stranger's stars are not shown
    assert "может быть другая организация" in card.note
    assert card.rubrics[:1] == ["Стройматериалы"]  # the sphere is shown: the user sees it is not stationery
    assert site.tabs_requested == []  # no phone or site to compare, no legal entity on the card: nothing to read for
    assert all("рейтинг" not in e.fact and "отзыв" not in e.fact for e in card.evidence)  # not in the facts either


def test_with_a_phone_to_compare_the_tab_of_a_name_only_card_is_read_and_may_confirm_it(cache):
    site = Site()
    cp = company(SATURN, "7801248119", phone="+7 (812) 322-22-66")  # the phone of that very hypermarket card
    card = reviews.find_reviews(cp, cache=cache, client=site.client()).cards[0]
    assert site.tabs_requested == ["/spb/firm/5348552839595094/tab/reviews"]
    assert (card.basis, card.verified) == ("phone", True)  # the contact, not the name, is what confirms


def test_a_phone_that_is_not_the_cards_leaves_the_name_only_card_without_quotes_even_though_the_tab_was_read(cache):
    """The typical real case: the company has a phone (from the registry), the chain has another one."""
    site = Site()
    cp = company(SATURN, "7801248119", phone="+7 (812) 000-00-00")
    card = reviews.find_reviews(cp, cache=cache, client=site.client()).cards[0]
    assert site.tabs_requested == ["/spb/firm/5348552839595094/tab/reviews"]  # it was read, the reviews are on it...
    assert (card.basis, card.verified) == ("name", False)
    assert card.quotes == [] and (card.rating, card.review_count) == (None, None)  # ...and none of them is shown


def test_one_legal_name_of_several_organizations_confirms_nothing(cache):
    """Three different organizations of the search carry «ООО Сатурн» (cartridges, housing services, heating):
    the legal name does not say which of them is the stationery wholesaler."""
    site = Site(search={"САТУРН": page("search_saturn_legal.html")})
    result = reviews.find_reviews(company(SATURN, "7801248119"), cache=cache, client=site.client())
    assert result.status == "ok" and len(result.cards) == 3
    for card in result.cards:
        assert (card.basis, card.verified, card.quotes) == ("name", False, [])
        assert (card.rating, card.review_count) == (None, None)
        assert "у нескольких разных организаций (3)" in card.note and "ООО Сатурн" in card.note
    assert {c.rubrics[0] for c in result.cards} >= {"Заправка картриджей", "Жилищно-коммунальные услуги"}
    assert all("рейтинг" not in e.fact and "отзыв" not in e.fact for c in result.cards for e in c.evidence)
    assert site.tabs_requested == []  # no contact to compare, an ambiguous legal name: nothing to read for


def test_a_phone_still_confirms_one_of_several_organizations_with_the_same_legal_name(cache):
    tab = page("reviews_grace_trade.html").replace("70000001065972795", "70000001025095817")
    site = Site(search={"САТУРН": page("search_saturn_legal.html")}, tabs={"70000001025095817": tab})
    cp = company(SATURN, "7801248119", phone="+7 (812) 561-91-10")  # a phone on the page of the first card
    result = reviews.find_reviews(cp, cache=cache, client=site.client())
    assert result.cards[0].branch_id == "70000001025095817" and result.cards[0].verified  # the contact decides


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

def _tab_with(items: list[tuple[str, str, str]], contacts: list[dict] | None = None) -> str:
    """The recorded Grace tab with other reviews, (id, day, text), and extra contacts of the card: same structure and
    escaping as the site."""
    html = page("reviews_grace_trade.html")
    initial, queries = gis2._state(html, gis2._INITIAL), gis2._state(html, gis2._QUERY_STATE)
    assert initial is not None and queries is not None
    card = next(iter(initial["data"]["entity"]["profile"].values()))["data"]
    card["contact_groups"][0]["contacts"].extend(contacts or [])
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


# --- more bases, the cut, the switch ---------------------------------------------------------------

def test_the_email_of_the_registry_equal_to_the_email_of_the_card_confirms_it(cache):
    tab = _tab_with([("1", "2025-01-01", "Нормальный отзыв про срок")],
                    contacts=[{"type": "email", "value": "Info@GraceTrade.ru", "text": "info@gracetrade.ru"}])
    site = Site(tabs={"70000001065972795": tab})
    cp = company(GRACE, "7811777270", email="INFO@gracetrade.ru")
    card = reviews.find_reviews(cp, cache=cache, client=site.client()).cards[0]
    assert (card.basis, card.verified) == ("email", True) and "Email" in card.note
    assert {e.field for e in card.evidence} == {"address", "email"}
    assert all(evidence.check_evidence(e, cache.page_text) == [] for e in card.evidence)
    assert [q.text for q in card.quotes] == ["Нормальный отзыв про срок"]


def test_another_email_or_another_site_confirms_nothing(cache):
    tab = _tab_with([("1", "2025-01-01", "Нормальный отзыв про срок")],
                    contacts=[{"type": "email", "value": "info@gracetrade.ru", "text": "info@gracetrade.ru"}])
    cp = company(GRACE, "7811777270", email="sales@other.example", site="https://other.example")
    card = reviews.find_reviews(
        cp, cache=cache, client=Site(tabs={"70000001065972795": tab}).client()).cards[0]
    assert (card.basis, card.verified) == ("legal_name", False)  # not confirmed: the legal name is the next level


def test_a_confirmation_whose_evidence_does_not_pass_is_not_claimed(cache, monkeypatch):
    real = reviews._evidence

    def refuse_phone(src, url, field, value, fact):
        return None if field == "phone" else real(src, url, field, value, fact)

    monkeypatch.setattr(reviews, "_evidence", refuse_phone)
    cp = company(GRACE, "7811777270", phone="+78125619110")  # the card has this phone, the proof cannot be shown
    card = reviews.find_reviews(cp, cache=cache, client=Site().client()).cards[0]
    assert (card.basis, card.verified) == ("legal_name", False)
    assert {e.field for e in card.evidence} == {"address"}


def _search_with_organizations(count: int, legal_for: tuple[int, ...] | None = None,
                               other_brand: tuple[int, ...] = ()) -> str:
    html = page("search_grace_trade.html")
    state = gis2._state(html, gis2._INITIAL)
    assert state is not None
    profiles = state["data"]["entity"]["profile"]
    first = next(iter(profiles.values()))
    out = {}
    for i in range(count):
        node = json.loads(json.dumps(first))
        node["data"]["id"] = f"7000000200000{i:02d}"
        node["data"]["org"]["id"] = f"7000000300000{i:02d}"
        if legal_for is not None and i not in legal_for:
            node["data"]["name_ex"].pop("legal_name", None)
        if i in other_brand:  # another brand of the SAME legal entity
            node["data"]["name"] = "Совсем другое название"
            node["data"]["name_ex"].update(primary="Совсем другое название", short_name="Совсем другое название")
        out[node["data"]["id"]] = node

    def lit(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("\\", "\\\\").replace("'", "\\'")

    state_literal = lit({"data": {"entity": {"profile": out}}})
    return f"<html><script>var initialState = JSON.parse('{state_literal}');</script></html>"


def test_at_most_three_organizations_are_shown_and_the_ones_naming_our_legal_entity_come_first(cache):
    html = _search_with_organizations(5)
    site = Site(search={"ГРЕЙС ТРЭЙД": html}, tabs={})
    result = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=site.client())
    assert len(result.cards) == reviews.MAX_CARDS
    assert all(c.basis == "name" for c in result.cards)  # five organizations share the legal name: ambiguous


def test_a_branch_chain_does_not_push_the_legal_entity_out_of_the_cut(cache):
    """The chain has many branches (one organization); the cut counts organizations, and our legal name goes first."""
    chain = gis2.parse_search_html(page("search_saturn.html"))  # 3 branches of ONE organization
    legal = gis2.parse_search_html(page("search_saturn_legal.html"))  # 3 other organizations
    found = chain + legal[:1]
    got = reviews._candidates(found, "ООО", reviews._norm("САТУРН"))
    assert [c.org_id for c in got] == [legal[0].org_id, chain[0].org_id]  # one card per organization, legal first


def test_the_operator_can_switch_the_reading_of_2gis_off(cache, monkeypatch):
    monkeypatch.setenv("GIS2_DISABLED", "1")
    site = Site()
    result = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=site.client())
    assert result.status == "unavailable" and "отключено" in (result.message or "") and site.paths == []


# --- what the review of the second pass found ----------------------------------------------------------

def test_a_legal_form_that_differs_is_another_legal_entity(cache):
    """«АО Грейс трэйд» on the card, ООО «ГРЕЙС ТРЭЙД» in the registry: not the same, only the name is shared."""
    other_form = page("search_grace_trade.html").replace("ООО Грейс трэйд", "АО Грейс трэйд")
    site = Site(search={"ГРЕЙС ТРЭЙД": other_form})
    card = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=site.client()).cards[0]
    assert (card.basis, card.quotes, card.rating) == ("name", [], None) and site.tabs_requested == []


def test_the_legal_name_found_only_on_the_tab_does_not_make_a_second_organization_unique(cache):
    """Two organizations; only the first names the legal entity in the search. The tab of the second names it too —
    but the uniqueness was counted on the search result, and the second one stays a stranger."""
    first, second = "700000020000000", "700000020000001"  # the ids _search_with_organizations gives its clones
    html = _search_with_organizations(2, legal_for=(0,))
    tab_of = lambda cid: page("reviews_grace_trade.html").replace("70000001065972795", cid)  # noqa: E731
    site = Site(search={"ГРЕЙС ТРЭЙД": html}, tabs={first: tab_of(first), second: tab_of(second)})
    cp = company(GRACE, "7811777270", phone="+7 (812) 000-00-00")  # some contact: the tabs are read, nothing matches
    cards = {c.branch_id: c for c in reviews.find_reviews(cp, cache=cache, client=site.client()).cards}
    assert cards[first].basis == "legal_name" and len(cards[first].quotes) == 1
    assert cards[second].basis == "name" and cards[second].quotes == [] and cards[second].rating is None


def test_a_natural_person_is_refused_by_the_inn_even_when_the_name_has_no_ip_prefix(cache):
    site = Site()
    bare = company("ИВАНОВ ИВАН ИВАНОВИЧ", "781100000020")  # the registry fallback gives the bare name of an ИП
    result = reviews.find_reviews(bare, cache=cache, client=site.client())
    assert result.status == "unsupported" and site.paths == []


def test_the_page_of_a_platform_is_not_the_site_of_the_company(cache):
    tab = _tab_with([("1", "2025-01-01", "Нормальный отзыв про срок")],
                    contacts=[{"type": "website", "url": "https://vk.com/gracetrade"}])
    cp = company(GRACE, "7811777270", site="https://vk.com/gracetrade")
    card = reviews.find_reviews(cp, cache=cache, client=Site(tabs={"70000001065972795": tab}).client()).cards[0]
    assert (card.basis, card.verified) == ("legal_name", False)  # the same host «vk.com» confirms nothing


# --- the notes tell what really matched (third pass) -----------------------------------------------------

def _combined_search(*fixtures: str, take: dict[str, int] | None = None) -> str:
    """One search page from the cards of several recorded ones (organizations of one result)."""
    profiles: dict[str, dict] = {}
    for name in fixtures:
        state = gis2._state(page(name), gis2._INITIAL)
        assert state is not None
        profiles.update(list(state["data"]["entity"]["profile"].items())[: (take or {}).get(name, 99)])

    def lit(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("\\", "\\\\").replace("'", "\\'")

    literal = lit({"data": {"entity": {"profile": profiles}}})
    return f"<html><script>var initialState = JSON.parse('{literal}');</script></html>"


def test_the_note_of_a_chain_among_ambiguous_legal_entities_does_not_borrow_their_name(cache):
    """Two organizations named «ООО Сатурн» and the chain (no legal entity on its card): the chain's note must say what
    it says about ITSELF, not «Юрлицо «None» … у нескольких организаций»."""
    html = _combined_search("search_saturn_legal.html", "search_saturn.html", take={"search_saturn_legal.html": 2})
    site = Site(search={"САТУРН": html})
    result = reviews.find_reviews(company(SATURN, "7801248119"), cache=cache, client=site.client())
    assert result.status == "ok" and all(c.basis == "name" for c in result.cards)
    for card in result.cards:
        assert "None" not in card.note
        if card.title.startswith("Сатурн, строительный"):
            assert "юрлицо не указано" in card.note and "нескольких" not in card.note
        else:
            assert "у нескольких разных организаций (2)" in card.note


def test_a_card_that_names_another_legal_entity_says_so(cache):
    other = page("search_grace_trade.html").replace("ООО Грейс трэйд", "АО Грейс трэйд")
    card = reviews.find_reviews(
        company(GRACE, "7811777270"), cache=cache, client=Site(search={"ГРЕЙС ТРЭЙД": other}).client()).cards[0]
    assert card.basis == "name" and "другое юрлицо — «АО Грейс трэйд»" in card.note
    assert "юрлицо не указано" not in card.note


def test_a_sole_trader_named_as_the_legal_entity_of_a_card_is_not_named_to_the_user(cache):
    sole = page("search_grace_trade.html").replace("ООО Грейс трэйд", "ИП Иванов Иван Иванович")
    card = reviews.find_reviews(
        company(GRACE, "7811777270"), cache=cache, client=Site(search={"ГРЕЙС ТРЭЙД": sole}).client()).cards[0]
    assert card.basis == "name" and "индивидуальный предприниматель" in card.note and "Иванов" not in card.note


def test_a_legal_name_without_a_legal_form_is_not_a_legal_entity(cache):
    bare = page("search_grace_trade.html").replace("ООО Грейс трэйд", "Грейс трэйд")
    card = reviews.find_reviews(
        company(GRACE, "7811777270"), cache=cache, client=Site(search={"ГРЕЙС ТРЭЙД": bare}).client()).cards[0]
    assert (card.basis, card.quotes) == ("name", [])


def test_the_legal_entity_is_counted_over_the_whole_result_even_for_cards_of_another_brand(cache):
    """Another brand of the same legal entity is not a candidate to show, but it makes the legal name ambiguous: the
    count is taken BEFORE the cut to candidates."""
    html = _search_with_organizations(2, other_brand=(1,))
    site = Site(search={"ГРЕЙС ТРЭЙД": html}, tabs={})
    cards = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=site.client()).cards
    assert len(cards) == 1 and (cards[0].basis, cards[0].quotes) == ("name", [])
    assert "у нескольких разных организаций (2)" in cards[0].note


def test_a_contact_found_on_the_search_page_is_proved_on_that_page_when_the_tab_was_not_read(cache):
    """A card without reviews: no tab is read; if the search page itself shows the card's phone, the proof is on it."""
    state = gis2._state(page("search_grace_trade.html"), gis2._INITIAL)
    assert state is not None
    card = next(iter(state["data"]["entity"]["profile"].values()))["data"]
    card["reviews"] = {}
    card["contact_groups"] = [{"contacts": [{"type": "phone", "value": "+78125619110", "text": "+7 812 561-91-10"}]}]

    def lit(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("\\", "\\\\").replace("'", "\\'")

    html = f"<html><script>var initialState = JSON.parse('{lit(state)}');</script></html>"
    site = Site(search={"ГРЕЙС ТРЭЙД": html})
    result = reviews.find_reviews(company(GRACE, "7811777270", phone="+78125619110"), cache=cache, client=site.client())
    got = result.cards[0]
    assert (got.basis, got.verified) == ("phone", True) and site.tabs_requested == []
    assert {e.url for e in got.evidence} == {gis2.search_url("spb", "ГРЕЙС ТРЭЙД")}
    assert all(evidence.check_evidence(e, cache.page_text) == [] for e in got.evidence)


# --- one organization, several branches, several legal entities (fourth pass) ------------------------------

def _rewrite_search(html: str, edit) -> str:
    """A recorded search page after `edit(profiles)` — profiles: {branch id: {"data": …}} — in the site's escaping."""
    state = gis2._state(html, gis2._INITIAL)
    assert state is not None
    edit(state["data"]["entity"]["profile"])

    def lit(obj: object) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("\\", "\\\\").replace("'", "\\'")

    return f"<html><script>var initialState = JSON.parse('{lit(state)}');</script></html>"


def test_a_franchise_the_branch_shown_is_the_one_that_names_our_company_not_the_sole_trader_of_another_branch(cache):
    """The organization has two branches: the one with the reviews names «ИП Иванов…», the warehouse names «ООО Грейс
    трэйд». The reviews belong to the sole trader's branch: not shown under our company, the name not repeated."""
    def franchise(profiles):
        main = profiles["70000001065972795"]["data"]
        main["name_ex"]["legal_name"] = "ИП Иванов Иван Иванович"

    site = Site(search={"ГРЕЙС ТРЭЙД": _rewrite_search(page("search_grace_trade.html"), franchise)})
    result = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=site.client())
    assert len(result.cards) == 1
    card = result.cards[0]
    assert card.title == "Грейс трэйд, склад" and (card.basis, card.quotes) == ("legal_name", [])
    assert "Иванов" not in card.note and "ООО Грейс трэйд" in card.note
    assert site.tabs_requested == []  # the warehouse has no reviews; the sole trader's branch is not read


def test_a_branch_without_a_legal_entity_does_not_speak_for_an_ambiguous_organization(cache):
    """Two organizations name «ООО Грейс трэйд»; the first one also has a branch with more reviews and no legal entity.
    The branch that names it is shown, and its note names it — never «None»."""
    def add_branch(profiles):
        first = profiles["700000020000000"]
        extra = json.loads(json.dumps(first))
        extra["data"]["id"] = "700000020000099"
        extra["data"]["name_ex"].pop("legal_name")
        extra["data"]["reviews"] = {"general_rating": 4.0, "general_review_count": 50}
        profiles["700000020000099"] = extra

    html = _rewrite_search(_search_with_organizations(2), add_branch)
    site = Site(search={"ГРЕЙС ТРЭЙД": html}, tabs={})
    cards = {c.branch_id: c for c in reviews.find_reviews(
        company(GRACE, "7811777270"), cache=cache, client=site.client()).cards}
    assert "700000020000099" not in cards and "700000020000000" in cards
    for card in cards.values():
        assert card.basis == "name" and "None" not in card.note
        assert "«ООО Грейс трэйд» в выдаче 2ГИС у нескольких разных организаций (2)" in card.note


def test_of_two_branches_naming_our_company_the_one_with_more_reviews_is_shown_whatever_the_order(cache):
    def swap(profiles):
        branch = profiles.pop("70000001065972795")  # the branch with the review goes last
        profiles["70000001065972795"] = branch

    html = _rewrite_search(page("search_grace_trade.html"), swap)
    first_id = next(iter(gis2._state(html, gis2._INITIAL)["data"]["entity"]["profile"]))  # type: ignore[index]
    assert first_id == "70000001081189400"  # the warehouse comes first in the page
    card = reviews.find_reviews(
        company(GRACE, "7811777270"), cache=cache, client=Site(search={"ГРЕЙС ТРЭЙД": html}).client()).cards[0]
    assert card.title == "Грейс трэйд" and len(card.quotes) == 1


def test_a_company_whose_legal_form_is_unknown_cannot_be_compared_and_the_note_says_so(cache):
    site = Site(search={"Грейс трэйд": page("search_grace_trade.html")})
    result = reviews.find_reviews(company("Грейс трэйд", "7811777270"), cache=cache, client=site.client())
    assert result.status == "ok"  # the search was answered (the query is the brand as written)
    card = result.cards[0]
    assert (card.basis, card.quotes) == ("name", [])
    assert "форму юрлица компании по её названию не определить" in card.note
    assert "другое юрлицо" not in card.note  # it is not claimed to be another one


def test_a_sole_trader_in_the_full_form_is_not_named_either(cache):
    full_form = "Индивидуальный предприниматель Петров Пётр Петрович"
    sole = page("search_grace_trade.html").replace("ООО Грейс трэйд", full_form)
    card = reviews.find_reviews(
        company(GRACE, "7811777270"), cache=cache, client=Site(search={"ГРЕЙС ТРЭЙД": sole}).client()).cards[0]
    assert card.basis == "name" and "индивидуальный предприниматель" in card.note and "Петров" not in card.note


def test_a_tab_that_names_another_legal_entity_than_the_search_does_not_get_quoted(cache):
    """The search says «ООО Грейс трэйд», the tab of the same card another one: the card that is read is judged."""
    tab = page("reviews_grace_trade.html").replace("ООО Грейс трэйд", "ООО Грейс трэйд Нева")
    site = Site(tabs={"70000001065972795": tab})
    card = reviews.find_reviews(company(GRACE, "7811777270"), cache=cache, client=site.client()).cards[0]
    assert site.tabs_requested == ["/spb/firm/70000001065972795/tab/reviews"]
    assert (card.basis, card.quotes, card.rating) == ("name", [], None)
    assert "другое юрлицо — «ООО Грейс трэйд Нева»" in card.note
