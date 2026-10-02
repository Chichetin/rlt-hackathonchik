"""Role of a counterparty by its main OKVED2 (#97, D-057): the rule, its basis as an Evidence on every
enrichment path (pb card, МСП registry by INN, ГИР БО, block «Новые»), the Evidence check of the role
and the filter «Только производители». Recorded answers only, no network."""

import json
from pathlib import Path

import pytest

from src.contracts import Query
from src.enrich import discover as D
from src.enrich.enrich import enrich
from src.enrich.evidence import check_counterparty, strip_unverified
from src.match.filters import apply_filters
from src.sources import bo_nalog, pb_nalog, rmsp, role
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures"
UL = "7804428656"  # ООО «БРАСС»: main OKVED 33.12 «Ремонт машин и оборудования» (pb card and МСП registry)
BIG = "7736050003"  # ПАО «Газпром»: not in the МСП registry, ГИР БО okved2 46.71.4
WEIGHTS = {"product_fit": 0.35, "experience": 0.2, "reliability": 0.2, "activity": 0.1, "geography": 0.1,
           "price": 0.05}


# --- the rule ------------------------------------------------------------------------------------

@pytest.mark.parametrize(("code", "kind"), [
    ("09.10", "unknown"),  # section B (mining services): the last code before manufacturing
    ("10", "manufacturer"), ("10.11", "manufacturer"), ("17.12", "manufacturer"), ("18.12", "manufacturer"),
    ("32.50", "manufacturer"), ("32.99", "manufacturer"),
    ("33", "unknown"), ("33.12", "unknown"), ("33.20", "unknown"),  # repair and installation: services
    ("34", "unknown"), ("35.11", "unknown"),
    ("45", "dealer"), ("45.11", "dealer"), ("45.32", "dealer"), ("45.40", "dealer"),
    ("45.2", "unknown"), ("45.20", "unknown"), ("45.20.1", "unknown"),  # maintenance and repair of vehicles
    ("46", "distributor"),  # the whole group «Торговля оптовая» without a subclass
    ("46.1", "dealer"), ("46.11.1", "dealer"), ("46.19", "dealer"),
    ("46.2", "distributor"), ("46.21", "distributor"), ("46.49.33", "distributor"), ("46.90", "distributor"),
    ("47", "dealer"), ("47.71.1", "dealer"),
    ("48", "unknown"), ("01.11", "unknown"), ("81.21", "unknown"), (" 17.12 ", "manufacturer"),
])
def test_rule_by_main_okved(code, kind):
    got = role.classify(code)
    assert got is not None and got[0] == kind and got[1]


@pytest.mark.parametrize("code", [None, "", " ", "1", "4", "46.", "46.4x", "x17.12", "１７.12", "17.12.1.1.1"])
def test_no_okved_no_role(code):
    assert role.classify(code) is None


@pytest.mark.parametrize(("code", "why"), [
    ("17.12", "обрабатывающее производство (раздел C, коды 10–32)"),
    ("33.12", "ремонт и монтаж машин и оборудования — услуги, не производство (класс 33)"),
    ("45.20", "техническое обслуживание и ремонт автотранспортных средств"),
    ("45.11", "торговля автотранспортными средствами, мотоциклами и их деталями — перепродавец"),
    ("01.11", "вне обрабатывающих производств и торговли — правило v1 роль не определяет"),
    ("08.12", "вне обрабатывающих производств и торговли — правило v1 роль не определяет"),
])
def test_basis_text(code, why):
    got = role.classify(code)
    assert got is not None and got[1] == why


