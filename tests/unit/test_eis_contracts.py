"""ЕИС, реестр контрактов: parsing of real pages, Evidence on the cached card, the as_of cut (no future
in eval), and — as in РНП — «not checked» (None) for every error, never False out of an error.

Fixtures (tests/fixtures/eis_contracts/README.md): real zakupki.gov.ru pages as archived by the Wayback
Machine (zakupki.gov.ru itself did not answer from this machine on 02.10 — TLS handshake timeout):
- `search_supplier_7733284010.html` — contract search with the supplier filter `supplierTitle=7733284010`
  (24.12.2025): «47 записей», 10 contracts on the page, customer ФНС;
- `card_1771014610219000243.html` — card «Платежи и объекты закупки» (14.05.2026): 62.01.11.000, 62.02.20.130;
- `card_3263207897522000001.html` — the same tab of a goods contract (19.04.2025): 22.29.10.110, 14.12.30.150,
  14.13.22.120;
- `search_no_results.html` — contract search «Поиск не дал результатов» (04.05.2026).
No card of a contract listed on the search page is archived, so the positive path takes its list of
contracts from the card header itself (`_listed`) — a list of real contracts, not a made-up page; the
search page cached for it is SEARCH with those numbers appended (derived, marked in `_listed`). Every
other page marked «derived» below is a real page with one edit, to reach a state no snapshot shows.
"""

import re
import time
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from src.contracts import Evidence
from src.enrich import eis_contracts_check as chk
from src.enrich import evidence as ev_check
from src.sources import eis_contracts as m
from src.sources import eis_rnp
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "eis_contracts"
SEARCH = (FIX / "search_supplier_7733284010.html").read_text(encoding="utf-8")
CARD = (FIX / "card_1771014610219000243.html").read_text(encoding="utf-8")
CARD_GOODS = (FIX / "card_3263207897522000001.html").read_text(encoding="utf-8")
NO_RESULTS = (FIX / "search_no_results.html").read_text(encoding="utf-8")
SUPPLIER = "7733284010"  # the supplier of the archived search
N_IT, N_GOODS = "1771014610219000243", "3263207897522000001"
AT = datetime(2026, 5, 14, 8, 43, 45, tzinfo=UTC).timestamp()  # Wayback snapshot time of CARD


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    monkeypatch.setattr(eis_rnp, "MIN_INTERVAL_S", 0.0)
    eis_rnp.PACER.reset()
    c = SourceCache()
    c.put(m.card_url(N_IT), CARD, fetched_at=AT)
    c.put(m.card_url(N_GOODS), CARD_GOODS, fetched_at=AT)
    return c


def _listed(monkeypatch, cache, as_of, *bodies, total=None):
    """The search answer lists the contracts of these real cards (header: number, status, customer, date).
    Derived page: SEARCH with the card numbers appended, so the number of a search fact is on the page."""
    entries = [c for c in (m.parse_card(b) for b in bodies) if c is not None]
    tail = "".join(f"<p>№ {c.number}</p>" for c in entries)
    cache.put(m.search_url(SUPPLIER, as_of), SEARCH + tail, fetched_at=AT)
    monkeypatch.setattr(m, "parse_search", lambda body: (entries, len(entries) if total is None else total))


def _card_facts(evs: list[Evidence]) -> list[Evidence]:
    return [e for e in evs if not m.is_search(e.url)]


# --- parsing --------------------------------------------------------------------------------------


def test_search_page_contracts_dates_customers_and_total():
    entries, total = m.parse_search(SEARCH)
    assert total == 47 and len(entries) == 10
    first = entries[0]
    assert first.number == "1770732915224000102"
    assert first.status == "Исполнение"
    assert first.customer == "ФЕДЕРАЛЬНАЯ НАЛОГОВАЯ СЛУЖБА"
    assert first.signed == date(2024, 12, 28)
    assert first.ikz == "241770732915277070100101200016201246" and first.ikz_class == "62.01"
    assert [e.ikz_class for e in entries].count("62.02") == 1  # «Сопровождение … программного обеспечения»
    assert {e.status for e in entries} == {"Исполнение", "Исполнение завершено"}


