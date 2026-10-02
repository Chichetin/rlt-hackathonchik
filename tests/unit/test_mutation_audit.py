"""Tests added by the mutation audit of the core (#302, research/TEST_AUDIT.md).

Every test below is red on the surviving mutants named in its docstring (`cosmic-ray` operator, line of the
code at audit commit 0b7a6c1) and green on the code: the product already behaved correctly, the audit found
gaps in the tests, not bugs. They pin behaviour the older tests executed but did not assert: the exact digits
of the INN checksum, what counts as a phone, which Evidence backs which fact, the boundaries of the scoring
components. Style and fixtures follow the neighbours (test_evidence.py, test_filters.py, test_search.py).
"""

from __future__ import annotations

import json
from datetime import date
from itertools import product
from pathlib import Path

import pytest

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query, ScoreComponent, inn_is_valid
from src.enrich.evidence import check_counterparty, check_evidence, phones, strip_unverified, value_in_page
from src.match import filters as F
from src.match import search as S

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "evidence"
PAGE = (FIX / "page.html").read_text(encoding="utf-8")
URL = "https://example.org/rekvizity"
SITE_INN = "0000100012"  # the INN written on the fixture page
EMAIL = "sales@example.org"  # the e-mail written on the fixture page (as Sales@Example.org)
DIGITS = "0123456789"


def page_text(url: str) -> str | None:
    return PAGE if url == URL else None


def ev(field, value, fact="x", url=URL, source="сайт компании") -> Evidence:
    return Evidence(source=source, url=url, fetched_at="2026-09-30T12:00:00Z",  # type: ignore[arg-type]
                    fact=fact, field=field, value=value)


# --- src/contracts.py::inn_is_valid ---------------------------------------------------------------------------------

@pytest.mark.parametrize(("head", "tail"), [
    ("770708389", ["3"]), ("783000229", ["3"]),  # 10 digits: one control digit
    ("5001007322", ["59"]), ("7801000000", ["90"]),  # 12 digits: two control digits, the second one covers the first
])
def test_exactly_one_set_of_control_digits_completes_an_inn(head, tail):
    """Kills `==` -> `<=` / `>=` on both 12-digit control digits (inn_is_valid, line 46): a checksum that only
    rejects a control digit on one side of the right value would pass the wrong digits on the other side."""
    ok = ["".join(t) for t in product(DIGITS, repeat=len(tail[0])) if inn_is_valid(head + "".join(t))]
    assert ok == tail


# --- src/enrich/evidence.py -----------------------------------------------------------------------------------------

def test_a_phone_is_a_prefix_or_separators_not_a_bare_run():
    """Kills `or` -> `and` and `len(d) == 11` -> `> 11` / `== 12` in `_phones` (line 57): 11 digits without
    separators and 10 digits with separators are both phones; and the 10-digit value of an Evidence is
    found on the page (kills `len(want) in (10, 11)` -> `(9, 11)` / `(11, 11)`, line 121)."""
    assert phones("+78120000001") == {"8120000001"}  # prefix, no separators
    assert phones("(812) 000-00-01") == {"8120000001"}  # separators, no prefix
    assert phones("8120000001") == set()  # a bare 10-digit run is an INN / OGRN fragment
    assert value_in_page("phone", "812 000-00-01", PAGE)  # 10 digits in the Evidence, +7 and 11 on the page
    assert not value_in_page("phone", "000-00-01", PAGE)  # 7 digits are not a phone


def test_text_fact_needs_min_text_characters_exactly():
    """Kills MIN_TEXT = 7 / 5 and `<` -> `<=` in `_in_page` (lines 35, 128): a 6-character value that is on the
    page counts, a 5-character one does not, even though both are there."""
    page = "Склад: Москва, Томск"
    assert value_in_page("name", "Москва", page)  # 6 = MIN_TEXT
    assert not value_in_page("name", "Томск", page)  # 5 < MIN_TEXT


@pytest.mark.parametrize(("field", "value", "source", "error"), [
    (None, EMAIL, "сайт компании", "без field/value"),  # no field: what is it?
    # no value: nothing to look for (a mutant that skips this check would crash on `None.isascii()`)
    ("email", None, "сайт компании", "без field/value"),
    ("inn", SITE_INN, "data:contracts", "не соответствует схеме"),  # a web url with an organizers' source
])
def test_malformed_evidence_is_an_error_not_a_pass_or_a_crash(field, value, source, error):
    """Kills `or` -> `and` (check_evidence, line 147) and `!=` -> `>` of the url/source scheme check (line 140)."""
    errors = check_evidence(ev(field, value, source=source), page_text)
    assert any(error in e for e in errors), errors


