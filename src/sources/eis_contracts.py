"""ЕИС, реестр контрактов 44-ФЗ: did the supplier deliver an OKPD2 class under state contracts.
Owner: SOURCES. Signal 1 of #77 («подтверждена контрактом») → product_fit 0.8 of a «Новые» candidate
(#244, решение человека IR630 02.10). Not wired into src/enrich/discover.py yet: `supplied()` is the
whole entry point, see «Подключение» below.

Two pages of zakupki.gov.ru (research/SOURCES.md, «ЕИС, реестр контрактов по ИНН поставщика»):
1. search by supplier INN — `/epz/contract/search/results.html?supplierTitle=<ИНН>&…` (the filter
   «Поставщик», seen in a Wayback snapshot of 24.12.2025): per contract the registry number, status,
   customer, «Заключение контракта» and the purchase ID (ИКЗ), up to 50 per page. No OKPD2 codes;
   the supplier INN is in the URL filter, not on the page.
2. the contract card, tab «Платежи и объекты закупки» —
   `/epz/contract/contractCard/payment-info-and-target-of-order.html?reestrNumber=<номер>`: the same
   header (number, status, customer, date) and every position with its code «(62.01.11.000)».
   The card fact is built from this page: the contract number and the OKPD2 code literally occur in it.
   The supplier link is a second fact on page 1: the number is an entry of the search whose URL carries
   `supplierTitle=<ИНН>` (the card tab has no supplier INN; the filter's exactness is [ПРОВЕРИТЬ] live).

The ИКЗ digits 30–33 are the first four digits of the purchase OKPD2 code — used only to decide which
cards to open first (the search page has up to 50 contracts, a card costs one request per minute),
never as a fact: a wrong guess means one wasted request, not a wrong answer.

Answer (`Supply.supplied`):
- True — at least one card of a contract concluded before `as_of`, status «Исполнение» / «Исполнение
  завершено», has a position whose code starts with the class; basis «поставлял этот товар по
  госконтрактам: N …, последний — <дата>, <ссылка>», two Evidence per such contract (field
  `contract`, value — the registry number): its card and its entry in the search by supplier INN;
- False («нет подтверждения по 44-ФЗ», not «never delivered»: 223-ФЗ is not read) — only when the answer
  is verifiably complete: a search page «Поиск не дал результатов», or a
  page with all its contracts on it (total ≤ entries) and every eligible card opened without a match;
- None («не проверено») — anything else: an entry or a card without a date «Заключение контракта», with an
  empty or unknown status, a card whose positions table gives no code; an error, a captcha or a foreign
  page (`Blocked`), a page that
  does not fit, cards left unopened, offline without cache. Never False out of an error.

`as_of` cuts the history: a contract concluded on or after `as_of` does not count (the lot date in
eval — otherwise the future leaks into the backtest). The cut is applied twice: the search URL asks
for `contractDateTo = as_of − 1 day`, and every date read from the pages is checked again.

Pacing: one host, one budget — the lock and the time of the last request are those of
src/sources/eis_rnp.py (`Crawl-delay: 60` of zakupki.gov.ru), so РНП and contract requests together
keep one request per MIN_INTERVAL_S. Pages go to SourceCache as is; `ENRICH_OFFLINE=1` — cache only.

Подключение (этап 2, после PR #131): in discover(), behind an option off by default, for candidates
already in the top of the lot only — `supplied(inn, okpd2_class, as_of=query.as_of)`; True → the
product_fit step PRODUCT_FIT (0.8) with `basis` and `evidence`. Before that, `contract` has to return
to `src/enrich/evidence.FIELDS` (it was there until D-066), otherwise strip_unverified() drops these
facts; until then `check_supply(evidence, page_text, inn)` below is the check (both facts per contract).
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from src.contracts import Evidence, inn_is_valid
from src.enrich import evidence as ev_check
from src.sources import eis_rnp
from src.sources.cache import CachedPage, SourceCache
from src.sources.eis_rnp import BASE, NO_RESULTS, UA, Blocked
from src.sources.tls import gov_ssl_context

log = logging.getLogger(__name__)

SOURCE = "ЕИС, реестр контрактов"
FIELD = "contract"
PRODUCT_FIT = 0.8  # «подтверждена контрактом» (#77, ступени product_fit — решение человека IR630)
MIN_INTERVAL_S = eis_rnp.MIN_INTERVAL_S  # the zakupki.gov.ru budget, shared with РНП
SEARCH_MAX_AGE_S = float(os.environ.get("EIS_CONTRACTS_SEARCH_MAX_AGE_S", str(7 * 24 * 3600)))
CARD_MAX_AGE_S = float(os.environ.get("EIS_CONTRACTS_CARD_MAX_AGE_S", str(30 * 24 * 3600)))
MAX_CARDS = int(os.environ.get("EIS_CONTRACTS_MAX_CARDS", "3"))  # cards opened per check: 1 request/min each
TIMEOUT_S = eis_rnp.TIMEOUT_S
DELIVERED = ("Исполнение", "Исполнение завершено")  # stages that mean the goods were (being) delivered
# every stage of the search filter contractStageList=0,1,2,3; the labels of 2 and 3 are [ПРОВЕРИТЬ] live —
# an unknown label makes the answer incomplete (None), it is never skipped silently
STAGES = (*DELIVERED, "Исполнение прекращено", "Аннулировано")

_NUMBER = re.compile(r"\d{19}")  # registry number of a 44-ФЗ contract
_CLASS = re.compile(r"\d{2}(?:\.\d{1,2}){0,3}(?:\.\d{3})?")  # «62», «62.0», «62.01», …, «62.01.11.000»
_ENTRY = re.compile(r"search-registry-entry-block.*?(?=search-registry-entry-block|$)", re.S)
_ENTRY_NUMBER = re.compile(r"registry-entry__header-mid__number\">\s*<a[^>]*>\s*№\s*(\d{19})\s*</a>", re.S)
_ENTRY_STATUS = re.compile(r"registry-entry__header-mid__title\">\s*(.*?)\s*</div>", re.S)
_CARD_NUMBER = re.compile(r"cardMainInfo__purchaseLink[^\"]*\">\s*<a[^>]*>\s*№\s*(\d{19})\s*</a>", re.S)
_CARD_STATUS = re.compile(r"cardMainInfo__state[^\"]*\">\s*(.*?)\s*</span>", re.S)
_FIELD = re.compile(
    r"class=\"(?:registry-entry__body-title|data-block__title|cardMainInfo__title)\">\s*([^<]*?)\s*</(?:div|span)>\s*"
    r"<(?:div|span) class=\"(?:registry-entry__body-(?:value|href)|data-block__value|cardMainInfo__content)\"[^>]*>"
    r"(.*?)</(?:div|span)>", re.S)
_IKZ = re.compile(r"\(ИКЗ\)\s*(\d{36})")
_TOTAL = re.compile(r"Результаты поиска\s+(\d[\d\s]*?)\s+запис\w*")
_POSITIONS = re.compile(r"Позиции по КТРУ, ОКПД2(.*?)(?:Итого|$)", re.S)
_CODE = re.compile(r"\((\d{2}\.\d{2}(?:\.\d{1,2})?(?:\.\d{3})?)(?:-\d{8})?\)")
_DATE = re.compile(r"\d{2}\.\d{2}\.\d{4}")


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).replace("\xa0", " ").strip()


def _date(s: str | None) -> date | None:
    m = _DATE.search(s or "")
    return datetime.strptime(m[0], "%d.%m.%Y").date() if m else None


@dataclass(frozen=True)
class Contract:
    number: str  # registry number «1771014610219000243»
    status: str  # «Исполнение», «Исполнение завершено», …
    customer: str
    signed: date | None  # «Заключение контракта»
    ikz: str | None = None  # search page only
    codes: tuple[str, ...] = ()  # card only: OKPD2 (KTRU without its «-NNNNNNNN») of every position

    @property
    def ikz_class(self) -> str | None:
        """OKPD2 «XX.XX» from ИКЗ digits 30–33 — an ordering hint, not a fact."""
        if not self.ikz or self.ikz[29:33] == "0000":
            return None
        return f"{self.ikz[29:31]}.{self.ikz[31:33]}"


@dataclass(frozen=True)
class Supply:
    supplied: bool | None
    basis: str | None = None  # the line for the card, only when supplied is True
    evidence: list[Evidence] = field(default_factory=list)
    contracts: list[Contract] = field(default_factory=list)  # confirmed ones, newest first
    error: str | None = None  # why the answer is None; «blocked: …» — stop the batch


SEARCH_PATH = "/epz/contract/search/results.html"


def search_url(inn: str, as_of: date | None = None) -> str:
    params = {"supplierTitle": inn, "fz44": "on", "contractStageList": "0,1,2,3",
              "sortBy": "UPDATE_DATE", "sortDirection": "false", "recordsPerPage": "_50"}
    if as_of is not None:
        params["contractDateTo"] = (as_of - timedelta(days=1)).strftime("%d.%m.%Y")
    return f"{BASE}{SEARCH_PATH}?{urlencode(params)}"


def card_url(number: str) -> str:
    return f"{BASE}/epz/contract/contractCard/payment-info-and-target-of-order.html?reestrNumber={number}"


def _fields(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in _FIELD.findall(block):
        out.setdefault(_text(k), _text(v))
    return out


def parse_search(body: str) -> tuple[list[Contract], int | None]:
    """Contracts on a search page and the total «Результаты поиска N записей» (None if not shown)."""
    out = []
    for block in _ENTRY.findall(body):
        number = _ENTRY_NUMBER.search(block)
        if not number:
            continue
        f = _fields(block)
        status = _ENTRY_STATUS.search(block)
        ikz = _IKZ.search(_text(block))
        out.append(Contract(number=number[1], status=_text(status[1]) if status else "",
                            customer=f.get("Заказчик", ""), signed=_date(f.get("Заключение контракта")),
                            ikz=ikz[1] if ikz else None))
    total = _TOTAL.search(_text(body))
    return out, (int(re.sub(r"\D", "", total[1])) if total else None)


def parse_card(body: str) -> Contract | None:
    """The contract of a card page «Платежи и объекты закупки» with the OKPD2 codes of its positions."""
    number = _CARD_NUMBER.search(body)
    if not number:
        return None
    f = _fields(body)
    status = _CARD_STATUS.search(body)
    table = _POSITIONS.search(_text(body))
    codes = tuple(dict.fromkeys(_CODE.findall(table[1]))) if table else ()
    return Contract(number=number[1], status=_text(status[1]) if status else "", customer=f.get("Заказчик", ""),
                    signed=_date(f.get("Заключение контракта")), codes=codes)


def in_class(code: str, okpd2: str) -> bool:
    """A position code falls into the class: «62.01.11.000» is in «62», «62.0», «62.01», «62.01.1»;
    not in «62.02» and not in «62.01.12» (a string prefix that is not a level of the code)."""
    if not code.startswith(okpd2):
        return False
    rest = code[len(okpd2):]
    if not rest:
        return True
    if "." in okpd2 and len(okpd2.rsplit(".", 1)[1]) == 1:  # «62.0», «62.01.1»: the level goes on in the segment
        return True
    return rest[0] == "."  # «62», «62.01», «62.01.11»: the next level starts with a dot


def _validate_search(body: str) -> None:
    if "search-registry-entrys-block" not in body and NO_RESULTS not in body:
        raise Blocked("zakupki.gov.ru вернул не страницу поиска контрактов — останавливаемся")


def _validate_card(number: str) -> Callable[[str], None]:
    def check(body: str) -> None:
        card = parse_card(body)
        if card is None or card.number != number or "Позиции по КТРУ, ОКПД2" not in body:
            raise Blocked(f"zakupki.gov.ru вернул не карточку контракта {number} — останавливаемся")
    return check


def _get(client: httpx.Client, url: str, validate: Callable[[str], None]) -> tuple[int, str]:
    # one host, one budget: the РНП client's lock and clock (Crawl-delay: 60).
    # TODO(#244): a public pacer in eis_rnp.py (or eis_common.py) once PR #156 is merged; until then the
    # private _lock/_last_request are shared on purpose — a second clock would double the request rate.
    with eis_rnp._lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - eis_rnp._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            r = client.get(url)
        finally:
            eis_rnp._last_request = time.monotonic()
    if r.status_code in (403, 429):
        raise Blocked(f"zakupki.gov.ru ответил {r.status_code} — останавливаемся")
    r.raise_for_status()
    validate(r.text)
    return r.status_code, r.text


def fetch(url: str, validate: Callable[[str], None], max_age_s: float, cache: SourceCache | None = None,
          client: httpx.Client | None = None) -> CachedPage:
    """Cached, paced GET. Offline → cache or KeyError; a failed request → the stale page if any."""
    cache = cache or SourceCache()
    hit = cache.get(url, allow_stale=True)
    if os.environ.get("ENRICH_OFFLINE") == "1":
        if hit is None:
            raise KeyError(url)
        return hit
    if hit is not None and time.time() - hit.fetched_at <= max_age_s:
        return hit
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, verify=gov_ssl_context(), headers={"User-Agent": UA})
    try:
        status, body = _get(client, url, validate)
    except Exception:
        if hit is not None:
            return hit  # stale answer beats none; its fetched_at goes with the fact
        raise
    finally:
        if own:
            client.close()
    return cache.put(url, body, status)


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "контракт"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "контракта"
    return "контрактов"


def _readable(c: Contract) -> bool:
    """Date and status are read and the status is a known stage: only then may the contract be skipped."""
    return c.signed is not None and c.status in STAGES


def _eligible(c: Contract, as_of: date | None) -> bool:
    return _readable(c) and c.status in DELIVERED and (as_of is None or (c.signed is not None and c.signed < as_of))


def _hint(c: Contract, okpd2: str) -> bool:
    g = c.ikz_class
    return g is not None and (in_class(g, okpd2) or in_class(okpd2, g))


def supplied(inn: str, okpd2: str, as_of: date | None = None, *, cache: SourceCache | None = None,
             client: httpx.Client | None = None, max_cards: int | None = None) -> Supply:
    """Did `inn` deliver goods of the OKPD2 class `okpd2` («17.12», «26.20.11») under 44-ФЗ contracts
    concluded before `as_of`. Never raises on network trouble: that is `Supply(None, error=…)`."""
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    okpd2 = okpd2.strip()
    if not _CLASS.fullmatch(okpd2):
        raise ValueError(f"OKPD2 class {okpd2!r} is not a code like «17.12»")
    cache = cache or SourceCache()
    limit = MAX_CARDS if max_cards is None else max_cards
    try:
        page = fetch(search_url(inn, as_of), _validate_search, SEARCH_MAX_AGE_S, cache, client)
    except Blocked as e:
        log.warning("eis_contracts: %s (%s)", e, inn)
        return Supply(None, error=f"blocked: {e}")
    except Exception as e:  # offline without cache, timeout, 5xx: «не проверено»
        log.warning("eis_contracts: поиск по %s не выполнен: %r", inn, e)
        return Supply(None, error=f"search: {e!r}")
    entries, total = parse_search(page.body)
    if not entries:
        if NO_RESULTS in page.body and total is None and "search-registry-entry-block" not in page.body:
            return Supply(False)  # a clean «Поиск не дал результатов»
        return Supply(None, error="search page without readable contracts")
    unreadable = [c.number for c in entries if not _readable(c)]
    complete = total is not None and total <= len(entries)
    eligible = [c for c in entries if _eligible(c, as_of)]
    error: str | None = f"search entries without date or known status: {', '.join(unreadable)}" if unreadable else None
    # the ИКЗ hint first, then the newest: the order only decides which cards are opened
    eligible.sort(key=lambda c: (not _hint(c, okpd2), -(c.signed or date.min).toordinal()))
    found: list[tuple[Contract, CachedPage, str]] = []
    opened = 0
    for c in eligible[:limit]:
        try:
            card_page = fetch(card_url(c.number), _validate_card(c.number), CARD_MAX_AGE_S, cache, client)
        except Blocked as e:
            log.warning("eis_contracts: %s (%s)", e, c.number)
            error = f"blocked: {e}"
            break
        except Exception as e:
            log.warning("eis_contracts: карточка %s не получена: %r", c.number, e)
            error = f"card {c.number}: {e!r}"
            continue
        card = parse_card(card_page.body)
        if card is None or card.number != c.number:
            error = f"card {c.number}: not a card of this contract"
            continue
        if not _readable(card) or not card.codes:  # a positions table without codes is a reading error
            error = f"card {c.number}: no date, known status or OKPD2 codes read"
            continue
        opened += 1
        # the card's own date and status decide (the search entry may be older than the card)
        if not _eligible(card, as_of):
            continue
        code = next((x for x in card.codes if in_class(x, okpd2)), None)
        if code is not None:
            found.append((card, card_page, code))
    if found:
        found.sort(key=lambda t: t[0].signed or date.min, reverse=True)
        last, last_page, _ = found[0]
        basis = (f"поставлял этот товар по госконтрактам: {len(found)} {_plural(len(found))}, "
                 f"последний — {last.signed:%d.%m.%Y}, {last_page.url}")
        evs = [e for card, p, code in found for e in (_evidence(card, p, code), _listed_evidence(card, page))]
        return Supply(True, basis=basis, evidence=evs, contracts=[t[0] for t in found])
    if complete and error is None and opened == len(eligible):
        return Supply(False)
    return Supply(None, error=error or f"opened {opened} of {len(eligible)} eligible cards")


def _evidence(card: Contract, page: CachedPage, code: str) -> Evidence:
    at = datetime.fromtimestamp(page.fetched_at, tz=UTC)
    signed = f" от {card.signed:%d.%m.%Y}" if card.signed else ""
    customer = f", заказчик {card.customer}" if card.customer else ""
    return Evidence(source=SOURCE, url=page.url, fetched_at=at, field=FIELD, value=card.number,
                    fact=f"Госконтракт № {card.number}{signed}{customer}, позиция ОКПД2 {code}")


def _listed_evidence(card: Contract, search: CachedPage) -> Evidence:
    """The link contract → supplier: the contract is in the answer of the search filtered by supplier INN
    (`supplierTitle=<ИНН>` in the URL; the card tab has no supplier INN). The filter's exactness is
    [ПРОВЕРИТЬ] live. No INN in the fact: it is not on the page, only in the URL."""
    at = datetime.fromtimestamp(search.fetched_at, tz=UTC)
    return Evidence(source=SOURCE, url=search.url, fetched_at=at, field=FIELD, value=card.number,
                    fact=f"Госконтракт № {card.number} — в выдаче реестра контрактов ЕИС по фильтру «Поставщик»")


def _is_search(url: str) -> bool:
    return urlsplit(url).path == SEARCH_PATH


def supplier_of(url: str) -> str | None:
    """`supplierTitle` of a contract search URL — the INN the answer is filtered by."""
    parts = urlsplit(url)
    if parts.netloc != urlsplit(BASE).netloc or parts.path != SEARCH_PATH:
        return None
    values = parse_qs(parts.query).get("supplierTitle", [])
    return values[0] if len(values) == 1 and inn_is_valid(values[0]) else None


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
    if ev.field != FIELD or ev.value is None or not _NUMBER.fullmatch(ev.value):
        return [f"{ev.url}: не факт о контракте ЕИС (field={ev.field!r}, value={ev.value!r})"]
    errors = ev_check.check_evidence(ev.model_copy(update={"field": "name"}), page_text)
    body = page_text(ev.url)
    if body is None:
        return errors
    if _is_search(ev.url):
        supplier = supplier_of(ev.url)
        if supplier is None or (inn is not None and supplier != inn):
            errors.append(f"{ev.url}: выдача не отфильтрована по ИНН поставщика {inn or '(любой)'}")
        if ev.value not in {c.number for c in parse_search(body)[0]}:
            errors.append(f"{ev.url}: контракта {ev.value} нет среди записей выдачи")
        return errors
    card = parse_card(body)
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
        kinds.setdefault(ev.value, set()).add(_is_search(ev.url))
    for number, k in kinds.items():
        if k != {True, False}:
            errors.append(f"контракт {number}: нужны оба факта — карточка и запись в выдаче по ИНН поставщика")
    return errors