def test_card_header_and_position_codes():
    it = m.parse_card(CARD)
    assert it is not None
    assert (it.number, it.status, it.signed) == (N_IT, "Исполнение завершено", date(2019, 7, 15))
    assert it.customer == "ГЕНЕРАЛЬНАЯ ПРОКУРАТУРА РОССИЙСКОЙ ФЕДЕРАЦИИ"
    assert it.codes == ("62.01.11.000", "62.02.20.130")
    goods = m.parse_card(CARD_GOODS)
    assert goods is not None and goods.signed == date(2022, 10, 25)
    assert goods.codes == ("22.29.10.110", "14.12.30.150", "14.13.22.120")  # not the country code «(643)»


def test_search_page_is_not_a_card_and_no_results_has_no_entries():
    assert m.parse_card(SEARCH) is None
    assert m.parse_search(NO_RESULTS) == ([], None)


@pytest.mark.parametrize("code, cls, ok", [
    ("62.01.11.000", "62", True), ("62.01.11.000", "62.0", True), ("62.01.11.000", "62.01", True),
    ("62.01.11.000", "62.01.1", True), ("62.01.11.000", "62.01.11", True), ("62.01.11.000", "62.01.11.000", True),
    ("62.01.11.000", "62.02", False), ("62.01.11.000", "62.1", False), ("62.01.11.000", "62.01.12", False),
    ("62.01.11.000", "62.01.11.00", False), ("26.20.11.110", "26.2", True), ("26.30.11.110", "26.2", False),
    ("17.12.14.110", "17.1", True), ("17.12.14.110", "1", False),
])
def test_in_class_follows_okpd2_levels(code, cls, ok):
    assert m.in_class(code, cls) is ok


def test_search_url_cuts_at_the_day_before_as_of():
    assert f"supplierTitle={SUPPLIER}" in m.search_url(SUPPLIER)
    assert "contractDateTo" not in m.search_url(SUPPLIER)
    assert "contractDateTo=14.07.2019" in m.search_url(SUPPLIER, date(2019, 7, 15))


@pytest.mark.parametrize("n, word", [(1, "контракт"), (2, "контракта"), (4, "контракта"), (5, "контрактов"),
                                     (11, "контрактов"), (12, "контрактов"), (21, "контракт"), (22, "контракта"),
                                     (112, "контрактов")])
def test_plural(n, word):
    assert m._plural(n) == word


# --- the answer -----------------------------------------------------------------------------------


def test_supplied_class_gives_basis_and_evidence_that_passes(cache, monkeypatch):
    as_of = date(2025, 1, 1)
    _listed(monkeypatch, cache, as_of, CARD, CARD_GOODS)
    res = m.supplied(SUPPLIER, "62.01", as_of, cache=cache)
    assert res.supplied is True and res.error is None
    url = m.card_url(N_IT)
    assert res.basis == f"поставлял этот товар по госконтрактам: 1 контракт, последний — 15.07.2019, {url}"
    [ev, listed] = res.evidence  # the card, and the entry in the search by supplier INN
    assert (ev.source, ev.url, ev.field, ev.value) == (m.SOURCE, url, "contract", N_IT)
    assert (listed.url, listed.field, listed.value) == (m.search_url(SUPPLIER, as_of), "contract", N_IT)
    assert m.supplier_of(listed.url) == SUPPLIER
    assert chk.check_supply(res.evidence, cache.page_text, SUPPLIER) == []
    assert chk.check_supply(res.evidence, cache.page_text, "7820324037")  # another supplier's INN
    assert chk.check_supply([ev], cache.page_text, SUPPLIER)  # the card alone does not tie it to the supplier
    assert ev.fetched_at == datetime.fromtimestamp(AT, tz=UTC)
    assert "ОКПД2 62.01.11.000" in ev.fact and "от 15.07.2019" in ev.fact
    assert chk.check_evidence(ev, cache.page_text) == []
    # evidence.py's literal check: the contract number and the code are on the cached page
    assert ev_check.value_in_page("contract", N_IT, CARD)
    assert ev_check.value_in_page("okpd2", "62.01.11.000", CARD)
    assert m.PRODUCT_FIT == 0.8


def test_supplied_picks_the_card_with_the_class(cache, monkeypatch):
    _listed(monkeypatch, cache, None, CARD, CARD_GOODS)
    res = m.supplied(SUPPLIER, "22.29", cache=cache)
    assert res.supplied is True and [c.number for c in res.contracts] == [N_GOODS]
    assert "ОКПД2 22.29.10.110" in res.evidence[0].fact
    assert chk.check_evidence(res.evidence[0], cache.page_text) == []
    assert m.supplied(SUPPLIER, "14.1", cache=cache).contracts[0].number == N_GOODS


