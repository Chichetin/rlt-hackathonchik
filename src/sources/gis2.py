"""2ГИС (2gis.ru): cards of organizations and their reviews, ON DEMAND, one supplier at a time (#234, D-112).
Owner: SOURCES.

What we read, and only this (robots.txt of 2gis.ru allows both paths; `robots.allowed` is asked before each request):
- `/<city>/search/<brand>` — the search page. 2GIS renders the result into the page itself
  (`var initialState = JSON.parse('…')`): name, address, rubrics, the card rating and number of reviews;
- `/<city>/firm/<id>/tab/reviews` — the reviews tab of ONE card: the same state plus the first reviews
  (`__REACT_QUERY_STATE__`, query `fetchEntityReviews`) and the phone / site of the card.
2GIS does NOT publish the INN, so a card is never «this company» by itself: src/enrich/reviews.py
decides how far a card can be trusted. Authors of reviews are private persons: their name and profile
are never read, never cached.

Cache (src/sources/cache.py): the page itself is ~600 KB of markup, so what goes under the page URL is
the flat text `card.<id>.<field>: <value>` / `review.<id>.<field>: <value>` — the values verbatim
(only JS/JSON escaping removed, line breaks of a review squashed to spaces). src/enrich/evidence.py
checks every shown quote against this text, the same way as for rmsp (src/sources/rmsp.py).

Manners: robots.txt via the cache; one request at a time with a pause (`GIS2_MIN_INTERVAL_S`, 3 s);
403 / 429 / a protection page / a page without the state → `Blocked`: the host is left alone for
BLOCK_COOLDOWN_S and nothing is done to get around it; a timeout pauses it for DOWN_COOLDOWN_S.
A search that finds nothing is an HTTP 404 on 2gis.ru: a valid empty answer, cached like any other.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from src.sources import robots
from src.sources.cache import SourceCache
from src.sources.pacing import HostPacer

BASE = "https://2gis.ru"
SOURCE = "2ГИС"
UA = "Mozilla/5.0 (compatible; RLTHack-Reviews/0.1; hackathon, cached, paced)"
MIN_INTERVAL_S = float(os.environ.get("GIS2_MIN_INTERVAL_S", "3.0"))
TIMEOUT_S = float(os.environ.get("GIS2_TIMEOUT_S", "20.0"))
BLOCK_COOLDOWN_S = 900.0
DOWN_COOLDOWN_S = float(os.environ.get("GIS2_DOWN_COOLDOWN_S", "90"))
MAX_BODY = 4_000_000  # a card page is ~600 KB; anything much bigger is not what we asked for

_CITY = re.compile(r"[a-z][a-z_]{1,29}", re.ASCII)
_ID = re.compile(r"\d{5,20}", re.ASCII)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_INITIAL = "var initialState = JSON.parse('"
_QUERY_STATE = "var __REACT_QUERY_STATE__ = JSON.parse('"

# MIN_INTERVAL_S is read at every request (a test or an operator may override the module value)
PACER = HostPacer(lambda: MIN_INTERVAL_S, BLOCK_COOLDOWN_S)


class Blocked(RuntimeError):
    """The host refuses us, or the page is not what 2GIS serves (protection / changed markup): stop, do not bypass."""


class Paused(Blocked):
    """No request was made: the pause after a refusal or a timeout is still on."""


class RobotsDisallowed(RuntimeError):
    """robots.txt of the site forbids the path: it is not requested."""


@dataclass(frozen=True)
class Card:
    id: str
    name: str
    primary: str | None  # the brand without the kind of business: «Сатурн» of «Сатурн, строительный гипермаркет»
    legal_name: str | None  # «ООО Грейс трэйд» — only when the card says so
    org_id: str | None  # the organization; its branches share it
    address: str | None
    rubrics: list[str]
    rating: float | None  # the rating of THIS card, as 2GIS shows it
    review_count: int | None
    phones: list[str]  # digits, as in `value` of the contact
    sites: list[str]
    emails: list[str] = field(default_factory=list)  # lower case

    @property
    def brand(self) -> str:
        return self.primary or self.name


@dataclass(frozen=True)
class Review:
    id: str
    date: str  # YYYY-MM-DD, the day the review was written
    rating: int | None
    text: str


@dataclass(frozen=True)
class ReviewsPage:
    card: Card
    reviews: list[Review]


# --- addresses ---------------------------------------------------------------------------------

def _city(city: str) -> str:
    if not _CITY.fullmatch(city):
        raise ValueError(f"2GIS: недопустимый город {city!r}")
    return city


def search_url(city: str, query: str) -> str:
    query = " ".join(query.split())
    if not query or len(query) > 120:
        raise ValueError("2GIS: пустой или слишком длинный запрос")
    return f"{BASE}/{_city(city)}/search/{quote(query, safe='')}"


def reviews_url(city: str, branch_id: str) -> str:
    if not _ID.fullmatch(branch_id):
        raise ValueError(f"2GIS: недопустимый id карточки {branch_id!r}")
    return f"{BASE}/{_city(city)}/firm/{branch_id}/tab/reviews"


# --- the state embedded in the page ------------------------------------------------------------

_ESC = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def unescape_js(literal: str) -> str:
    """The text of a single-quoted JavaScript string literal (without the quotes) → the string it denotes."""
    def one(m: re.Match[str]) -> str:
        g = m.group(1)
        return chr(int(g[1:], 16)) if g[0] in "ux" and len(g) > 1 else _SIMPLE.get(g, g)

    return _ESC.sub(one, literal)


def _state(html: str, marker: str) -> dict | None:
    """`JSON.parse('<literal>')` after `marker`: parsed, or None when the page has no such assignment."""
    start = html.find(marker)
    if start < 0:
        return None
    start += len(marker)
    pos = start
    while True:  # the closing quote is the first one not preceded by an odd number of backslashes
        end = html.find("'", pos)
        if end < 0:
            return None
        back = end
        while back > start and html[back - 1] == "\\":
            back -= 1
        if (end - back) % 2 == 0:
            break
        pos = end + 1
    try:
        state = json.loads(unescape_js(html[start:end]))
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def _squash(text: str) -> str:
    return " ".join(text.split())


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _obj(d: dict, key: str) -> dict:
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def _items(d: dict, key: str) -> list[dict]:
    v = d.get(key)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _card(d: dict) -> Card | None:
    cid = d.get("id")
    name = d.get("name")
    if not isinstance(cid, str) or not _ID.fullmatch(cid) or not isinstance(name, str) or not name.strip():
        return None
    org, name_ex, rv = _obj(d, "org"), _obj(d, "name_ex"), _obj(d, "reviews")
    rating, count = rv.get("general_rating"), rv.get("general_review_count")
    phones: list[str] = []
    sites: list[str] = []
    emails: list[str] = []
    for group in _items(d, "contact_groups"):
        for c in _items(group, "contacts"):
            kind = c.get("type")
            if kind == "phone" and _digits(str(c.get("value") or c.get("text") or "")):
                phones.append(_digits(str(c.get("value") or c.get("text"))))
            elif kind == "website":
                site = str(c.get("url") or c.get("value") or c.get("text") or "").strip()
                if site:
                    sites.append(site)
            elif kind == "email":
                email = str(c.get("value") or c.get("text") or "").strip().lower()
                if "@" in email:
                    emails.append(email)
    return Card(
        id=cid,
        name=_squash(name),
        primary=_squash(str(org.get("primary") or name_ex.get("primary") or "")) or None,
        legal_name=_squash(str(name_ex.get("legal_name") or "")) or None,
        org_id=str(org["id"]) if org.get("id") else None,
        address=_squash(str(d.get("address_name") or "")) or None,
        rubrics=[_squash(r["name"]) for r in d.get("rubrics") or [] if isinstance(r, dict) and r.get("name")],
        rating=float(rating) if isinstance(rating, int | float) and not isinstance(rating, bool) else None,
        review_count=count if isinstance(count, int) and not isinstance(count, bool) else None,
        phones=list(dict.fromkeys(phones)),
        sites=list(dict.fromkeys(sites)),
        emails=list(dict.fromkeys(emails)),
    )


def _profiles(state: dict) -> list[Card]:
    profiles = _obj(_obj(_obj(state, "data"), "entity"), "profile")
    cards = []
    for node in profiles.values():
        d = _obj(node, "data") if isinstance(node, dict) else {}
        card = _card(d) if d.get("type") == "branch" else None
        if card is not None:
            cards.append(card)
    return cards


def parse_search_html(html: str) -> list[Card]:
    state = _state(html, _INITIAL)
    if state is None:
        raise Blocked("2gis.ru: в странице выдачи нет данных (страница защиты или разметка изменилась)")
    return _profiles(state)


def _review(item: dict) -> Review | None:
    rid, text, day = item.get("id"), item.get("text"), str(item.get("date_created") or "")[:10]
    if item.get("is_hidden") or not isinstance(rid, str | int) or not isinstance(text, str):
        return None
    text = _squash(text)
    rating = item.get("rating")
    if not text or not _DATE.fullmatch(day):
        return None
    ok = isinstance(rating, int) and not isinstance(rating, bool) and 1 <= rating <= 5
    return Review(id=str(rid), date=day, rating=rating if ok else None, text=text)  # no author: never read


def parse_reviews_html(html: str) -> ReviewsPage:
    state, queries = _state(html, _INITIAL), _state(html, _QUERY_STATE)
    cards = _profiles(state) if state is not None else []
    if not cards or queries is None:
        raise Blocked("2gis.ru: в странице отзывов нет данных (страница защиты или разметка изменилась)")
    reviews: dict[str, Review] = {}
    saw_list = False  # a list of reviews (possibly empty) was found: only then «no reviews» is an answer
    card_id = cards[0].id
    for q in _items(queries, "queries"):
        key = q.get("queryKey")
        if not (isinstance(key, list) and key and key[0] == "fetchEntityReviews"):
            continue
        asked = key[1] if len(key) > 1 and isinstance(key[1], list) and key[1] else None
        if asked is not None and str(asked[0]) != card_id:  # reviews of some other object of the page
            continue
        for page in _items(_obj(_obj(q, "state"), "data"), "pages"):
            if isinstance(page.get("items"), list):
                saw_list = True
            for item in _items(page, "items"):
                owner = _obj(item, "object").get("id")
                if owner is not None and str(owner) != card_id:  # a review that says it is about another card
                    continue
                review = _review(item)
                if review is not None:
                    reviews.setdefault(review.id, review)
    if not saw_list:
        raise Blocked("2gis.ru: в странице нет списка отзывов (разметка изменилась или защита)")
    return ReviewsPage(card=cards[0], reviews=list(reviews.values()))


# --- the flat text that goes to the cache ------------------------------------------------------

def _phone_text(digits: str) -> str:
    """A number as the Evidence check recognises a phone: `7…` of 11 digits needs the plus (src/enrich/evidence.py).
    The same digits as `value` of the contact on the page, written the way phones are written."""
    return f"+{digits}" if len(digits) == 11 and digits.startswith("7") else digits


def _card_lines(c: Card) -> list[str]:
    lines = [f"card.{c.id}.name: {c.name}"]
    for name, value in (("primary", c.primary), ("legal_name", c.legal_name), ("org_id", c.org_id),
                         ("address", c.address), ("rubrics", "; ".join(c.rubrics) or None),
                         ("rating", None if c.rating is None else repr(c.rating)),
                         ("review_count", None if c.review_count is None else str(c.review_count))):
        if value:
            lines.append(f"card.{c.id}.{name}: {value}")
    lines += [f"card.{c.id}.phone: {_phone_text(p)}" for p in c.phones]
    lines += [f"card.{c.id}.site: {s}" for s in c.sites]
    lines += [f"card.{c.id}.email: {e}" for e in c.emails]
    return lines


def flatten_search(cards: list[Card]) -> str:
    return "".join(line + "\n" for c in cards for line in _card_lines(c))


def flatten_reviews(page: ReviewsPage) -> str:
    lines = _card_lines(page.card)
    for r in page.reviews:
        lines.append(f"review.{r.id}.date: {r.date}")
        if r.rating is not None:
            lines.append(f"review.{r.id}.rating: {r.rating}")
        lines.append(f"review.{r.id}.text: {r.text}")
    return "".join(line + "\n" for line in lines)


def _fields(flat: str, prefix: str) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    for line in flat.splitlines():
        key, sep, value = line.partition(": ")
        parts = key.split(".")
        if sep and len(parts) == 3 and parts[0] == prefix:
            out.setdefault(parts[1], {}).setdefault(parts[2], []).append(value)
    return out


def _card_from_fields(cid: str, f: dict[str, list[str]]) -> Card:
    def one(name: str) -> str | None:
        return f[name][0] if f.get(name) else None

    rating, count = one("rating"), one("review_count")
    return Card(
        id=cid, name=one("name") or "", primary=one("primary"), legal_name=one("legal_name"), org_id=one("org_id"),
        address=one("address"), rubrics=[r for r in (one("rubrics") or "").split("; ") if r],
        rating=float(rating) if rating else None, review_count=int(count) if count else None,
        phones=[_digits(p) for p in f.get("phone", [])], sites=f.get("site", []), emails=f.get("email", []),
    )


def parse_search(flat: str) -> list[Card]:
    return [_card_from_fields(cid, f) for cid, f in _fields(flat, "card").items()]


def parse_reviews(flat: str) -> ReviewsPage:
    cards = parse_search(flat)
    if not cards:
        raise ValueError("2gis.ru: в сохранённой странице отзывов нет карточки")
    reviews = []
    for rid, f in _fields(flat, "review").items():
        rating = f["rating"][0] if f.get("rating") else None
        reviews.append(Review(id=rid, date=f["date"][0], rating=int(rating) if rating else None, text=f["text"][0]))
    return ReviewsPage(card=cards[0], reviews=reviews)


# --- the client --------------------------------------------------------------------------------

def disabled() -> bool:
    """`GIS2_DISABLED=1`: an operator switched the reading of 2gis.ru off (read at every call, like PB_DISABLED)."""
    return os.environ.get("GIS2_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def blocked() -> bool:
    """True while the host is paused: after a refusal (15 min) or after a timeout (DOWN_COOLDOWN_S)."""
    return PACER.blocked()


def _block(reason: str) -> Blocked:
    PACER.block()
    return Blocked(f"2gis.ru: {reason} — останавливаемся, защиту не обходим")


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, follow_redirects=True,
                        headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})


def _get(client: httpx.Client, url: str) -> httpx.Response:
    if disabled():
        raise Paused("2gis.ru: чтение отключено (GIS2_DISABLED)")
    if blocked():
        raise Paused("2gis.ru: пауза после отказа или таймаута источника ещё не истекла")
    with PACER.slot(timeout=TIMEOUT_S) as limit:
        try:
            r = client.get(url, timeout=limit)
        except httpx.TransportError:
            PACER.block(DOWN_COOLDOWN_S)  # timeout / no connection: leave the host alone for a while
            raise
    if r.url.host not in ("2gis.ru", "www.2gis.ru") or "captcha" in str(r.url):
        raise _block("запрос перенаправлен на страницу защиты")
    if r.status_code in (403, 429):
        raise _block(f"HTTP {r.status_code}")
    if len(r.content) > MAX_BODY:
        raise _block("ответ слишком большой для карточки")
    return r


def _robots_checked(client: httpx.Client, cache: SourceCache, url: str) -> None:
    def live(u: str) -> tuple[int, str]:
        r = _get(client, u)
        if r.status_code in (404, 410):
            return r.status_code, ""  # no robots.txt: nothing is forbidden
        r.raise_for_status()
        return r.status_code, r.text

    text = cache.fetch(BASE + "/robots.txt", live).body
    if not robots.allowed(text, url[len(BASE):], UA):
        raise RobotsDisallowed(f"robots.txt 2gis.ru запрещает {url[len(BASE):]}")


def fetch_search(city: str, query: str, cache: SourceCache | None = None,
                 client: httpx.Client | None = None) -> list[Card]:
    """The cards the search for `query` shows in `city` (a cached answer, or one live GET)."""
    url, cache = search_url(city, query), cache or SourceCache()
    own = client or _client()

    def live(u: str) -> tuple[int, str]:
        r = _get(own, u)
        if r.status_code == 404:  # 2gis.ru answers 404 to a search that finds nothing
            return 404, ""
        r.raise_for_status()
        try:
            return 200, flatten_search(parse_search_html(r.text))
        except Blocked:
            raise _block("в странице выдачи нет данных") from None

    try:
        hit = cache.get(url)
        if hit is None:
            _robots_checked(own, cache, url)
        page = hit or cache.fetch(url, live)
    finally:
        if client is None:
            own.close()
    return parse_search(page.body)


def fetch_reviews(city: str, branch_id: str, cache: SourceCache | None = None,
                  client: httpx.Client | None = None) -> ReviewsPage:
    """The reviews tab of one card: its first reviews, phone and site (a cached answer, or one live GET)."""
    url, cache = reviews_url(city, branch_id), cache or SourceCache()
    own = client or _client()

    def live(u: str) -> tuple[int, str]:
        r = _get(own, u)
        r.raise_for_status()
        try:
            page = parse_reviews_html(r.text)
        except Blocked:
            raise _block("в странице отзывов нет данных") from None
        if page.card.id != branch_id:
            raise ValueError(f"2gis.ru: вместо карточки {branch_id} пришла {page.card.id}")
        return 200, flatten_reviews(page)

    try:
        hit = cache.get(url)
        if hit is None:
            _robots_checked(own, cache, url)
        page = hit or cache.fetch(url, live)
    finally:
        if client is None:
            own.close()
    return parse_reviews(page.body)