def test_mentions_in_the_fact_text_must_be_on_the_page_too():
    """Kills ZeroIterationForLoop on the e-mail loop and on the INN-like number loop (check_evidence, lines 161,
    163): the older tests checked only a phone in the fact, not an e-mail or a 10-digit number."""
    address = "Санкт-Петербург, ул. Тестовая, д. 1"
    assert check_evidence(ev("address", address, fact=f"{EMAIL}, ИНН {SITE_INN}"), page_text) == []  # all on the page
    errors = check_evidence(ev("address", address, fact="Пишите на nobody@example.net"), page_text)
    assert any("email nobody@example.net из текста факта" in e for e in errors), errors
    errors = check_evidence(ev("address", address, fact="ИНН 7707083893"), page_text)
    assert any("число 7707083893 из текста факта" in e for e in errors), errors


@pytest.mark.parametrize("other", ["a@example.org", "z@example.org"])  # below and above the verified e-mail
def test_contact_is_backed_by_evidence_of_its_own_value_not_a_neighbouring_one(other):
    """Kills `==` -> `<=` / `>=` on e.value in `_backed` (line 170) and in the contacts rule of
    check_counterparty (line 192): a mutant would let an e-mail that is on the page back any contact on one side
    of it."""
    cp = Counterparty(inn=None, name="x", contacts={"email": other}, evidence=[ev("email", EMAIL)])
    assert strip_unverified(cp, page_text).contacts == {}
    assert any(f"контакт email={other!r} без Evidence" in e for e in check_counterparty(cp, page_text))


@pytest.mark.parametrize("field", ["site", "address"])  # alphabetically after / before "email"
def test_contact_is_backed_by_evidence_of_its_own_field_not_a_neighbouring_one(field):
    """Kills `==` -> `<=` / `>=` on e.field in `_backed` (line 170) and in check_counterparty (line 192): the
    page text is on the page, but an Evidence for the address does not back the e-mail."""
    cp = Counterparty(inn=None, name="x", contacts={"email": EMAIL}, evidence=[ev(field, EMAIL)])
    assert check_evidence(cp.evidence[0], page_text) == []  # the Evidence itself is honest
    assert strip_unverified(cp, page_text).contacts == {}
    assert any("контакт email" in e and "без Evidence" in e for e in check_counterparty(cp, page_text))


@pytest.mark.parametrize(("field", "value"), [
    ("ogrn", SITE_INN), ("address", SITE_INN),  # another field, the value is right (and on the page)
    ("inn", "0000100020"), ("inn", "0000100004"),  # the right field, another INN, above and below
])
def test_inn_is_backed_by_an_inn_evidence_of_its_own_value(field, value):
    """Kills `==` -> `<=` / `>=` on e.field / e.value and `and` -> `or` in the INN rule of check_counterparty
    (line 189): only an Evidence with field='inn' and this very value backs the INN."""
    cp = Counterparty(inn=SITE_INN, name="x", evidence=[ev(field, value)])
    assert f"ИНН {SITE_INN} без Evidence с field='inn'" in check_counterparty(cp, page_text)


def test_strip_unverified_hides_an_inn_the_page_does_not_have():
    """Kills AddNot on `cp.inn is None` in strip_unverified (line 204): a mutant that kept a made-up INN (valid
    checksum, not on the page) survived the old tests — it would be shown, the blocker of the Evidence layer."""
    fabricated = Counterparty(**json.loads((FIX / "fabricated_inn.json").read_text(encoding="utf-8")))
    assert fabricated.inn == "0000100020"
    assert strip_unverified(fabricated, page_text).inn is None
    honest = Counterparty(inn=SITE_INN, name="x", evidence=[ev("inn", SITE_INN)])
    assert strip_unverified(honest, page_text).inn == SITE_INN


OKVED_PAGE = "Карточка компании\nvyp.КодОКВЭД: 17.12\nvyp.Тел: 0\n"


