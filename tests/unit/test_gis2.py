"""2ГИС: parsing of recorded pages, the flat text that goes to the cache, and the rules of the client.

Fixtures (`tests/fixtures/gis2/README.md`) are REAL pages of 2gis.ru recorded 02.10.2026, trimmed:
- `search_saturn.html` — query «САТУРН»: the chain «Сатурн, строительный гипермаркет» (a different company
  from the ООО «САТУРН» of the МСП register — the reason why a name alone never confirms a match);
- `search_art_tehnika.html` — a branch without reviews and three other cards; `search_grace_trade.html` —
  cards whose `org.primary` is empty (the name is the brand itself);
- `reviews_rostelecom.html` — the reviews tab (4 of 6 reviews, authors removed) with the phone of the card;
- `reviews_empty.html` — the reviews tab of a card without reviews.
"""

import json
from pathlib import Path

import httpx
import pytest

from src.sources import gis2
from src.sources.cache import SourceCache
from src.sources.pacing import HostPacer

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "gis2"
SATURN = (FIX / "search_saturn.html").read_text(encoding="utf-8")
ART = (FIX / "search_art_tehnika.html").read_text(encoding="utf-8")
GRACE = (FIX / "search_grace_trade.html").read_text(encoding="utf-8")
GRACE_REVIEWS = (FIX / "reviews_grace_trade.html").read_text(encoding="utf-8")
ROSTELECOM = (FIX / "reviews_rostelecom.html").read_text(encoding="utf-8")
EMPTY = (FIX / "reviews_empty.html").read_text(encoding="utf-8")
ROBOTS = (FIX / "robots.txt").read_text(encoding="utf-8")

ROSTELECOM_ID = "70000001104087830"


def test_unescape_js_literal():
    assert gis2.unescape_js(r"a\\b \'q\' \n Ж \x41") == "a\\b 'q' \n Ж A"


def test_search_cards_of_the_chain_keep_org_address_and_card_rating():
    cards = gis2.parse_search_html(SATURN)
    assert [c.id for c in cards] == ["5348552839595094", "5348552838577613", "5348552838577615"]
    first = cards[0]
    assert (first.name, first.primary) == ("Сатурн, строительный гипермаркет", "Сатурн")
    assert first.org_id == "5348561428468307"
    assert first.address == "Литовская улица, 15"
    assert first.rubrics[:2] == ["Стройматериалы", "Отделочные материалы"]
    assert (first.rating, first.review_count) == (4.9, 74)
    assert {c.org_id for c in cards} == {"5348561428468307"}  # one organization, several branches


def test_card_without_reviews_has_no_rating_and_no_count():
    art = gis2.parse_search_html(ART)[0]
    assert (art.primary, art.rating, art.review_count) == ("Арт-техника", None, None)


def test_legal_name_is_read_when_the_card_states_it_and_brand_falls_back_to_name():
    grace, branch = gis2.parse_search_html(GRACE)
    assert (grace.name, grace.primary, grace.legal_name) == ("Грейс трэйд", "Грейс трэйд", "ООО Грейс трэйд")
    assert (branch.name, branch.primary, branch.org_id) == ("Грейс трэйд, склад", "Грейс трэйд", grace.org_id)
    assert gis2.parse_search_html(SATURN)[0].legal_name is None  # a chain card does not state the legal entity
    assert gis2.parse_search_html(SATURN)[0].brand == "Сатурн"  # the brand, not «Сатурн, строительный гипермаркет»
    bare = gis2.Card("70000001065972795", "Грейс", None, None, None, None, [], None, None, [], [])
    assert bare.brand == "Грейс"


def test_reviews_page_card_contacts_and_reviews_without_authors():
    page = gis2.parse_reviews_html(ROSTELECOM)
    assert page.card.id == ROSTELECOM_ID
    assert page.card.phones == ["88001000800"]  # the digits from `value`, not the figure dashes of `text`
    assert (page.card.rating, page.card.review_count) == (1.1, 6)
    assert [r.id for r in page.reviews] == ["264728803", "260626922", "247890942", "245491779"]
    first = page.reviews[0]
    assert (first.date, first.rating) == ("2026-08-04", 1)
    assert first.text.startswith("Самый ужасный оператор связи")
    assert "user" not in json.dumps(first.__dict__)  # the author is never kept


def test_reviews_page_of_a_card_without_reviews_is_empty_not_an_error():
    page = gis2.parse_reviews_html(EMPTY)
    assert page.reviews == [] and page.card.id == "70000001027588920"