def test_basis_is_only_the_main_okved_line_of_this_inn():
    inn = "7804428656"
    assert role.basis_kind("vyp.КодОКВЭД: 17.12", inn) == "manufacturer"  # pb card
    assert role.basis_kind("search.okved2main: 46.19", inn) == "dealer"  # pb search row
    assert role.basis_kind(f"row.{inn}.okved1: 46.19", inn) == "dealer"  # МСП registry
    assert role.basis_kind(f"row.{inn}.okved2: 46.71.4", inn) == "distributor"  # ГИР БО
    for value in (None, "", "46.71.4", "Ремонт машин и оборудования", "vyp.sulst_ex: 10", "search.periodcode: 12",
                  "search.okved2: 46.19", "query.okved1: 17.12", "row.7736050003.okved1: 17.12",
                  f"{inn}.okved1: 17.12", f"row.{inn}.okved1: 17.12.1.1.1", f"row.{inn}.okved1:17.12"):
        assert role.basis_kind(value, inn) is None, value
    assert role.basis_kind(f"row.{inn}.okved1: 17.12", None) is None  # no INN → only the pb keys count


def test_basis_fact_names_code_name_and_reason():
    facts: list[tuple[str, str, str]] = []

    def add(field: str, value: str, fact: str) -> bool:
        facts.append((field, value, fact))
        return True

    assert role.assign("17.12", "Производство бумаги и картона", "1.okved1: 17.12", add) == "manufacturer"
    assert facts == [("okved", "1.okved1: 17.12", "Роль — производитель: заявленный основной ОКВЭД 17.12 "
                      "«Производство бумаги и картона» — обрабатывающее производство (раздел C, коды 10–32)")]
    assert role.assign("81.21", None, "1.okved1: 81.21", add) == "unknown"
    assert facts[-1][2].startswith("Роль не определена: заявленный основной ОКВЭД 81.21 — ")
    facts.clear()
    assert role.assign(None, "x", "1.okved1: None", add) is None and facts == []  # no code → no role, no fact
    assert role.assign("17.12", None, "x", lambda *_: False) is None  # the fact was not added → no role


# --- enrich(): three paths -----------------------------------------------------------------------

@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ENRICH_OFFLINE", "1")
    return SourceCache(tmp_path / "c.sqlite")


def _card(cache: SourceCache, body: str | None = None) -> None:
    cache.put(pb_nalog.card_url(UL), body or (FIX / "pb_nalog" / f"{UL}.txt").read_text(encoding="utf-8"))


def _recorded(cache: SourceCache, name: str, url: str, flat) -> None:  # type: ignore[no-untyped-def]
    a = json.loads((FIX / "enrich_fallback" / name).read_text(encoding="utf-8"))
    cache.put(url, flat(a), fetched_at=a["recorded_at"])


def _basis(cp) -> list:  # type: ignore[no-untyped-def]
    return [e for e in cp.evidence if e.field == "okved" and e.fact.startswith("Роль")]


def test_pb_card_gives_role_with_basis(cache):
    """ООО «БРАСС», 33.12 «Ремонт машин и оборудования»: section C by number, a service by meaning —
    the exception of the rule (class 33 → роль не определена), with its own basis."""
    _card(cache)
    cp = enrich(UL, cache)
    assert cp.kind == "unknown" and cp.okved == ["33.12"]
    [ev] = _basis(cp)
    assert ev.value == "vyp.КодОКВЭД: 33.12" and ev.url == pb_nalog.card_url(UL) and ev.source == pb_nalog.SOURCE
    assert ev.fact == ("Роль не определена: заявленный основной ОКВЭД 33.12 «Ремонт машин и оборудования» — "
                       "ремонт и монтаж машин и оборудования — услуги, не производство (класс 33)")
    assert check_counterparty(cp, cache.page_text) == []
    assert strip_unverified(cp, cache.page_text).kind == "unknown"


def test_pb_card_of_a_producer(cache):
    _card(cache, (FIX / "pb_nalog" / f"{UL}.txt").read_text(encoding="utf-8").replace("33.12", "32.99"))
    cp = enrich(UL, cache)
    assert cp.kind == "manufacturer" and _basis(cp)[0].value == "vyp.КодОКВЭД: 32.99"
    assert check_counterparty(cp, cache.page_text) == []
    assert strip_unverified(cp, cache.page_text).kind == "manufacturer"


