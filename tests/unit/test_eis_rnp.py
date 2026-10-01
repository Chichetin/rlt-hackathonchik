"""ЕИС, РНП: parsing of recorded pages, Evidence on the cached page, and — the point of the file —
«not found in the РНП» is only given when the search verifiably worked (никогда не из ошибки).

Recorded 01.10.2026 19:41-19:43 МСК from zakupki.gov.ru (three requests, not mass):
- `list_10_entries.html` — the register without a query, 10 newest entries (44-ФЗ and 223-ФЗ numbers);
- `inn_9717122721_3_entries.html` — search by INN of a company with three current 223-ФЗ entries;
- `inn_7804428656_no_results.html` — search by INN of a company that is not in the register.
Pages marked «derived» below are these recordings with one edit, to reach states that three
requests did not show; the markup of an «Исключено» entry itself was NOT seen live (the status
filter of the page lists the two statuses «Размещено» / «Исключено»).
"""

import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src.contracts import Counterparty
from src.enrich.evidence import check_counterparty, check_evidence
from src.sources import eis_rnp
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "eis_rnp"
LIST_PAGE = (FIX / "list_10_entries.html").read_text(encoding="utf-8")
POS_PAGE = (FIX / "inn_9717122721_3_entries.html").read_text(encoding="utf-8")
NEG_PAGE = (FIX / "inn_7804428656_no_results.html").read_text(encoding="utf-8")
IN_RNP = "9717122721"  # ООО «Строймонтажатом», entries Р2621501, Р2621503, Р2621504 (01.10.2026)
NOT_IN_RNP = "7804428656"
OTHER_INN = "7720949660"  # valid INN of an unrelated listed company, for «page has no own entries»

POS_AT = datetime(2026, 10, 1, 16, 42, 21, tzinfo=UTC).timestamp()  # 19:42:21 МСК, when the page was recorded
NEG_AT = datetime(2026, 10, 1, 16, 43, 13, tzinfo=UTC).timestamp()

_STATUS_NEW = re.compile(r'(registry-entry__header-mid__title "?>\s*)Размещено')


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    monkeypatch.setattr(eis_rnp, "MIN_INTERVAL_S", 0.0)
    c = SourceCache()
    c.put(eis_rnp.search_url(IN_RNP), POS_PAGE, fetched_at=POS_AT)
    c.put(eis_rnp.search_url(NOT_IN_RNP), NEG_PAGE, fetched_at=NEG_AT)
    return c


def _check(cache: SourceCache, body: str, inn: str = IN_RNP) -> eis_rnp.RnpResult:
    cache.put(eis_rnp.search_url(inn), body, fetched_at=POS_AT)
    return eis_rnp.check(inn, cache)


def _all_valid(res: eis_rnp.RnpResult, cache: SourceCache) -> None:
    assert res.evidence
    for ev in res.evidence:
        assert check_evidence(ev, cache.page_text) == [], ev.fact
        assert "None" not in ev.fact and "«»" not in ev.fact  # an unknown value is left out, not printed


def _status(body: str, status: str, only: int | None = None) -> str:
    """derived: header status of the entries («Размещено» → status); `only` — just the first N entries."""
    return _STATUS_NEW.sub(lambda m: m[1] + status, body, count=only or 0)


def test_parse_list_page_has_both_registry_number_formats():
    entries, total, total_text = eis_rnp.parse(LIST_PAGE)
    assert len(entries) == 10
    numbers = {e.number for e in entries}
    assert "№ Р2621500" in numbers  # 223-ФЗ: a letter before the digits (was dropped by the first parser)
    assert "№ 26011096" in numbers  # 44-ФЗ / ПП 615: digits only
    assert {e.status for e in entries} == {"Размещено"} and all(e.current for e in entries)
    assert total is None and total_text is None  # «более 4 400 записей» is not a count we can trust
    assert next(e for e in entries if e.number == "№ Р2621500").inn == OTHER_INN