def test_no_position_of_the_class_on_a_complete_page_is_false(cache, monkeypatch):
    _listed(monkeypatch, cache, None, CARD, CARD_GOODS)
    res = m.supplied(SUPPLIER, "17.12", cache=cache)
    assert res.supplied is False and res.evidence == [] and res.basis is None


@pytest.mark.parametrize("as_of, expected", [(date(2019, 7, 15), False), (date(2019, 7, 16), True)])
def test_contract_concluded_on_or_after_as_of_does_not_count(cache, monkeypatch, as_of, expected):
    _listed(monkeypatch, cache, as_of, CARD)
    res = m.supplied(SUPPLIER, "62.01", as_of, cache=cache)
    assert res.supplied is expected
    assert bool(res.evidence) is expected


def test_card_date_decides_over_the_search_entry(cache, monkeypatch):
    """The search entry says 2019-01-01, the card says 15.07.2019: the card is the source of the fact."""
    as_of = date(2019, 7, 1)
    cache.put(m.search_url(SUPPLIER, as_of), SEARCH, fetched_at=AT)
    card = m.parse_card(CARD)
    assert card is not None
    entry = m.Contract(number=card.number, status=card.status, customer=card.customer, signed=date(2019, 1, 1))
    monkeypatch.setattr(m, "parse_search", lambda body: ([entry], 1))
    res = m.supplied(SUPPLIER, "62.01", as_of, cache=cache)
    assert res.supplied is not True and res.evidence == []


def test_unfinished_stage_is_not_counted(cache, monkeypatch):
    card = m.parse_card(CARD)
    assert card is not None
    cache.put(m.search_url(SUPPLIER), SEARCH, fetched_at=AT)
    annulled = m.Contract(number=card.number, status="Аннулировано", customer=card.customer, signed=card.signed)
    monkeypatch.setattr(m, "parse_search", lambda body: ([annulled], 1))
    assert m.supplied(SUPPLIER, "62.01", cache=cache).supplied is not True


def test_no_results_page_is_false(cache):
    cache.put(m.search_url(SUPPLIER), NO_RESULTS, fetched_at=AT)
    assert m.supplied(SUPPLIER, "62.01", cache=cache).supplied is False


# --- «не проверено» -------------------------------------------------------------------------------


def test_offline_without_cache_is_none(cache):
    res = m.supplied(SUPPLIER, "62.01", date(2026, 1, 1), cache=cache)
    assert res.supplied is None and res.error and res.error.startswith("search")


def test_real_search_page_without_cached_cards_is_none(cache, caplog):
    as_of = date(2026, 1, 1)
    cache.put(m.search_url(SUPPLIER, as_of), SEARCH, fetched_at=AT)
    with caplog.at_level("WARNING", logger=m.__name__):
        res = m.supplied(SUPPLIER, "62.01", as_of, cache=cache)
    assert res.supplied is None and res.evidence == [] and res.error and res.error.startswith("card ")
    tried = [r.getMessage() for r in caplog.records]
    assert len(tried) == m.MAX_CARDS
    assert "1770732915225000060" in tried[0]  # the newest 62.01 contract (27.10.2025) is opened first


def test_page_with_more_contracts_than_shown_is_never_false(cache, monkeypatch):
    _listed(monkeypatch, cache, None, CARD, CARD_GOODS, total=47)
    assert m.supplied(SUPPLIER, "17.12", cache=cache).supplied is None
    _listed(monkeypatch, cache, None, CARD, CARD_GOODS)
    assert m.supplied(SUPPLIER, "17.12", cache=cache, max_cards=1).supplied is None  # one card left unopened