def test_pb_card_without_the_card_code_uses_the_search_row(cache):
    body = (FIX / "pb_nalog" / f"{UL}.txt").read_text(encoding="utf-8")
    _card(cache, "\n".join(x for x in body.splitlines() if not x.startswith("vyp.КодОКВЭД")).replace("33.12", "46.19"))
    cp = enrich(UL, cache)
    assert cp.kind == "dealer" and _basis(cp)[0].value == "search.okved2main: 46.19"
    assert strip_unverified(cp, cache.page_text).kind == "dealer"


def test_pb_card_without_okved_has_no_role(cache):
    body = (FIX / "pb_nalog" / f"{UL}.txt").read_text(encoding="utf-8")
    _card(cache, "\n".join(x for x in body.splitlines() if "ОКВЭД" not in x and ".okved2" not in x))
    cp = enrich(UL, cache)
    assert cp.okved == [] and cp.kind is None and _basis(cp) == []


def test_msp_registry_by_inn_gives_role(cache):
    _recorded(cache, "rmsp_quick_7804428656.json", rmsp.quick_url(UL), lambda a: rmsp.quick_flatten(UL, a))
    cp = enrich(UL, cache)
    assert cp.kind == "unknown"  # 33.12 — repair, the exception of the rule
    [ev] = _basis(cp)
    assert ev.value == f"row.{UL}.okved1: 33.12" and ev.url == rmsp.quick_url(UL) and ev.source == rmsp.SOURCE
    assert ev.fact.startswith("Роль не определена: заявленный основной ОКВЭД 33.12 «Ремонт машин и оборудования»")
    assert check_counterparty(cp, cache.page_text) == []
    assert strip_unverified(cp, cache.page_text).kind == "unknown"


def test_bo_gives_role_by_its_okved2_code(cache):
    _recorded(cache, "rmsp_quick_7736050003.json", rmsp.quick_url(BIG), lambda a: rmsp.quick_flatten(BIG, a))
    _recorded(cache, "bo_7736050003.json", bo_nalog.search_url(BIG), lambda a: bo_nalog.flatten(BIG, a))
    cp = enrich(BIG, cache)
    assert cp.kind == "distributor"
    [ev] = _basis(cp)
    assert ev.value == f"row.{BIG}.okved2: 46.71.4" and ev.url == bo_nalog.search_url(BIG)
    assert "46.71.4 — оптовая торговля" in ev.fact
    assert check_counterparty(cp, cache.page_text) == []
    assert strip_unverified(cp, cache.page_text).kind == "distributor"


def test_bo_short_code_is_backed_by_its_page_line(cache):
    """A 5-character code fails the text check as a bare value; the `row.<inn>.okved2: <code>` line passes."""
    _recorded(cache, "rmsp_quick_7736050003.json", rmsp.quick_url(BIG), lambda a: rmsp.quick_flatten(BIG, a))
    a = json.loads((FIX / "enrich_fallback" / "bo_7736050003.json").read_text(encoding="utf-8"))
    a["content"][0]["okved2"] = "35.11"
    cache.put(bo_nalog.search_url(BIG), bo_nalog.flatten(BIG, a))
    cp = enrich(BIG, cache)
    assert cp.kind == "unknown" and strip_unverified(cp, cache.page_text).kind == "unknown"


# --- the Evidence check of the role: honest and tampered cards ---------------------------------------

@pytest.fixture
def honest(cache):
    _recorded(cache, "rmsp_quick_7736050003.json", rmsp.quick_url(BIG), lambda a: rmsp.quick_flatten(BIG, a))
    _recorded(cache, "bo_7736050003.json", bo_nalog.search_url(BIG), lambda a: bo_nalog.flatten(BIG, a))
    cp = enrich(BIG, cache)
    assert cp.kind == "distributor" and check_counterparty(cp, cache.page_text) == []
    return cp