@pytest.mark.parametrize("field", ["status", "name"])  # alphabetically after / before "okved"
def test_role_is_backed_by_an_okved_evidence_only(field):
    """Kills `!=` -> `<` / `>` and `or` -> `and` on e.field in `_kind_backed` (line 176): the whole line
    `vyp.КодОКВЭД: 17.12` is on the page, but only an Evidence with field='okved' backs the role."""
    basis = "vyp.КодОКВЭД: 17.12"
    pages = {URL: OKVED_PAGE}.get
    cp = Counterparty(inn=None, name="x", kind="manufacturer", evidence=[ev("okved", basis)])
    assert check_counterparty(cp, pages) == []  # the control: an okved Evidence backs it
    wrong = cp.model_copy(update={"evidence": [ev(field, basis)]})
    assert check_evidence(wrong.evidence[0], pages) == []  # the Evidence itself is honest
    assert any("роль manufacturer без подтверждённого" in e for e in check_counterparty(wrong, pages))
    assert strip_unverified(wrong, pages).kind is None


# --- src/match/filters.py -------------------------------------------------------------------------------------------

def _cp(**kw) -> Counterparty:
    return Counterparty(inn=kw.pop("inn", None), name="x", **kw)


def _match(cp: Counterparty) -> CounterpartyMatch:
    return CounterpartyMatch(counterparty=cp, score=0.5, origin="history",
                             components=[ScoreComponent(name="product_fit", value=0.5, weight=1, explanation="x")])


def test_kpp_region_is_its_two_first_digits_and_a_shorter_kpp_is_unknown():
    """Kills `len(cp.kpp) >= 2` -> `> 2` / `>= 1` / `!= 2` / `>= 3` in `in_region` (line 85)."""
    spb = "Санкт-Петербург"
    assert F.in_region(_cp(kpp="78"), spb) is True and F.in_region(_cp(kpp="47"), spb) is False
    assert F.in_region(_cp(kpp="7"), spb) is None and F.in_region(_cp(kpp=None), spb) is None


@pytest.mark.parametrize(("kpp", "region"), [
    ("7A0101001", None),  # the second character is not a digit: not a region (`[:1]`)
    ("78A101001", "78"),  # the region is the first TWO digits, the third character is not looked at (`[:3]`)
])
def test_supplier_region_reads_exactly_two_kpp_characters(kpp, region):
    """Kills `kpp[:2].isdigit()` -> `kpp[:1]` / `kpp[:3]` in `supplier_region` (line 116): the contract puts no
    format on a KPP (`str | None`), so the rule «the region is its first two digits» is pinned on both sides."""
    assert F.supplier_region(_cp(inn="7801000012", kpp=kpp)) == region


def test_filters_without_a_customer_region_code_mark_nobody_and_a_choice_of_any_is_not_an_error():
    """Kills `return False, False` -> `(False, True)` / `(True, False)` in `near_marks` (line 126),
    `==` -> `<=` on "auto" and `!=` -> `>=` on "any" in `near_of` (lines 144, 146)."""
    moscow = Query(query_id="q", text="x", region="Москва", filters={"near_customer": "any"})
    nc = F.near_of(moscow)  # no code for Moscow, choice any: not an error
    assert nc.code is None and nc.choice == "any"
    rows = [_match(_cp(kpp="780101001")), _match(_cp(kpp="470101001")), _match(_cp(inn=None))]
    assert F.near_marks(rows[0].counterparty, nc) == (False, False)
    assert F.near_facets(rows, nc) == {"worked": 0, "near": 0, "any": 3}
    # an explicit "any" is not the purchase-type default: works (43.39.11) would give "near" for "auto"
    works = Query(query_id="q", text="x", okpd2=["43.39.11.000"], filters={"near_customer": "any"})
    assert F.near_of(works).choice == "any"
    assert F.near_of(works.model_copy(update={"filters": {"near_customer": "auto"}})).choice == "near"


@pytest.mark.parametrize("value", ["", True, 1.5])
def test_region_filter_ignores_a_value_that_is_not_a_region_name(value):
    """Kills `and` -> `or` in `parse_filters` (line 211) and `only_msp: bool = False` -> `True` (line 63)."""
    assert F.parse_filters({"region": value}).region is None
    assert F.parse_filters({"region": "Санкт-Петербург"}).region == "Санкт-Петербург"
    nothing = F.UserFilters()  # the default is «no filter» (parse_filters always passes only_msp)
    assert F.keeps(_match(_cp()), nothing)


# --- src/match/search.py: scoring components ------------------------------------------------------------------------

def _cand(**kw) -> S.Candidate:
    base = {"inn": "7801000012", "bm25": 1.0, "n_lots": 3, "n_wins": 1, "best_lot": 7, "best_text": "бумага",
            "best_date": date(2025, 1, 1)}
    return S.Candidate(**{**base, **kw})