def test_parse_search_page_total():
    entries, total, total_text = eis_rnp.parse(POS_PAGE)
    assert [e.number for e in entries] == ["№ Р2621504", "№ Р2621501", "№ Р2621503"]
    assert total == 3 and total_text == "Результаты поиска 3 записей"
    assert all(e.inn == IN_RNP and e.included == "01.10.2026" and e.excluded is None for e in entries)
    assert eis_rnp.parse(NEG_PAGE) == ([], None, None)


def test_company_in_rnp_has_evidence(cache):
    res = eis_rnp.check(IN_RNP, cache)
    assert res.in_rnp is True
    (ev,) = res.evidence
    assert ev.field == "rnp" and ev.value == "№ Р2621504" and ev.url == eis_rnp.search_url(IN_RNP)
    assert "включено 01.10.2026" in ev.fact
    _all_valid(res, cache)


def test_company_not_found_is_dated_and_backed_by_the_page(cache):
    res = eis_rnp.check(NOT_IN_RNP, cache)
    assert res.in_rnp is False
    (ev,) = res.evidence
    assert ev.field == "rnp" and ev.value == eis_rnp.NO_RESULTS
    assert ev.fact.startswith("Не найдена в РНП ЕИС на 01.10.2026")  # a dated search, not «чистая»
    assert ev.fetched_at == datetime.fromtimestamp(NEG_AT, tz=UTC)
    _all_valid(res, cache)


def test_forged_rnp_value_is_caught(cache):
    ev = eis_rnp.check(IN_RNP, cache).evidence[0]
    assert check_evidence(ev.model_copy(update={"value": "№ Р2621999"}), cache.page_text)
    ev = eis_rnp.check(NOT_IN_RNP, cache).evidence[0]
    assert check_evidence(ev.model_copy(update={"value": "Записей нет"}), cache.page_text)


def test_invalid_inn_is_rejected_and_offline_without_cache_raises(cache):
    with pytest.raises(ValueError):
        eis_rnp.check("7804428650", cache)
    with pytest.raises(KeyError):
        eis_rnp.check(OTHER_INN, cache)  # ENRICH_OFFLINE=1, page not cached: no network, no guess


# --- absence is «не найдена на дату» only when the search verifiably worked ---------------------

def test_empty_entries_block_without_no_results_text_is_not_checked(cache):
    # derived: «Поиск не дал результатов» removed — an empty block without the server's own answer
    body = NEG_PAGE.replace(eis_rnp.NO_RESULTS, "")
    assert eis_rnp.NO_RESULTS not in body and "search-registry-entrys-block" in body
    assert _check(cache, body, NOT_IN_RNP).in_rnp is None


def test_no_results_text_next_to_entries_is_not_checked(cache):
    # derived: the contradictory page — entries present and «Поиск не дал результатов»
    body = POS_PAGE.replace("</body>", f'<p class="noRecords">{eis_rnp.NO_RESULTS}</p></body>')
    assert eis_rnp.NO_RESULTS in body
    res = _check(cache, body)
    assert res.in_rnp is None and res.evidence == []


def test_entries_not_parsed_is_not_checked(cache):
    # derived: layout changed (field titles renamed): total says 3, nothing parsed → не «чисто»
    body = POS_PAGE.replace("registry-entry__body-title", "registry-entry__body-label")
    assert eis_rnp.parse(body)[0] == [] and "3 записей" in body
    assert _check(cache, body).in_rnp is None


def test_incomplete_page_without_own_entry_is_not_checked(cache):
    # derived: 51 results, 3 on the page, none with this INN → the own entry may be on the next page
    body = POS_PAGE.replace(f">{IN_RNP}<", f">{OTHER_INN}<").replace(" 3 записей", " 51 записей")
    assert {e.inn for e in eis_rnp.parse(body)[0]} == {OTHER_INN}
    res = _check(cache, body)
    assert res.in_rnp is None and res.evidence == []


