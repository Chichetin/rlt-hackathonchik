"""Reviews of a supplier from 2ГИС, on demand: a quote, its date and a link — or nothing (#234, D-142). Owner: SOURCES.

2GIS does not publish the INN. A card found by the brand is therefore NOT «the company» by itself: the
company «САТУРН» of the МСП register (stationery) and the building-materials chain «Сатурн» share a name and a
city. How far a card is trusted is the `basis`, from the strongest to the weakest:

- `phone` / `email` / `site` — a phone, an email or a site of the card equals the company's own
  (`Counterparty.contacts`, each with its Evidence from the registry): confirmed, `verified=True`;
- `legal_name` — the card shown (the branch of the organization that itself names the legal entity: «ООО Грейс трэйд»)
  equals ours AND no other organization of the whole search result carries the same legal name (several different
  «ООО Сатурн» exist): the quotes are shown, marked «не подтверждено по ИНН»;
- `name` — only the brand is equal, or the legal name is shared by several organizations: the card is listed (the user
  sees its sphere and address and can open it), but no quote, no rating and no number of reviews is shown until a
  contact confirms it.

Every shown fact is an Evidence on a cached page (src/sources/gis2.py keeps the flat text); a quote is a
literal prefix of the review, and `check_evidence` runs on EVERY Evidence before it is returned: what the page
does not contain never reaches the user. Reviews are not in the score and do not touch the rating.
Individual entrepreneurs are not searched (a natural person), nor are companies with no name or outside СПб/ЛО.

`python -m src.enrich.reviews 'ООО "ГРЕЙС ТРЭЙД"' [--phone … --email … --site …]` prints the result (JSON).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from src.contracts import Counterparty, Evidence
from src.enrich import evidence as check
from src.sources import gis2
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)

MAX_CARDS = 3  # organizations with this brand we are willing to show
MAX_QUOTES = 3  # newest reviews per card
MAX_QUOTE_CHARS = 240
MIN_BRAND = 3  # a shorter name finds everything and proves nothing
MAX_BRAND = 100
CITY = {"Санкт-Петербург": "spb", "Ленинградская область": "spb"}  # 2GIS city of the supplier's region
KPP_REGION = {"78": "Санкт-Петербург", "47": "Ленинградская область"}
DISCLAIMER = (
    "Отзывы с 2ГИС, как их видит любой посетитель сайта. 2ГИС не публикует ИНН: принадлежность карточки компании "
    "подтверждена только там, где так и написано. В подбор и рейтинг отзывы не входят."
)

Basis = Literal["phone", "email", "site", "legal_name", "name"]
TIER: dict[str, int] = {"phone": 0, "email": 0, "site": 0, "legal_name": 1, "name": 2}


class ReviewQuote(BaseModel):
    text: str = Field(description="начало отзыва дословно; «…» в конце — отзыв длиннее")
    rating: int | None = Field(description="оценка автора, 1–5; автор не показывается")
    date: str = Field(description="дата отзыва, YYYY-MM-DD")
    evidence: Evidence


class ReviewCard(BaseModel):
    branch_id: str
    title: str
    address: str | None
    rubrics: list[str]
    url: str = Field(description="вкладка отзывов карточки на 2gis.ru")
    rating: float | None = Field(
        description="рейтинг карточки, как его показывает 2ГИС; пусто, пока нет подтверждения контактом (basis=name)")
    review_count: int | None
    basis: Basis
    verified: bool = Field(description="True только при совпадении телефона, email или сайта с данными компании")
    note: str = Field(description="что именно совпало и чего не хватает")
    quotes: list[ReviewQuote]
    evidence: list[Evidence]


class ReviewsResult(BaseModel):
    inn: str | None
    status: Literal["ok", "not_found", "unsupported", "unavailable"]
    message: str | None = None
    queried: str | None = Field(default=None, description="по какому названию искали")
    cards: list[ReviewCard] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


# --- who is searched ----------------------------------------------------------------------------

_FULL_FORMS = {
    "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ": "ООО",
    "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО": "ПАО",
    "НЕПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО": "НАО",
    "ЗАКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО": "ЗАО",
    "ОТКРЫТОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО": "ОАО",
    "АКЦИОНЕРНОЕ ОБЩЕСТВО": "АО",
}
_ABBREVIATIONS = {"ООО", "ПАО", "НАО", "ЗАО", "ОАО", "АО"}
_QUOTED = re.compile(r"[«\"“„](.+?)[»\"”“]")


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", " ", s.casefold().replace("ё", "е")).strip()


def _form(head: str) -> str | None:
    head = " ".join(head.upper().split())
    return _FULL_FORMS.get(head) or (head if head in _ABBREVIATIONS else None)


def _split_form(text: str) -> tuple[str, str]:
    """`ООО Ромашка` → (`ООО`, `Ромашка`); no known form at the start → (``, the whole text)."""
    upper = text.upper()
    for full in sorted([*_FULL_FORMS, *_ABBREVIATIONS], key=len, reverse=True):
        if upper.startswith(full + " "):
            return text[:len(full)], text[len(full):].strip()
    return "", text


def brand_of(legal_name: str) -> tuple[str | None, str] | None:
    """The form and the brand of a legal name: `ООО "ГРЕЙС ТРЭЙД"` → (`ООО`, `ГРЕЙС ТРЭЙД`). None — nothing to search:
    an individual entrepreneur, a placeholder `ИНН …`, only a form, or a brand too short or too long."""
    text = " ".join(legal_name.split())
    upper = text.upper()
    if not text or upper.startswith(("ИП ", "ИНН ", "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ")):
        return None
    quoted = _QUOTED.search(text)
    if quoted:
        head, brand = text[:quoted.start()], quoted.group(1).strip()
    else:
        head, brand = _split_form(text)
    if brand.upper() in _ABBREVIATIONS or not MIN_BRAND <= len(_norm(brand)) <= MAX_BRAND:
        return None
    return _form(head), brand


def _unsupported(cp: Counterparty) -> str | None:
    if cp.inn is not None and len(cp.inn) == 12:
        return "Индивидуальный предприниматель — физическое лицо: отзывы о нём не ищем."
    if brand_of(cp.name) is None:
        return "Название компании не определено или слишком общее — по нему нельзя искать отзывы."
    if _region(cp) is None:
        return "Поиск отзывов настроен для Санкт-Петербурга и Ленинградской области."
    return None


def _region(cp: Counterparty) -> str | None:
    """СПб or ЛО: by the region of the card, else by the region code of the KPP."""
    if cp.region in CITY:
        return cp.region
    return KPP_REGION.get(cp.kpp[:2]) if cp.kpp else None


# --- which card is the company's ------------------------------------------------------------------

def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


# Pages of platforms are not the company's own site: the same host would «confirm» any card that links to it.
PLATFORM_HOSTS = frozenset({
    "vk.com", "vk.ru", "t.me", "telegram.me", "ok.ru", "facebook.com", "instagram.com", "youtube.com", "wa.me",
    "wildberries.ru", "ozon.ru", "avito.ru", "market.yandex.ru", "yandex.ru", "google.com", "2gis.ru", "hh.ru",
})


def _host(url: str) -> str | None:
    host = urlsplit(url if "//" in url else "//" + url).hostname
    return host.lower().removeprefix("www.") if host else None


def _own_site_host(url: str) -> str | None:
    host = _host(url)
    return None if host is None or host in PLATFORM_HOSTS else host


def _same_brand(card: gis2.Card, brand_norm: str) -> bool:
    return _norm(card.brand) == brand_norm


def _legal_matches(card: gis2.Card, form: str | None, brand_norm: str) -> bool:
    legal = brand_of(card.legal_name) if card.legal_name else None
    if legal is None or _norm(legal[1]) != brand_norm:
        return False
    return form is not None and legal[0] == form  # a form on both sides and the same: «АО Сатурн» ≠ «ООО САТУРН»


def _org_key(card: gis2.Card) -> str:
    return card.org_id or card.id


def _candidates(found: list[gis2.Card], form: str | None, brand_norm: str) -> list[gis2.Card]:
    """One card per organization — its branch that ITSELF names our legal entity, else the one with the most reviews —
    and the organizations that name our legal entity come first, so a chain with many branches cannot push them out of
    the first MAX_CARDS. (Branches of one organization may name different legal entities: a franchise.)"""
    best: dict[str, gis2.Card] = {}

    def rank(c: gis2.Card) -> tuple[bool, int]:
        return _legal_matches(c, form, brand_norm), c.review_count or 0

    for c in found:
        if not _same_brand(c, brand_norm):
            continue
        key = _org_key(c)
        if key not in best or rank(c) > rank(best[key]):
            best[key] = c
    ranked = sorted(best.values(), key=lambda c: not _legal_matches(c, form, brand_norm))  # stable
    return ranked[:MAX_CARDS]


def _legal_organizations(found: list[gis2.Card], form: str | None, brand_norm: str) -> set[str]:
    """The organizations of the WHOLE search result (before any cut) that carry our legal name."""
    return {_org_key(c) for c in found if _legal_matches(c, form, brand_norm)}


def _excerpt(text: str) -> tuple[str, str]:
    """(what the user sees, the literal prefix the Evidence names): a long review is cut at a word."""
    if len(text) <= MAX_QUOTE_CHARS:
        return text, text
    value = text[:MAX_QUOTE_CHARS].rsplit(" ", 1)[0].rstrip()
    return value + "…", value


def _pretty_phone(digits: str) -> str:
    return f"+{digits[0]} {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}" if len(digits) == 11 else digits


class _Page:
    """The cache as the Evidence check reads it, and the time each page was read."""

    def __init__(self, cache: SourceCache):
        self.cache = cache
        self.text = check.cached_pages(cache.page_text)

    def at(self, url: str) -> datetime:
        page = self.cache.get(url, allow_stale=True)
        if page is None:
            raise KeyError(url)
        return datetime.fromtimestamp(page.fetched_at, tz=UTC)


def _evidence(src: _Page, url: str, field: str, value: str, fact: str) -> Evidence | None:
    ev = Evidence(source=gis2.SOURCE, url=url, fetched_at=src.at(url), fact=fact, field=field, value=value)
    problems = check.check_evidence(ev, src.text)
    if problems:
        log.warning("reviews: факт не прошёл проверку Evidence и не показан: %s", problems[0])
        return None
    return ev


def _basis(card: gis2.Card, cp: Counterparty, unique_legal: bool) -> tuple[Basis, str]:
    """The strongest basis the card has, and the value on the card that proves it (`''` for the two weak ones)."""
    ours = _digits(cp.contacts.get("phone", ""))[-10:]
    if len(ours) == 10:
        for phone in card.phones:
            if phone[-10:] == ours:
                return "phone", phone
    our_email = cp.contacts.get("email", "").strip().lower()
    if "@" in our_email and our_email in card.emails:
        return "email", our_email
    our_site = _own_site_host(cp.contacts["site"]) if cp.contacts.get("site") else None
    if our_site:
        for site in card.sites:
            if _own_site_host(site) == our_site:
                return "site", site
    # the legal name is a basis only while it points at ONE organization of the result: «ООО Сатурн» is not unique
    return ("legal_name", "") if unique_legal else ("name", "")


def _other_legal_entity(card: gis2.Card) -> str:
    """How the legal entity of a card that is NOT ours is named to the user: a natural person (ИП) is not named."""
    legal = card.legal_name or ""
    return "индивидуальный предприниматель" if legal.upper().startswith(("ИП", "ИНДИВИДУАЛЬН")) else f"«{legal}»"


def _note(basis: Basis, card: gis2.Card, proof: str, legal_orgs: int, names_ours: bool, form_known: bool) -> str:
    """Why the card has this status — what really matched and what did not. `names_ours`: this very card names OUR
    legal entity; `form_known`: the legal form of the company is known (so the card's legal entity can be compared)."""
    if basis == "phone":
        return (f"Телефон из карточки ({_pretty_phone(proof)}) совпал с телефоном компании из реестра: "
                "карточка относится к ней.")
    if basis == "email":
        return f"Email из карточки ({proof}) совпал с email компании из реестра: карточка относится к ней."
    if basis == "site":
        return f"Сайт из карточки ({_host(proof)}) совпал с сайтом компании из реестра: карточка относится к ней."
    if basis == "legal_name":
        return (f"2ГИС указывает юрлицо «{card.legal_name}» — оно совпадает с названием компании, и в выдаче 2ГИС это "
                "юрлицо у одной организации. Телефон, email и сайт для сверки не совпали или неизвестны, а ИНН 2ГИС не "
                "публикует: проверьте адрес и сферу.")
    shown = ("Отзывы, рейтинг и число отзывов не показываются, пока карточку не подтвердит контакт; "
             "откройте её и проверьте адрес и сферу.")
    if names_ours and legal_orgs > 1:
        return (f"Юрлицо «{card.legal_name}» в выдаче 2ГИС у нескольких разных организаций ({legal_orgs}): "
                f"какая из них эта компания, по карточке не определить, ИНН 2ГИС не публикует. {shown}")
    if names_ours:  # the card names OUR legal entity, but its uniqueness in the result is not established
        return (f"Юрлицо «{card.legal_name}» в карточке совпадает с названием компании, но единственность этого "
                "юрлица в выдаче 2ГИС не подтверждена (в выдаче поиска оно у этой карточки не указано), "
                f"ИНН 2ГИС не публикует. {shown}")
    if card.legal_name and not form_known:
        return (f"2ГИС указывает юрлицо — {_other_legal_entity(card)}, а форму юрлица компании по её названию не "
                f"определить: сверить не с чем, ИНН 2ГИС не публикует. {shown}")
    if card.legal_name:
        return (f"2ГИС указывает другое юрлицо — {_other_legal_entity(card)}, оно не совпадает с названием компании; "
                f"название «{card.brand}» общее, ИНН 2ГИС не публикует. {shown}")
    return (f"Название «{card.brand}» в карточке есть, но юрлицо не указано, а ИНН 2ГИС не публикует — это может "
            f"быть другая организация. {shown}")


def _quotes(page: gis2.ReviewsPage, src: _Page, url: str) -> list[ReviewQuote]:
    out: list[ReviewQuote] = []
    for r in sorted(page.reviews, key=lambda r: r.date, reverse=True):
        if len(out) >= MAX_QUOTES:
            break
        if check.phones(r.text) or check.emails(r.text):  # a stranger's contact in a public review: not ours to repeat
            continue
        shown, value = _excerpt(r.text)
        ev = _evidence(src, url, "review", value, f"Отзыв от {r.date}: «{shown}»")
        if ev is not None:
            out.append(ReviewQuote(text=shown, rating=r.rating, date=r.date, evidence=ev))
    return out


def _build(card: gis2.Card, cp: Counterparty, form: str | None, brand: str, city: str, src: _Page,
           client: httpx.Client | None, legal: set[str]) -> tuple[ReviewCard | None, str | None, int]:
    """The card of the answer (None when none of its facts can be proved from a page), a message when its reviews
    could not be read, and the number of reviews 2GIS shows for it (for ordering only)."""
    tab = gis2.reviews_url(city, card.id)
    shown_count = card.review_count or 0
    brand_norm = _norm(brand)
    # the legal name confirms the card only when ONE organization of the whole search result carries it AND the card
    # that is shown names it itself (a franchise: another branch of the organization may name another legal entity)
    unique_legal = len(legal) == 1 and _legal_matches(card, form, brand_norm)
    page: gis2.ReviewsPage | None = None
    problem: str | None = None
    # The tab holds the reviews and the contacts of the card. With nothing to compare them with (the company has no
    # contact) and no unambiguous legal entity named by the card, nothing could be shown or confirmed: do not ask.
    comparable = bool(cp.contacts.get("phone") or cp.contacts.get("email") or cp.contacts.get("site")) or unique_legal
    if shown_count > 0 and comparable:
        try:
            page = gis2.fetch_reviews(city, card.id, src.cache, client)
        except (gis2.Blocked, gis2.RobotsDisallowed, httpx.HTTPError, ValueError) as e:
            log.warning("reviews: вкладка отзывов %s не прочитана: %s", card.id, e)
            problem = "Не удалось прочитать тексты отзывов: 2ГИС сейчас недоступен."
    full = page.card if page is not None else card
    names_ours = _legal_matches(full, form, brand_norm)
    unique_legal = unique_legal and names_ours  # the tab of the card is judged too
    read_from = tab if page is not None else gis2.search_url(city, brand)  # the page the card facts came from
    basis, proof = _basis(full, cp, unique_legal)
    evidence: list[Evidence] = []
    if proof:
        field, label = {"phone": ("phone", "Телефон +" + proof), "email": ("email", "Email " + proof)}.get(
            basis, ("site", "Сайт " + proof))
        match = _evidence(src, read_from, field, proof,
                          f"{label} в карточке 2ГИС совпадает с контактом компании из реестра")
        if match is None:  # never claim «подтверждено» on a fact that did not pass
            basis = "legal_name" if unique_legal else "name"
        else:
            evidence.append(match)
    # The first fact is built AFTER the final basis: a stranger's rating and number of reviews are not in any field
    weak = basis == "name"
    where = full.address or ""
    named = len(_norm(where)) >= check.MIN_TEXT
    stats = "" if weak else ", ".join(x for x in (f"рейтинг {full.rating:g}" if full.rating is not None else "",
                                                   f"отзывов {full.review_count}" if full.review_count else "") if x)
    head = _evidence(src, read_from, "address" if named else "name", where if named else full.name,
                     f"Карточка 2ГИС «{full.name}»" + (f", {where}" if where else "") + (f"; {stats}" if stats else ""))
    if head is None:
        return None, problem, shown_count
    quotes = _quotes(page, src, tab) if page is not None and TIER[basis] <= 1 else []
    return ReviewCard(
        branch_id=card.id, title=full.name, address=full.address, rubrics=full.rubrics, url=tab,
        rating=None if weak else full.rating, review_count=None if weak else full.review_count,
        basis=basis, verified=TIER[basis] == 0,
        note=_note(basis, full, proof, len(legal), names_ours, form is not None),
        quotes=quotes, evidence=[head, *evidence],
    ), problem, shown_count


def find_reviews(cp: Counterparty, cache: SourceCache | None = None,
                 client: httpx.Client | None = None) -> ReviewsResult:
    """Reviews of `cp` from 2GIS: cached, or at most one search and MAX_CARDS reviews tabs (paced, see gis2.py).
    `cp` should come through `strip_unverified`: its name and contacts are what the cards are compared with."""
    why = _unsupported(cp)
    if why is not None:
        return ReviewsResult(inn=cp.inn, status="unsupported", message=why)
    if gis2.disabled():
        return ReviewsResult(inn=cp.inn, status="unavailable", message="Чтение 2ГИС отключено на этом сервере.")
    form, brand = brand_of(cp.name) or (None, "")
    brand_norm = _norm(brand)
    city = CITY[_region(cp) or "Санкт-Петербург"]
    src = _Page(cache or SourceCache())
    try:
        found = gis2.fetch_search(city, brand, src.cache, client)
    except (gis2.Blocked, gis2.RobotsDisallowed, httpx.HTTPError, ValueError) as e:
        log.warning("reviews %s: 2ГИС недоступен: %s", cp.inn, e)
        return ReviewsResult(inn=cp.inn, status="unavailable", queried=brand,
                             message="2ГИС сейчас недоступен — попробуйте позже.")
    legal = _legal_organizations(found, form, brand_norm)  # counted before the cut to MAX_CARDS
    candidates = _candidates(found, form, brand_norm)
    if not candidates:
        return ReviewsResult(inn=cp.inn, status="not_found", queried=brand,
                             message=f"В 2ГИС нет карточки с названием «{brand}».")
    built = [_build(c, cp, form, brand, city, src, client, legal) for c in candidates]
    ranked = sorted(((c, n) for c, _, n in built if c is not None), key=lambda x: (TIER[x[0].basis], -x[1]))
    cards = [c for c, _ in ranked]
    problems = [p for _, p, _ in built if p]
    if not cards:
        return ReviewsResult(inn=cp.inn, status="not_found", queried=brand,
                             message=f"В 2ГИС нет карточки с названием «{brand}», факты которой можно подтвердить.")
    return ReviewsResult(inn=cp.inn, status="ok", queried=brand, cards=cards, message=problems[0] if problems else None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Отзывы из 2ГИС о компании по названию (воспроизведение, #234)")
    ap.add_argument("name", help='название как в ЕГРЮЛ, например: ООО "ГРЕЙС ТРЭЙД"')
    ap.add_argument("--region", default="Санкт-Петербург")
    ap.add_argument("--inn")
    ap.add_argument("--phone")
    ap.add_argument("--email")
    ap.add_argument("--site")
    args = ap.parse_args(argv)
    contacts = {k: v for k, v in (("phone", args.phone), ("email", args.email), ("site", args.site)) if v}
    cp = Counterparty(inn=args.inn, name=args.name, region=args.region, contacts=contacts)
    sys.stdout.write(find_reviews(cp).model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