def test_page_without_state_is_not_parsed_as_empty():
    with pytest.raises(gis2.Blocked):
        gis2.parse_search_html("<html><title>Подтвердите, что вы не робот</title></html>")
    with pytest.raises(gis2.Blocked):
        gis2.parse_reviews_html("<html>captcha</html>")


def test_flat_text_round_trips_and_keeps_values_verbatim():
    cards = gis2.parse_search_html(SATURN)
    flat = gis2.flatten_search(cards)
    assert "card.5348552839595094.address: Литовская улица, 15" in flat
    assert gis2.parse_search(flat) == cards
    page = gis2.parse_reviews_html(ROSTELECOM)
    flat = gis2.flatten_reviews(page)
    assert "card.70000001104087830.phone: 88001000800" in flat
    assert "review.264728803.date: 2026-08-04" in flat
    assert gis2.parse_reviews(flat) == page


def test_phone_written_the_way_the_evidence_check_recognises_phones():
    page = gis2.parse_reviews_html(GRACE_REVIEWS)
    assert page.card.phones == ["78125619110", "78124263900"] and page.card.sites == ["http://gracetrade.ru"]
    flat = gis2.flatten_reviews(page)
    assert "card.70000001065972795.phone: +78125619110" in flat  # `7…` of 11 digits gets the plus
    assert "card.70000001065972795.site: http://gracetrade.ru" in flat
    assert gis2.parse_reviews(flat) == page  # …and the plus is gone again on reading
    from src.enrich.evidence import value_in_page
    assert value_in_page("phone", "78125619110", flat) and value_in_page("phone", "+7 (812) 426-39-00", flat)


def test_a_review_with_line_breaks_becomes_one_line_of_the_flat_text():
    page = gis2.parse_reviews_html(ROSTELECOM)
    assert "Если есть возможность подключить другого провайдера" in page.reviews[0].text
    assert all("\n" not in r.text and "  " not in r.text for r in page.reviews)  # whitespace squashed
    flat = gis2.flatten_reviews(page)
    assert all(line.startswith(("card.", "review.")) for line in flat.splitlines() if line)
    assert gis2.parse_reviews(flat) == page


def test_urls_are_the_real_pages_of_the_site():
    assert gis2.search_url("spb", "Сатурн ТД") == "https://2gis.ru/spb/search/%D0%A1%D0%B0%D1%82%D1%83%D1%80%D0%BD%20%D0%A2%D0%94"
    assert gis2.reviews_url("spb", ROSTELECOM_ID) == f"https://2gis.ru/spb/firm/{ROSTELECOM_ID}/tab/reviews"
    with pytest.raises(ValueError):
        gis2.reviews_url("spb", "../etc")
    with pytest.raises(ValueError):
        gis2.search_url("Москва; drop", "x")


# --- the client ---------------------------------------------------------------------------------

class Site:
    """A stub of 2gis.ru: robots.txt, a search page and a reviews tab; counts the requests."""

    def __init__(self, search=SATURN, reviews=ROSTELECOM, status=200, robots=ROBOTS):
        self.search, self.reviews, self.status, self.robots = search, reviews, status, robots
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        self.paths.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        if self.status != 200:
            return httpx.Response(self.status, text="")
        if "/search/" in path:
            return httpx.Response(200, text=self.search)
        return httpx.Response(200, text=self.reviews)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(gis2, "PACER", HostPacer(0.0, gis2.BLOCK_COOLDOWN_S))
    return SourceCache(tmp_path / "sources.sqlite")


def test_search_goes_through_the_cache_and_stores_flat_text(cache):
    site = Site()
    cards = gis2.fetch_search("spb", "Сатурн", cache=cache, client=site.client())
    assert len(cards) == 3
    cached = cache.page_text(gis2.search_url("spb", "Сатурн"))
    assert cached is not None and cached.startswith("card.") and "<html" not in cached
    again = gis2.fetch_search("spb", "Сатурн", cache=cache, client=site.client())
    assert again == cards
    assert [p for p in site.paths if "/search/" in p] == [site.paths[1]]  # robots.txt first, the search once


def test_reviews_fetch_returns_card_and_reviews(cache):
    site = Site()
    page = gis2.fetch_reviews("spb", ROSTELECOM_ID, cache=cache, client=site.client())
    assert page.card.id == ROSTELECOM_ID and len(page.reviews) == 4
    assert cache.page_text(gis2.reviews_url("spb", ROSTELECOM_ID)).count("review.") >= 4


