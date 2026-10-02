"""Reviews of a supplier from 2ГИС, on demand: a quote, its date and a link — or nothing (#234, D-112). Owner: SOURCES.

2GIS does not publish the INN. A card found by the brand is therefore NOT «the company» by itself: the
company «САТУРН» of the МСП register (stationery) and the building-materials chain «Сатурн» share a name and a
city. How far a card is trusted is the `basis`, from the strongest to the weakest:

- `phone` / `site` — a phone / site of the card equals the company's own (`Counterparty.contacts`, each with
  its Evidence from the registry): the card is confirmed, `verified=True`;
- `legal_name` — the card itself names the legal entity («ООО Грейс трэйд») and it equals ours: the quotes are
  shown, marked «не подтверждено по ИНН»;
- `name` — only the brand is equal: the card is listed (the user sees its sphere and address and can open it),
  but not a single quote is shown until something above confirms it.

Every shown fact is an Evidence on a cached page (src/sources/gis2.py keeps the flat text); a quote is a
literal prefix of the review, and `check_evidence` runs on EVERY Evidence before it is returned: what the page
does not contain never reaches the user. Reviews are not in the score and do not touch the rating.
Individual entrepreneurs are not searched (a natural person), nor are companies with no name or outside СПб/ЛО.

`python -m src.enrich.reviews 'ООО "ГРЕЙС ТРЭЙД"' [--phone … --site …]` prints the result as JSON (reproduction).
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

Basis = Literal["phone", "site", "legal_name", "name"]
TIER: dict[str, int] = {"phone": 0, "site": 0, "legal_name": 1, "name": 2}


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
    rating: float | None = Field(description="рейтинг карточки, как его показывает 2ГИС")
    review_count: int | None
    basis: Basis
    verified: bool = Field(description="True только при совпадении телефона или сайта с данными компании")
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


def _host(url: str) -> str | None:
    host = urlsplit(url if "//" in url else "//" + url).hostname
    return host.lower().removeprefix("www.") if host else None


def _same_brand(card: gis2.Card, brand_norm: str) -> bool:
    return _norm(card.brand) == brand_norm


def _legal_matches(card: gis2.Card, form: str | None, brand_norm: str) -> bool:
    legal = brand_of(card.legal_name) if card.legal_name else None
    if legal is None or _norm(legal[1]) != brand_norm:
        return False
    return form is None or legal[0] is None or legal[0] == form


def _candidates(found: list[gis2.Card], brand_norm: str) -> list[gis2.Card]:
    """One card per organization (its branch with the most reviews), at most MAX_CARDS organizations."""
    best: dict[str, gis2.Card] = {}
    for c in found:
        if not _same_brand(c, brand_norm):
            continue
        key = c.org_id or c.id
        if key not in best or (c.review_count or 0) > (best[key].review_count or 0):
            best[key] = c
    return list(best.values())[:MAX_CARDS]


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


def _basis(card: gis2.Card, cp: Counterparty, form: str | None, brand_norm: str) -> tuple[Basis, str]:
    """The strongest basis the card has, and the value on the card that proves it (`''` for the two weak ones)."""
    ours = _digits(cp.contacts.get("phone", ""))[-10:]
    if len(ours) == 10:
        for phone in card.phones:
            if phone[-10:] == ours:
                return "phone", phone
    our_site = _host(cp.contacts["site"]) if cp.contacts.get("site") else None
    if our_site:
        for site in card.sites:
            if _host(site) == our_site:
                return "site", site
    return ("legal_name", "") if _legal_matches(card, form, brand_norm) else ("name", "")


def _note(basis: Basis, card: gis2.Card, proof: str) -> str:
    if basis == "phone":
        return (f"Телефон из карточки ({_pretty_phone(proof)}) совпал с телефоном компании из реестра: "
                "карточка относится к ней.")
    if basis == "site":
        return f"Сайт из карточки ({_host(proof)}) совпал с сайтом компании из реестра: карточка относится к ней."
    if basis == "legal_name":
        return (f"2ГИС указывает юрлицо «{card.legal_name}» — оно совпадает с названием компании. Телефон и сайт для "
                "сверки не совпали или неизвестны, а ИНН 2ГИС не публикует: проверьте адрес и сферу.")
    return (f"Название «{card.brand}» в карточке есть, но юрлицо не указано, а ИНН 2ГИС не публикует — это может "
            "быть другая организация. Отзывы не показываются, пока принадлежность не подтверждена; откройте карточку и "
            "проверьте адрес и сферу.")


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
           client: httpx.Client | None) -> tuple[ReviewCard | None, str | None]:
    """The card of the answer (None when none of its facts can be proved from a page) and a message when its
    reviews could not be read."""
    brand_norm = _norm(brand)
    tab = gis2.reviews_url(city, card.id)
    page: gis2.ReviewsPage | None = None
    problem: str | None = None
    # The tab holds the reviews and the contacts of the card. With nothing to compare them with (the company has no
    # phone or site) and no legal entity named by the card, nothing could be shown or confirmed: do not ask.
    comparable = bool(cp.contacts.get("phone") or cp.contacts.get("site")) or _legal_matches(card, form, brand_norm)
    if (card.review_count or 0) > 0 and comparable:
        try:
            page = gis2.fetch_reviews(city, card.id, src.cache, client)
        except (gis2.Blocked, gis2.RobotsDisallowed, httpx.HTTPError, ValueError) as e:
            log.warning("reviews: вкладка отзывов %s не прочитана: %s", card.id, e)
            problem = "Не удалось прочитать тексты отзывов: 2ГИС сейчас недоступен."
    full = page.card if page is not None else card
    basis, proof = _basis(full, cp, form, brand_norm)
    read_from = tab if page is not None else gis2.search_url(city, brand)  # the page the card facts came from
    where = full.address or ""
    named = len(_norm(where)) >= check.MIN_TEXT
    stats = ", ".join(x for x in (f"рейтинг {full.rating:g}" if full.rating is not None else "",
                                   f"отзывов {full.review_count}" if full.review_count else "") if x)
    head = _evidence(src, read_from, "address" if named else "name", where if named else full.name,
                     f"Карточка 2ГИС «{full.name}»" + (f", {where}" if where else "") + (f"; {stats}" if stats else ""))
    if head is None:
        return None, problem
    evidence = [head]
    if proof:
        field, label = ("phone", "Телефон +" + proof) if basis == "phone" else ("site", "Сайт " + proof)
        match = _evidence(src, tab, field, proof, f"{label} в карточке 2ГИС совпадает с контактом компании из реестра")
        if match is None:  # never claim «подтверждено» on a fact that did not pass
            basis = "legal_name" if _legal_matches(full, form, brand_norm) else "name"
        else:
            evidence.append(match)
    quotes = _quotes(page, src, tab) if page is not None and TIER[basis] <= 1 else []
    return ReviewCard(
        branch_id=card.id, title=full.name, address=full.address, rubrics=full.rubrics, url=tab, rating=full.rating,
        review_count=full.review_count, basis=basis, verified=TIER[basis] == 0, note=_note(basis, full, proof),
        quotes=quotes, evidence=evidence,
    ), problem


def find_reviews(cp: Counterparty, cache: SourceCache | None = None,
                 client: httpx.Client | None = None) -> ReviewsResult:
    """Reviews of `cp` from 2GIS: cached, or at most one search and MAX_CARDS reviews tabs (paced, see gis2.py).
    `cp` should come through `strip_unverified`: its name and contacts are what the cards are compared with."""
    why = _unsupported(cp)
    if why is not None:
        return ReviewsResult(inn=cp.inn, status="unsupported", message=why)
    form, brand = brand_of(cp.name) or (None, "")
    city = CITY[_region(cp) or "Санкт-Петербург"]
    src = _Page(cache or SourceCache())
    try:
        candidates = _candidates(gis2.fetch_search(city, brand, src.cache, client), _norm(brand))
    except (gis2.Blocked, gis2.RobotsDisallowed, httpx.HTTPError, ValueError) as e:
        log.warning("reviews %s: 2ГИС недоступен: %s", cp.inn, e)
        return ReviewsResult(inn=cp.inn, status="unavailable", queried=brand,
                             message="2ГИС сейчас недоступен — попробуйте позже.")
    if not candidates:
        return ReviewsResult(inn=cp.inn, status="not_found", queried=brand,
                             message=f"В 2ГИС нет карточки с названием «{brand}».")
    built = [_build(c, cp, form, brand, city, src, client) for c in candidates]
    cards = sorted((c for c, _ in built if c is not None), key=lambda c: (TIER[c.basis], -(c.review_count or 0)))
    problems = [p for _, p in built if p]
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
    ap.add_argument("--site")
    args = ap.parse_args(argv)
    contacts = {k: v for k, v in (("phone", args.phone), ("site", args.site)) if v}
    cp = Counterparty(inn=args.inn, name=args.name, region=args.region, contacts=contacts)
    sys.stdout.write(find_reviews(cp).model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