def test_best_text_is_cut_to_sixty_characters_in_the_product_fit_line():
    """Kills `<= 60` -> `<= 59` / `> 60` / `== 60` / `>= 60`, AddNot and `[:57]` -> `[:56]` in `_product_fit`
    (line 270): 60 characters are shown whole, 61 are cut to 57 and an ellipsis."""
    for n, shown in ((59, "а" * 59), (60, "а" * 60), (61, "а" * 57 + "…")):
        _, why = S._product_fit(_cand(best_text="а" * n), 1.0, [], False)
        assert f"«{shown}»" in why, (n, why)


@pytest.mark.parametrize(("level", "code"), [(4, "17.12.14.110"), (3, "17.12.14"), (2, "17.12"), (1, "17")])
def test_product_fit_names_every_depth_of_the_okpd2_match(level, code):
    """Kills `okpd_level == 0` -> `== 1` in `_product_fit` (line 276): a supplier that matched the query's class
    only would be told that the class does not occur in its history."""
    value, why = S._product_fit(_cand(okpd_level=level, okpd_code=code), 1.0, ["17.12.14.110"], False)
    assert S.OKPD_LEVEL_TEXT[level] in why and "не встречается" not in why
    assert value == pytest.approx(S.FIT_BM25_SHARE + (1 - S.FIT_BM25_SHARE) * S.OKPD_DEPTH[level])


@pytest.mark.parametrize(("level", "code"), [(0, None), (0, "17.12.14.110"), (2, None)])
def test_product_fit_without_a_usable_match_says_the_class_is_not_in_the_history(level, code):
    """Kills `or` -> `and` in `_product_fit` (line 276): level 0 or no code alone is «not found» (the mutant
    went on to `_prefix(None, 2)` / `_prefix(code, 0)` and raised)."""
    _, why = S._product_fit(_cand(okpd_level=level, okpd_code=code), 1.0, ["17.12.14.110"], True)
    assert "ОКПД2 определён по похожим позициям 17.12.14.110 в его истории не встречается" in why


def test_experience_names_the_group_of_a_short_okpd2_code():
    """Kills `x[2] or x[0]` -> `x[-1]` / `x[1]` in `_experience` (line 285): a three-character code (32.5) has
    no level-4 prefix and stands for itself, not for its class 32 or for nothing."""
    value, why = S._experience(_cand(class_lots=2, class_wins=1), ["32.5"], None)
    assert why.startswith("2 лот(ов) по ОКПД2 32.5") and "по ОКПД2 32 " not in why
    assert value == pytest.approx(0.5 * S._sat(2, S.SAT_LOTS) + 0.5 * S._sat(1, S.SAT_WINS))


@pytest.mark.parametrize(("stats", "ref"), [({}, date(2025, 7, 1)), ({"last_seen": "2025-06-01"}, None)])
def test_activity_without_a_last_participation_or_a_reference_date_is_no_data(stats, ref):
    """Kills `or` -> `and` in `_activity` (line 315, the mutant raised on `fromisoformat(None)`) and
    `return 0.0` -> `1.0` (line 316): no data is a zero, not a perfectly active supplier."""
    cp = Counterparty(inn=None, name="x", stats=stats)
    assert S._activity(cp, ref, None) == (0.0, "Нет данных о последнем участии")


def test_activity_explanation_names_the_date_it_counts_to():
    """Kills AddNot on `as_of` in `_activity` (line 318): with a cutoff — «до 01.07.2025», without — the end of the
    data; and `max(0, days)` -> `max(-1, days)` (line 317): a participation after the reference date is 0 days."""
    cp = Counterparty(inn=None, name="x", stats={"last_seen": "2025-06-01"})
    ref = date(2025, 7, 1)
    value, why = S._activity(cp, ref, ref)
    assert value == pytest.approx(0.5 ** (30 / S.HALF_LIFE_DAYS))
    assert why == "Последнее участие 01.06.2025 — 30 дн. до 01.07.2025"
    assert S._activity(cp, ref, None)[1] == "Последнее участие 01.06.2025 — 30 дн. до конца данных (01.07.2025)"
    later = Counterparty(inn=None, name="x", stats={"last_seen": "2025-07-05"})  # after the reference: 0 days, not -1
    assert S._activity(later, ref, ref) == (1.0, "Последнее участие 05.07.2025 — 0 дн. до 01.07.2025")