def _without_basis(cp):  # type: ignore[no-untyped-def]
    return [e for e in cp.evidence if e not in _basis(cp)]


def test_role_not_matching_its_basis_is_dropped(honest, cache):
    """The stated kind is recomputed from the code on the page: a «производитель» over 46.71.4 is a lie."""
    bad = honest.model_copy(update={"kind": "manufacturer"})
    assert any("роль" in e for e in check_counterparty(bad, cache.page_text))
    shown = strip_unverified(bad, cache.page_text)
    assert shown.kind is None and _basis(shown)  # the honest basis fact stays, the wrong role does not


def test_fabricated_basis_is_dropped_with_its_role(honest, cache):
    [ev] = _basis(honest)
    fake = ev.model_copy(update={"value": f"row.{BIG}.okved2: 17.12", "fact": "Роль — производитель: ОКВЭД 17.12"})
    bad = honest.model_copy(update={"kind": "manufacturer", "evidence": [*_without_basis(honest), fake]})
    assert check_counterparty(bad, cache.page_text)
    shown = strip_unverified(bad, cache.page_text)
    assert shown.kind is None and fake not in shown.evidence


@pytest.mark.parametrize(("value", "kind"), [
    ("vyp.sulst_ex: 10", "manufacturer"),  # a real line of the card, but not the OKVED: «10» is a status code
    ("search.periodcode: 12", "manufacturer"),
    ("vyp.КодОКВЭД: 46", "distributor"),  # a prefix of the real «vyp.КодОКВЭД: 46.19»
    ("search.okved2: 46.19", "dealer"),  # the right code under a key that is not the main OKVED
])
def test_basis_must_be_the_whole_main_okved_line_of_the_card(cache, value, kind):
    """Reviewer's forgeries on a pb card with 46.19: each value is on the page as a substring."""
    _card(cache, (FIX / "pb_nalog" / f"{UL}.txt").read_text(encoding="utf-8").replace("33.12", "46.19"))
    cp = enrich(UL, cache)
    assert cp.kind == "dealer" and check_counterparty(cp, cache.page_text) == []
    [ev] = _basis(cp)
    fake = ev.model_copy(update={"value": value, "fact": f"Роль — {kind}"})
    bad = cp.model_copy(update={"kind": kind, "evidence": [*_without_basis(cp), fake]})
    assert any("роль" in e for e in check_counterparty(bad, cache.page_text))
    assert strip_unverified(bad, cache.page_text).kind is None


def test_role_whose_basis_is_not_shown_is_dropped(honest, cache):
    """The basis line is on the page, but the fact carries a phone that is not: the fact is hidden — the role too."""
    [ev] = _basis(honest)
    noisy = ev.model_copy(update={"fact": f"{ev.fact}; тел. +7 (812) 555-12-34"})
    bad = honest.model_copy(update={"evidence": [*_without_basis(honest), noisy]})
    shown = strip_unverified(bad, cache.page_text)
    assert noisy not in shown.evidence and shown.kind is None and check_counterparty(bad, cache.page_text)


def test_role_without_basis_or_without_cached_page_is_dropped(honest, cache):
    no_basis = honest.model_copy(update={"evidence": _without_basis(honest)})
    assert strip_unverified(no_basis, cache.page_text).kind is None and check_counterparty(no_basis, cache.page_text)
    [ev] = _basis(honest)
    elsewhere = ev.model_copy(update={"url": "https://bo.nalog.gov.ru/not-cached"})
    moved = honest.model_copy(update={"evidence": [*_without_basis(honest), elsewhere]})
    assert strip_unverified(moved, cache.page_text).kind is None


def test_no_role_is_not_an_error(honest, cache):
    cp = honest.model_copy(update={"kind": None})
    assert check_counterparty(cp, cache.page_text) == [] and strip_unverified(cp, cache.page_text).kind is None