def test_search_404_is_a_valid_empty_answer_and_is_cached(cache):
    site = Site(status=404)
    assert gis2.fetch_search("spb", "zzqx", cache=cache, client=site.client()) == []
    assert gis2.fetch_search("spb", "zzqx", cache=cache, client=site.client()) == []
    assert sum("/search/" in p for p in site.paths) == 1


def test_reviews_404_is_not_an_empty_answer(cache):
    with pytest.raises(httpx.HTTPStatusError):
        gis2.fetch_reviews("spb", ROSTELECOM_ID, cache=cache, client=Site(status=404).client())


def test_a_path_that_robots_txt_forbids_is_never_requested(cache):
    site = Site(robots="User-agent: *\nDisallow: /spb/search/\n")
    with pytest.raises(gis2.RobotsDisallowed):
        gis2.fetch_search("spb", "Сатурн", cache=cache, client=site.client())
    assert site.paths == ["/robots.txt"]


def test_refusal_pauses_the_host_and_the_next_call_sends_nothing(cache):
    site = Site(status=403)
    with pytest.raises(gis2.Blocked):
        gis2.fetch_search("spb", "Сатурн", cache=cache, client=site.client())
    sent = len(site.paths)
    with pytest.raises(gis2.Paused):
        gis2.fetch_search("spb", "Альтера", cache=cache, client=site.client())
    assert len(site.paths) == sent


def test_protection_page_with_200_pauses_the_host_too(cache):
    site = Site(search="<html><title>Вы не робот?</title></html>")
    with pytest.raises(gis2.Blocked):
        gis2.fetch_search("spb", "Сатурн", cache=cache, client=site.client())
    assert gis2.blocked()


def test_requests_are_paced(cache, monkeypatch):
    slept: list[float] = []
    clock = iter(range(1000, 2000))
    monkeypatch.setattr(gis2, "PACER", HostPacer(3.0, 0.0, clock=lambda: float(next(clock)), sleep=slept.append))
    site = Site()
    gis2.fetch_search("spb", "Сатурн", cache=cache, client=site.client())
    gis2.fetch_reviews("spb", ROSTELECOM_ID, cache=cache, client=site.client())
    # robots.txt, the search and the reviews tab are three requests to one host: each next one waited
    assert len(slept) == 2 and all(s > 0 for s in slept)


# --- manners of the client: what stops it, what it never does ----------------------------------------

def test_429_pauses_the_host_like_403(cache):
    with pytest.raises(gis2.Blocked):
        gis2.fetch_search("spb", "Сатурн", cache=cache, client=Site(status=429).client())
    assert gis2.blocked()