def test_complete_page_with_only_other_inns_is_not_found(cache):
    # derived: the search matched founders/managers (other INNs): complete page, none is ours
    body = POS_PAGE.replace(f">{IN_RNP}<", f">{OTHER_INN}<")
    res = _check(cache, body)
    assert res.in_rnp is False
    assert res.evidence[0].fact.startswith("Не найдена в РНП ЕИС на ") and res.evidence[0].value == (
        "Результаты поиска 3 записей")
    _all_valid(res, cache)


def test_own_current_entry_wins_over_incomplete_page(cache):
    body = POS_PAGE.replace(" 3 записей", " 51 записей")  # more results than on the page, ours is there
    assert _check(cache, body).in_rnp is True


def test_excluded_entries_are_history(cache):
    # derived: all three entries «Исключено» → was in РНП, now not; page complete
    res = _check(cache, _status(POS_PAGE, "Исключено"))
    assert res.in_rnp is False and res.evidence[0].fact.startswith("Был в РНП ЕИС: запись № Р2621504")
    _all_valid(res, cache)
    # the same, but the exclusion shows as a date block (Исключено 12.03.2026), status still «Размещено»
    dated = POS_PAGE.replace('<div class="data-block__title">Обновлено</div>',
                             '<div class="data-block__title">Исключено</div>\n'
                             '<div class="data-block__value">12.03.2026</div>\n'
                             '<div class="data-block__title">Обновлено</div>')
    assert eis_rnp.parse(dated)[0][0].excluded == "12.03.2026"
    res = _check(cache, dated)
    assert res.in_rnp is False and "исключено 12.03.2026" in res.evidence[0].fact


def test_one_current_entry_among_excluded_is_in_rnp(cache):
    res = _check(cache, _status(POS_PAGE, "Исключено", only=2))
    assert res.in_rnp is True and res.evidence[0].value == "№ Р2621503"


def test_excluded_entries_on_incomplete_page_are_not_checked(cache):
    body = _status(POS_PAGE, "Исключено").replace(" 3 записей", " 51 записей")
    assert _check(cache, body).in_rnp is None  # a current entry may be on another page


def test_unknown_status_is_not_called_clean(cache):
    res = _check(cache, _status(POS_PAGE, "Приостановлено"))
    assert res.in_rnp is None
    assert "статус «Приостановлено»" in res.evidence[0].fact


# --- review round: whole-number INN, contradictory «no results», Crawl-delay floor, MSK date, facts ---

def test_inn_with_a_suffix_in_the_entry_cell_is_not_checked_not_clean(cache):
    # derived: the cell reads «9717122721 (RU)»: it is the INN, but not equal to it — never «не найдена»
    body = POS_PAGE.replace(f">{IN_RNP}<", f">{IN_RNP} (RU)<")
    assert {e.inn for e in eis_rnp.parse(body)[0]} == {f"{IN_RNP} (RU)"}
    assert eis_rnp.mentions(body, IN_RNP) == 3
    res = _check(cache, body)
    assert res.in_rnp is None and res.evidence == []


def test_mentions_counts_whole_numbers_in_entry_blocks_only():
    assert eis_rnp.mentions(POS_PAGE, IN_RNP) == 3  # the query in the search form is not an entry
    assert eis_rnp.mentions(POS_PAGE, OTHER_INN) == 0
    for longer in (f">{IN_RNP}0<", f">1{IN_RNP}<"):  # another number that contains ours
        assert eis_rnp.mentions(POS_PAGE.replace(f">{IN_RNP}<", longer), IN_RNP) == 0
    in_name = POS_PAGE.replace("ООО «Строймонтажатом»", f"ООО «Строймонтажатом» ИНН {IN_RNP}")
    assert eis_rnp.mentions(in_name, IN_RNP) == 3  # any field of the block counts


def test_inn_only_in_a_name_field_is_not_checked(cache):
    # derived: the INN is in an entry (a founder's name field), no entry has it as its own INN
    body = POS_PAGE.replace(f">{IN_RNP}<", f">{OTHER_INN}<").replace(
        "ООО «Строймонтажатом»", f"ООО «Строймонтажатом» (учредитель, ИНН {IN_RNP})")
    assert {e.inn for e in eis_rnp.parse(body)[0]} == {OTHER_INN}
    assert _check(cache, body).in_rnp is None