@pytest.mark.parametrize(("customer_inn", "region", "note"), [
    ("780100000090", "Санкт-Петербург", True),  # an ИП customer: 12 digits (kills `(10, 12)` -> `(10, 13)`, line 346)
    ("78123", "Москва", False),  # digits but not an INN length (kills `and` -> `or`)
    ("78abcdefgh", "Москва", False),  # an INN length but not digits (kills `and` -> `or`)
])
def test_customer_region_takes_only_a_well_formed_inn(customer_inn, region, note):
    q = Query(query_id="q", text="x", region="Москва", customer_inn=customer_inn)
    got_region, got_note = S.customer_region(q)
    assert got_region == region and (got_note is not None) is note


@pytest.mark.parametrize(("kpp", "value", "why"), [
    (None, 0.0, "КПП нет (ИП или не указан)"), ("7", 0.0, "КПП нет (ИП или не указан)"),  # shorter than 2: no KPP
    ("00", 0.0, "заглушка из нулей"), ("000000000", 0.0, "заглушка из нулей"),  # zeros: a stub, not region 00
    ("78", 1.0, "на налоговом учёте в Санкт-Петербурге"),  # two characters are already a region
    ("770101001", 0.0, "на учёте в регионе с кодом 77, регион запроса — Санкт-Петербург"),  # another region: zero
    ("160101001", 0.0, "на учёте в регионе с кодом 16, регион запроса — Санкт-Петербург"),  # below the neighbour's 47
    ("470101001", S.NEIGHBOUR_VALUE, "соседний регион — Ленинградская область"),
])
def test_geography_by_kpp_boundaries_and_other_regions(kpp, value, why):
    """Kills `len(kpp) < 2` -> `== 2` / `< 3` (line 354), the same in the explanation choice -> `>= 2` / `<= 2` /
    `== 2` (line 355) and `return 0.0` -> `1.0` / `-1.0` for another region (line 365)."""
    got_value, got_why = S._geography(Counterparty(inn=None, name="x", kpp=kpp), "Санкт-Петербург")
    assert got_value == value and why in got_why, got_why


def test_geography_of_a_query_region_without_a_kpp_code_is_zero_not_negative():
    """Kills `return 0.0` -> `-1.0` in `_geography` (line 360): the value is a [0, 1] component, not a penalty."""
    value, why = S._geography(Counterparty(inn=None, name="x", kpp="780101001"), "Москва")
    assert value == 0.0 and "не сопоставлен с кодом КПП" in why


@pytest.mark.parametrize(("inn", "value"), [
    ("4701000038", S.NEIGHBOUR_VALUE),  # Leningrad region for a St Petersburg query: the neighbour (`==` -> `<=`)
    ("500100732259", 0.0), ("7707083893", 0.0),  # Moscow region, Moscow: above the neighbour's 47 (kills `==` -> `<=`)
    ("1655000010", 0.0),  # Tatarstan: below the neighbour's 47 (kills `==` -> `>=`)
])
def test_inn_region_signal_is_full_in_the_region_part_in_the_neighbour_and_zero_elsewhere(inn, value):
    """Kills `==` -> `<=` / `>=` and `and` -> `or` in `_inn_geography` (lines 329, 331) and `return 0.0` -> `1.0` /
    `-1.0` (line 333): a mutant would count every INN prefix on one side of the neighbour's code as the neighbour."""
    got, why = S._geography(Counterparty(inn=inn, name="x"), "Санкт-Петербург", 1.0)
    assert got == pytest.approx(value) and "ИНН выдан" in why and ("соседний" in why) is (value > 0), why


def test_inn_region_signal_without_an_inn_is_no_region():
    """Kills `or` -> `and` in `_inn_geography` (line 324): with neither KPP nor INN the mutant raises on `None[:2]`."""
    value, why = S._geography(Counterparty(inn=None, name="x"), "Санкт-Петербург", 1.0)
    assert value == 0.0 and why.endswith("регион не определён")


def test_component_value_is_clamped_to_the_unit_interval():
    """Kills `max(0.0, v)` -> `max(-1.0, v)` and `min(1.0, ..)` -> `min(2.0, ..)` in `_component` (line 369):
    the mutants passed an out-of-range value on to ScoreComponent, which then refused it."""
    weights = {"experience": 0.2}
    assert S._component("experience", 1.7, weights, "x").value == 1.0
    assert S._component("experience", -0.3, weights, "x").value == 0.0
    got = S._component("experience", 0.123456, weights, "x")
    assert got.value == 0.1235 and got.weight == 0.2 and S._component("price", 0.5, weights, "x").weight == 0.0