# --- block «Новые» and the filter «Только производители» ---------------------------------------------

@pytest.fixture
def recorded(tmp_path):
    """Real registry answers of the demo lot (tests/fixtures/rmsp/e2e): producers 17.12, wholesale 46.49.33."""
    c = SourceCache(tmp_path / "rmsp.sqlite")
    assert rmsp.seed(FIX / "rmsp" / "e2e", c) == 2
    return c


def _discover(cache: SourceCache, tmp_path: Path, **filters) -> D.Discovery:  # type: ignore[no-untyped-def]
    q = Query(query_id="q", text="Бумага офисная А4", okpd2=["17.12.14.110"], filters=filters)
    return D.discover(q, cache=cache, weights=WEIGHTS, processed=tmp_path / "no-processed", offline=True)


def test_discovered_companies_get_role_with_basis(recorded, tmp_path):
    res = _discover(recorded, tmp_path)
    kinds = {m.counterparty.okved[0]: set() for m in res.matches}
    for m in res.matches:
        cp = m.counterparty
        kinds[cp.okved[0]].add(cp.kind)
        [ev] = _basis(cp)
        assert ev.value == f"row.{cp.inn}.okved1: {cp.okved[0]}" and ev.url.startswith("https://rmsp.nalog.ru/")
        assert check_counterparty(cp, recorded.page_text) == []
        assert strip_unverified(cp, recorded.page_text).kind == cp.kind
    assert kinds == {"17.12": {"manufacturer"}, "46.49.33": {"distributor"}}


def test_search_page_lines_of_the_query_or_of_another_company_are_no_basis(recorded, tmp_path):
    """On the registry search page every line is a substring of the page; only this INN's okved1 counts."""
    every = _discover(recorded, tmp_path).matches
    maker = next(m.counterparty for m in every if m.counterparty.kind == "manufacturer")
    trader = next(m.counterparty for m in every if m.counterparty.kind == "distributor")
    [own] = _basis(maker)
    for value in ("query.okved1: 17.12", f"row.{maker.inn}.okved1: 17.12"):
        fake = own.model_copy(update={"value": value})  # the page of 17.12 rows, where the value is
        bad = trader.model_copy(update={"kind": "manufacturer", "evidence": [*_without_basis(trader), fake]})
        assert check_counterparty(bad, recorded.page_text) and strip_unverified(bad, recorded.page_text).kind is None
    assert strip_unverified(maker, recorded.page_text).kind == "manufacturer"


def test_only_manufacturers_keeps_the_producers_of_the_recorded_block(recorded, tmp_path):
    every = _discover(recorded, tmp_path).matches
    makers = [m for m in every if m.counterparty.okved == ["17.12"]]
    assert makers and len(makers) < len(every)  # both kinds are in the block
    shown = [m.model_copy(update={"counterparty": strip_unverified(m.counterparty, recorded.page_text)})
             for m in every]  # what the API filters and shows
    q = Query(query_id="q", text="x", filters={"only_manufacturers": True})
    assert apply_filters(shown, q) == [m for m in shown if m.counterparty.okved == ["17.12"]]
    filtered = _discover(recorded, tmp_path, only_manufacturers=True).matches
    assert filtered and all(m.counterparty.kind == "manufacturer" for m in filtered)
    assert {m.counterparty.inn for m in makers} <= {m.counterparty.inn for m in filtered}


# --- priority of the bases: registry → site → main OKVED (v0, #186) -------------------------------------------

@pytest.mark.parametrize("code", ["17.12", "33.12", "46.19", "46.49.33", "47.71.1", "45.20", "01.11", "81.21"])
def test_okved_alone_decides_exactly_as_the_rule(code):
    """Without a registry record and a site phrase nothing changes: the decision is the rule of D-057."""
    rule = role.classify(code)
    assert rule is not None
    d = role.decide(okved=role.okved_basis(code, "Название кода"))
    assert d.kind == rule[0] and not d.conflict
    assert d.basis is not None and d.basis.source == "okved" and rule[1] in d.basis.why and code in d.basis.why
    assert d.shown() == (d.basis,)