def test_a_valid_looking_page_from_another_host_is_not_read(cache):
    def to_foreign(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example":
            return httpx.Response(200, text=SATURN)  # a perfectly parsable page — from the wrong host
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(302, headers={"Location": "https://evil.example/spb/search/x"}, request=request)

    foreign = httpx.Client(transport=httpx.MockTransport(to_foreign), follow_redirects=True)
    with pytest.raises(gis2.Blocked):
        gis2.fetch_search("spb", "Сатурн", cache=cache, client=foreign)
    assert gis2.blocked()
    assert cache.get(gis2.search_url("spb", "Сатурн")) is None


def test_a_redirect_to_another_host_or_a_captcha_page_is_a_refusal(cache):
    def to_captcha(request: httpx.Request) -> httpx.Response:
        if request.url.host == "captcha.2gis.ru":
            return httpx.Response(200, text="Подтвердите, что вы не робот")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(302, headers={"Location": "https://captcha.2gis.ru/check"}, request=request)

    foreign = httpx.Client(transport=httpx.MockTransport(to_captcha), follow_redirects=True)
    with pytest.raises(gis2.Blocked):
        gis2.fetch_search("spb", "Сатурн", cache=cache, client=foreign)
    assert gis2.blocked()


def test_a_timeout_pauses_the_host_and_the_next_call_sends_nothing(cache, monkeypatch):
    monkeypatch.setattr(gis2, "DOWN_COOLDOWN_S", 600.0)

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no answer", request=request)

    with pytest.raises(httpx.ConnectTimeout):
        gis2.fetch_search("spb", "Сатурн", cache=cache, client=httpx.Client(transport=httpx.MockTransport(dead)))
    site = Site()
    with pytest.raises(gis2.Paused):
        gis2.fetch_search("spb", "Альтера", cache=cache, client=site.client())
    assert site.paths == []


def test_robots_txt_is_asked_before_the_reviews_tab_too(cache):
    site = Site(robots="User-agent: *\nDisallow: /spb/firm/\n")
    with pytest.raises(gis2.RobotsDisallowed):
        gis2.fetch_reviews("spb", ROSTELECOM_ID, cache=cache, client=site.client())
    assert site.paths == ["/robots.txt"]


def test_a_card_other_than_the_one_asked_for_is_an_error_not_an_answer(cache):
    site = Site(reviews=GRACE_REVIEWS)  # the page of another card comes back
    with pytest.raises(ValueError, match="вместо карточки"):
        gis2.fetch_reviews("spb", ROSTELECOM_ID, cache=cache, client=site.client())
    assert cache.get(gis2.reviews_url("spb", ROSTELECOM_ID)) is None  # and it is not cached as the answer


def test_hidden_reviews_and_reviews_without_text_or_date_are_skipped():
    html = GRACE_REVIEWS
    state = gis2._state(html, gis2._QUERY_STATE)
    assert state is not None
    items = state["queries"][0]["state"]["data"]["pages"][0]["items"]
    base = items[0]
    items[:] = [
        dict(base, id="1", is_hidden=True),  # hidden by 2GIS
        dict(base, id="2", text="   "),  # no text
        dict(base, id="3", date_created="вчера"),  # no date
        dict(base, id="4", rating=9),  # a rating outside 1–5 is dropped, the review is not
        dict(base, id="5", text="Нормальный отзыв"),
    ]
    literal = json.dumps(state, ensure_ascii=False, separators=(",", ":")).replace("\\", "\\\\").replace("'", "\\'")
    head = html[:html.index(gis2._QUERY_STATE)]
    page = gis2.parse_reviews_html(head + gis2._QUERY_STATE + literal + "');</script></html>")
    assert [(r.id, r.rating) for r in page.reviews] == [("4", None), ("5", 5)]


def test_a_page_without_a_list_of_reviews_is_blocked_not_read_as_no_reviews():
    html = GRACE_REVIEWS.replace('"items"', '"entries"')  # the markup changed under us
    with pytest.raises(gis2.Blocked, match="нет списка отзывов"):
        gis2.parse_reviews_html(html)
    assert gis2.parse_reviews_html(EMPTY).reviews == []  # an empty list IS an answer


def test_emails_of_the_card_are_read_lower_cased_and_survive_the_flat_text():
    html = GRACE_REVIEWS.replace('"contacts":[', '"contacts":[{"type":"email","value":"Info@GraceTrade.RU"},', 1)
    page = gis2.parse_reviews_html(html)
    assert page.card.emails == ["info@gracetrade.ru"]
    assert "card.70000001065972795.email: info@gracetrade.ru" in gis2.flatten_reviews(page)
    assert gis2.parse_reviews(gis2.flatten_reviews(page)) == page


def test_the_operator_switch_stops_every_request(cache, monkeypatch):
    monkeypatch.setenv("GIS2_DISABLED", "yes")
    site = Site()
    with pytest.raises(gis2.Paused, match="GIS2_DISABLED"):
        gis2.fetch_search("spb", "Сатурн", cache=cache, client=site.client())
    assert site.paths == []


def _reviews_page_with(edit) -> str:
    """The recorded Grace tab after `edit(queries_state)`, written back with the escaping of the site."""
    html = GRACE_REVIEWS
    state = gis2._state(html, gis2._QUERY_STATE)
    assert state is not None
    edit(state)
    literal = json.dumps(state, ensure_ascii=False, separators=(",", ":")).replace("\\", "\\\\").replace("'", "\\'")
    return html[:html.index(gis2._QUERY_STATE)] + gis2._QUERY_STATE + literal + "');</script></html>"


def test_reviews_asked_for_another_object_of_the_page_are_not_this_cards_reviews():
    def other_object(state):
        state["queries"][0]["queryKey"][1][0] = "70000001999999999"

    with pytest.raises(gis2.Blocked, match="нет списка отзывов"):  # nothing of THIS card is left: not «no reviews»
        gis2.parse_reviews_html(_reviews_page_with(other_object))


def test_a_review_that_says_it_is_about_another_card_is_skipped():
    def mixed(state):
        items = state["queries"][0]["state"]["data"]["pages"][0]["items"]
        items.append(dict(items[0], id="999", object={"id": "70000001999999999", "type": "branch"}))

    page = gis2.parse_reviews_html(_reviews_page_with(mixed))
    assert [r.id for r in page.reviews] == [r.id for r in gis2.parse_reviews_html(GRACE_REVIEWS).reviews]