def test_card_of_another_contract_is_not_used(cache, monkeypatch):
    _listed(monkeypatch, cache, None, CARD)
    cache.put(m.card_url(N_IT), CARD_GOODS, fetched_at=AT)  # wrong page under the card url
    res = m.supplied(SUPPLIER, "22.29", cache=cache)
    assert res.supplied is None and res.evidence == []


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status, body", [
    (403, "Forbidden"), (429, "Too Many Requests"),
    (200, "<html><body>Подтвердите, что вы не робот</body></html>"),  # captcha / foreign page
])
def test_blocked_search_is_none_and_not_cached(cache, monkeypatch, status, body):
    monkeypatch.delenv("ENRICH_OFFLINE")
    res = m.supplied(SUPPLIER, "62.01", cache=cache, client=_client(lambda r: httpx.Response(status, text=body)))
    assert res.supplied is None and res.error and res.error.startswith("blocked")
    assert cache.get(m.search_url(SUPPLIER), allow_stale=True) is None


def test_blocked_card_stops_the_check(cache, monkeypatch):
    monkeypatch.delenv("ENRICH_OFFLINE")
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(str(r.url))
        return httpx.Response(200, text=SEARCH if "search" in str(r.url) else CARD_GOODS)  # card of another one

    as_of = date(2026, 1, 1)
    res = m.supplied(SUPPLIER, "62.02", as_of, cache=cache, client=_client(handler), max_cards=3)
    assert res.supplied is None and res.error and res.error.startswith("blocked")
    assert len(seen) == 2  # the search and one card: a foreign page stops the batch
    assert seen[1] == m.card_url("1770732915224000091")  # ИКЗ hint: the only 62.02 contract goes first
    assert cache.get(m.card_url("1770732915224000091"), allow_stale=True) is None


def test_requests_share_the_rnp_pace(cache, monkeypatch):
    monkeypatch.delenv("ENRICH_OFFLINE")
    monkeypatch.setattr(eis_rnp, "MIN_INTERVAL_S", 0.3)
    with eis_rnp.PACER.slot():  # РНП has just asked zakupki.gov.ru
        pass
    t0 = time.monotonic()
    m.supplied(SUPPLIER, "62.01", cache=cache, client=_client(lambda r: httpx.Response(200, text=NO_RESULTS)))
    assert time.monotonic() - t0 >= 0.25
    t1 = time.monotonic()
    with eis_rnp.PACER.slot():  # and the next РНП request waits for this one
        pass
    assert time.monotonic() - t1 >= 0.25


def test_invalid_input_is_an_error_not_an_answer(cache):
    with pytest.raises(ValueError):
        m.supplied("7733284011", "62.01", cache=cache)
    with pytest.raises(ValueError):
        m.supplied(SUPPLIER, "программы", cache=cache)


# --- made-up facts are rejected -------------------------------------------------------------------


def _good(cache, monkeypatch) -> Evidence:
    _listed(monkeypatch, cache, None, CARD)
    [ev] = _card_facts(m.supplied(SUPPLIER, "62.01", cache=cache).evidence)
    assert chk.check_evidence(ev, cache.page_text) == []
    return ev


@pytest.mark.parametrize("update", [
    {"value": "1771014610219000244"},  # a contract that is not on the page
    {"fact": "Госконтракт № 1771014610219000243 от 15.07.2019, позиция ОКПД2 62.03.12.130"},  # code not in it
    {"fact": "Госконтракт № 1771014610219000243 от 15.07.2024, позиция ОКПД2 62.01.11.000"},  # wrong date
    {"fact": "Госконтракт № 1771014610219000243 от 15.07.2019"},  # no code at all
    {"url": m.card_url("1771014610219000999")},  # page not in cache
    {"url": m.card_url(N_GOODS)},  # the card of another contract
    {"field": "name"},  # not a contract fact
    {"fact": "Госконтракт № 1771014610219000243 от 15.07.2019, заказчик ФЕДЕРАЛЬНАЯ НАЛОГОВАЯ СЛУЖБА, "
             "позиция ОКПД2 62.01.11.000"},  # another customer
])
def test_made_up_contract_fact_is_rejected(cache, monkeypatch, update):
    ev = _good(cache, monkeypatch).model_copy(update=update)
    assert chk.check_evidence(ev, cache.page_text)


def test_number_elsewhere_on_a_card_is_not_that_contract(cache, monkeypatch):
    """Derived page (one edit): the real CARD with another contract number appended. The number is on the
    page, the code and the date are the card's — but the card is not of that contract."""
    ev = _good(cache, monkeypatch)
    other = "1770732915224000102"
    url = m.card_url(N_IT) + "&derived=1"
    cache.put(url, CARD + f"<p>см. также № {other}</p>", fetched_at=AT)
    forged = ev.model_copy(update={"url": url, "value": other, "fact": ev.fact.replace(N_IT, other)})
    assert ev_check.value_in_page("contract", other, cache.page_text(url) or "")
    assert chk.check_evidence(forged, cache.page_text)