def test_no_basis_no_role():
    d = role.decide()
    assert d.kind is None and d.basis is None and d.shown() == () and not d.conflict
    assert "Роль не определена" in d.explanation()
    assert role.okved_basis(None) is None and role.okved_basis("46.") is None
    assert role.decide(okved=role.okved_basis("")).kind is None


def test_site_phrase_outranks_okved_and_both_are_returned_on_disagreement():
    site_b = role.site_basis("manufacturer", "собственное производство")
    okved_b = role.okved_basis("46.49.33", "Торговля оптовая писчебумажными товарами")
    d = role.decide(site=site_b, okved=okved_b)
    assert d.kind == "manufacturer" and d.basis is site_b and d.conflict
    assert d.shown() == (site_b, okved_b) and d.bases == (site_b, okved_b)
    text = d.explanation()
    assert "Роль — производитель: сайт компании" in text and "собственное производство" in text
    assert "расхождение: основной ОКВЭД — дистрибьютор" in text and "46.49.33" in text


def test_agreeing_bases_are_not_a_conflict():
    site_b, okved_b = role.site_basis("distributor", "официальный дистрибьютор"), role.okved_basis("46.49.33")
    d = role.decide(site=site_b, okved=okved_b)
    assert d.kind == "distributor" and d.basis is site_b and not d.conflict
    assert d.shown() == (site_b,) and d.bases == (site_b, okved_b) and "расхождение" not in d.explanation()


def test_registry_outranks_site_and_okved():
    reg = role.registry_basis("ГИСП, реестровая запись 1234\\1\\2025")
    site_b, okved_b = role.site_basis("dealer", "официальный дилер"), role.okved_basis("46.49.33")
    d = role.decide(registry=reg, site=site_b, okved=okved_b)
    assert d.kind == "manufacturer" and d.basis is reg and d.conflict
    assert d.shown() == (reg, site_b, okved_b)
    assert role.decide(registry=reg, okved=role.okved_basis("17.12")).conflict is False


def test_unknown_okved_neither_decides_against_a_site_nor_counts_as_disagreement():
    site_b, okved_b = role.site_basis("manufacturer", "завод-изготовитель"), role.okved_basis("33.12")
    assert okved_b is not None and okved_b.kind == "unknown"
    d = role.decide(site=site_b, okved=okved_b)
    assert d.kind == "manufacturer" and d.basis is site_b and not d.conflict and d.shown() == (site_b,)
    vague = role.Basis("site", "unknown", "на сайте нет определённой фразы")  # a higher basis that cannot tell
    lower = role.decide(site=vague, okved=role.okved_basis("17.12"))
    assert lower.kind == "manufacturer" and lower.basis is not None and lower.basis.source == "okved"
    assert not lower.conflict and lower.bases[0] is vague
    alone = role.decide(okved=okved_b)  # nothing else: «не определена» with its basis, as before
    assert alone.kind == "unknown" and alone.basis is okved_b and "Роль не определена" in alone.explanation()


def test_decision_keeps_the_evidence_of_each_basis():
    from datetime import UTC, datetime

    from src.contracts import Evidence

    ev = Evidence(source="сайт компании", url="https://x.example/", fetched_at=datetime(2026, 10, 2, tzinfo=UTC),
                  fact="На сайте указано: «официальный дилер»", field="site", value="официальный дилер")
    d = role.decide(site=role.site_basis("dealer", "официальный дилер", ev), okved=role.okved_basis("17.12"))
    assert d.basis is not None and d.basis.evidence is ev and d.conflict
    assert [b.source for b in d.shown()] == ["site", "okved"]