@pytest.mark.parametrize("injected", [
    '<div class="search-registry-entry-block box-shadow-search-input"><div>unreadable</div></div>',
    '<div class="search-results__total"> Результаты поиска 3 записей </div>',
    '<div class="search-results__total"> Результаты поиска 0 записей </div>',
])
def test_no_results_text_with_raw_entry_blocks_or_a_total_is_not_checked(cache, injected):
    # derived: «Поиск не дал результатов» + something that says results exist (or may exist)
    block = '<div class="search-registry-entrys-block">'
    body = NEG_PAGE.replace(block, injected + block)
    assert eis_rnp.NO_RESULTS in body and injected in body
    res = _check(cache, body, NOT_IN_RNP)
    assert res.in_rnp is None and res.evidence == []


def test_crawl_delay_cannot_be_lowered_from_the_environment():
    code = (
        "import importlib, os\n"
        "from src.sources import eis_rnp\n"
        "out = []\n"
        "for raw in ('1', '0', '90', ''):\n"
        "    os.environ['RNP_MIN_INTERVAL_S'] = raw\n"
        "    out.append(importlib.reload(eis_rnp).MIN_INTERVAL_S)\n"
        "print(out)\n"
    )
    root = Path(__file__).resolve().parents[2]
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[60.0, 60.0, 90.0, 60.0]"  # robots.txt Crawl-delay: 60 is a floor


def test_search_date_is_moscow_time_not_utc(cache):
    late = datetime(2026, 10, 1, 22, 30, tzinfo=UTC)  # 01:30 on 02.10 in Moscow
    cache.put(eis_rnp.search_url(NOT_IN_RNP), NEG_PAGE, fetched_at=late.timestamp())
    ev = eis_rnp.check(NOT_IN_RNP, cache).evidence[0]
    assert ev.fact.startswith("Не найдена в РНП ЕИС на 02.10.2026") and ev.fetched_at == late
    early = datetime(2026, 10, 1, 20, 59, tzinfo=UTC)  # 23:59 on 01.10 in Moscow
    cache.put(eis_rnp.search_url(NOT_IN_RNP), NEG_PAGE, fetched_at=early.timestamp())
    assert eis_rnp.check(NOT_IN_RNP, cache).evidence[0].fact.startswith("Не найдена в РНП ЕИС на 01.10.2026")


def test_facts_leave_out_values_the_page_does_not_have(cache):
    # derived: no «Включено» block → no «включено None»
    no_date = POS_PAGE.replace('<div class="data-block__title">Включено</div>',
                               '<div class="data-block__title">Размещено в реестре</div>')
    assert eis_rnp.parse(no_date)[0][0].included is None
    res = _check(cache, no_date)
    assert res.in_rnp is True and "включено" not in res.evidence[0].fact
    _all_valid(res, cache)
    # excluded by status only: «статус «Исключено»», not «исключено Исключено»
    res = _check(cache, _status(POS_PAGE, "Исключено"))
    assert "статус «Исключено»" in res.evidence[0].fact and "исключено Исключено" not in res.evidence[0].fact
    _all_valid(res, cache)
    # status text missing in the header: unknown, said so
    res = _check(cache, _status(POS_PAGE, ""))
    assert res.in_rnp is None and "статус не указан" in res.evidence[0].fact
    _all_valid(res, cache)


# --- network: errors and captcha never become «не найдена» --------------------------------------

def _client(handler, calls=None):
    def wrapped(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped))


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "live.sqlite"))
    monkeypatch.delenv("ENRICH_OFFLINE", raising=False)
    monkeypatch.setattr(eis_rnp, "MIN_INTERVAL_S", 0.0)
    return SourceCache()