# --- review #273: incomplete answers are None, the supplier link is a fact -----------------------

_TOTAL_47 = "47 записей"
_FIRST_DATE = re.compile(r'(Заключение контракта</div>\s*<div class="data-block__value">)28\.12\.2024')
BEFORE_ALL = date(2024, 11, 1)  # every contract on SEARCH is concluded later: none is eligible


def _search_as(cache, body: str, as_of: date) -> m.Supply:
    cache.put(m.search_url(SUPPLIER, as_of), body, fetched_at=AT)
    return m.supplied(SUPPLIER, "62.01", as_of, cache=cache)


def test_complete_page_with_nothing_before_as_of_is_false(cache):
    """Derived: the total «47 записей» → «10 записей», so the page is the whole answer."""
    assert _search_as(cache, SEARCH.replace(_TOTAL_47, "10 записей"), BEFORE_ALL).supplied is False


def test_entry_without_date_makes_the_answer_none(cache):
    """Derived: as above, and the first entry lost its date «Заключение контракта»."""
    body, n = _FIRST_DATE.subn(r"\1", SEARCH.replace(_TOTAL_47, "10 записей"), count=1)
    assert n == 1
    res = _search_as(cache, body, BEFORE_ALL)
    assert res.supplied is None and res.error and "1770732915224000102" in res.error


@pytest.mark.parametrize("old, new", [
    ('registry-entry__header-mid__title">', 'registry-entry__header-mid__title-new">'),  # status not read
    ("Исполнение завершено", "Новая стадия"),  # a status outside the known stages
])
def test_entry_with_unread_or_unknown_status_makes_the_answer_none(cache, old, new):
    body = SEARCH.replace(_TOTAL_47, "10 записей").replace(old, new, 1)
    assert body != SEARCH.replace(_TOTAL_47, "10 записей")
    assert _search_as(cache, body, BEFORE_ALL).supplied is None


def test_card_without_readable_codes_is_not_opened(cache, monkeypatch):
    """Derived: CARD with the codes written without brackets — the positions table gives no code."""
    bare = CARD.replace("(62.01.11.000)", "62.01.11.000").replace("(62.02.20.130)", "62.02.20.130")
    card = m.parse_card(bare)
    assert card is not None and card.codes == ()
    _listed(monkeypatch, cache, None, CARD)
    cache.put(m.card_url(N_IT), bare, fetched_at=AT)
    res = m.supplied(SUPPLIER, "17.12", cache=cache)
    assert res.supplied is None and res.error and N_IT in res.error


LISTED = "1770732915224000102"  # first contract on the real SEARCH page


def _search_fact(url: str, number: str = LISTED) -> Evidence:
    fact = f"Госконтракт № {number} — в выдаче реестра контрактов ЕИС по фильтру «Поставщик»"
    return Evidence(source=m.SOURCE, url=url, fetched_at=datetime.fromtimestamp(AT, tz=UTC), field="contract",
                    value=number, fact=fact)


def test_search_fact_on_the_real_search_page(cache):
    url = m.search_url(SUPPLIER)
    cache.put(url, SEARCH, fetched_at=AT)
    assert chk.check_evidence(_search_fact(url), cache.page_text, SUPPLIER) == []
    assert chk.check_evidence(_search_fact(url, N_IT), cache.page_text, SUPPLIER)  # not in this answer
    # a 19-digit number that IS on the page but is a purchase notice, not a contract entry
    assert "0173100007824000071" in SEARCH
    assert chk.check_evidence(_search_fact(url, "0173100007824000071"), cache.page_text, SUPPLIER)
    assert chk.check_evidence(_search_fact(url), cache.page_text, "7820324037")  # filtered by another INN
    unfiltered = f"{m.BASE}{m.SEARCH_PATH}?searchString=%D0%BD%D0%B0%D0%BB%D0%BE%D0%B3"
    cache.put(unfiltered, SEARCH, fetched_at=AT)
    assert m.supplier_of(unfiltered) is None
    assert chk.check_evidence(_search_fact(unfiltered), cache.page_text)  # no supplier filter in the URL