def test_live_search_is_cached_and_uses_the_inn_query(live):
    calls: list[str] = []
    client = _client(lambda r: httpx.Response(200, text=POS_PAGE), calls)
    assert eis_rnp.check(IN_RNP, live, client).in_rnp is True
    assert eis_rnp.check(IN_RNP, live, client).in_rnp is True
    assert len(calls) == 1
    assert f"searchString={IN_RNP}" in calls[0] and "recordsPerPage=_50" in calls[0]
    assert "strictEqual" not in calls[0]  # 01.10: strictEqual=true answered «нет» for an INN with 28 entries


def test_answer_older_than_a_day_is_refetched_a_fresh_one_is_not(live):
    url = eis_rnp.search_url(IN_RNP)
    calls: list[str] = []
    client = _client(lambda r: httpx.Response(200, text=POS_PAGE), calls)
    live.put(url, NEG_PAGE, fetched_at=time.time() - 3600)  # an hour old: used as is
    assert eis_rnp.check(IN_RNP, live, client).in_rnp is False and calls == []
    live.put(url, NEG_PAGE, fetched_at=time.time() - 2 * 24 * 3600)  # two days old: asked again
    assert eis_rnp.check(IN_RNP, live, client).in_rnp is True and len(calls) == 1


@pytest.mark.parametrize("response", [
    httpx.Response(403, text="Forbidden"),
    httpx.Response(429, text="Too Many Requests"),
    httpx.Response(200, text="<html><body>Введите код с картинки captcha</body></html>"),
    httpx.Response(200, text=""),
    httpx.Response(200, text="<html><head><title>Подождите…</title></head></html>"),
])
def test_blocked_or_foreign_page_raises_and_is_not_cached(live, response):
    client = _client(lambda r: response)
    with pytest.raises(eis_rnp.Blocked):
        eis_rnp.check(NOT_IN_RNP, live, client)
    assert live.get(eis_rnp.search_url(NOT_IN_RNP), allow_stale=True) is None


def test_server_error_raises_and_is_not_cached(live):
    client = _client(lambda r: httpx.Response(503, text="unavailable"))
    with pytest.raises(httpx.HTTPStatusError):
        eis_rnp.check(NOT_IN_RNP, live, client)
    assert live.get(eis_rnp.search_url(NOT_IN_RNP), allow_stale=True) is None


def test_network_failure_raises(live):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    with pytest.raises(httpx.ConnectTimeout):
        eis_rnp.check(NOT_IN_RNP, live, _client(boom))


def test_stale_answer_is_used_when_blocked_and_carries_its_own_date(live, monkeypatch):
    live.put(eis_rnp.search_url(NOT_IN_RNP), NEG_PAGE, fetched_at=NEG_AT)
    monkeypatch.setattr(eis_rnp, "MAX_AGE_S", -1.0)  # everything cached is stale: refetch → 403 → stale
    res = eis_rnp.check(NOT_IN_RNP, live, _client(lambda r: httpx.Response(403, text="WAF")))
    assert res.in_rnp is False
    assert res.evidence[0].fetched_at == datetime.fromtimestamp(NEG_AT, tz=UTC)  # shown with the fact
    assert res.evidence[0].fact.startswith("Не найдена в РНП ЕИС на 01.10.2026")


def test_requests_are_paced_by_crawl_delay(live, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(eis_rnp, "MIN_INTERVAL_S", 60.0)
    monkeypatch.setattr(eis_rnp, "_last_request", time.monotonic())
    monkeypatch.setattr(eis_rnp.time, "sleep", slept.append)
    client = _client(lambda r: httpx.Response(200, text=NEG_PAGE))
    eis_rnp.check(NOT_IN_RNP, live, client)
    assert len(slept) == 1 and 58 < slept[0] <= 60  # robots.txt Crawl-delay: 60


def test_counterparty_with_rnp_evidence_passes_the_evidence_check(cache):
    res = eis_rnp.check(IN_RNP, cache)
    inn_ev = res.evidence[0].model_copy(update={"field": "inn", "value": IN_RNP, "fact": "ИНН в записи РНП"})
    cp = Counterparty(inn=IN_RNP, name="ООО «Строймонтажатом»", in_rnp=res.in_rnp, evidence=[*res.evidence, inn_ev])
    assert check_counterparty(cp, cache.page_text) == []
