"""Role for a service or works purchase (#188): `unknown` with the basis «roles do not apply to services
(works); the executor by the main OKVED», built on the verified main-OKVED line — and licences of the МСП dump
as card facts. Fixture records of the dump (tests/fixtures/msp_dump), no network."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query
from src.enrich import discover as discover_mod
from src.enrich import role
from src.enrich.evidence import check_counterparty, strip_unverified
from src.match import pipeline
from src.sources import msp_dump
from src.sources.cache import SourceCache

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "msp_dump" / "sample.xml"
AT = datetime(2026, 9, 12, tzinfo=UTC)
UL = "0078000013"  # ООО «ТЕСТОВАЯ БУМАГА»: main OKVED 17.12 «Производство бумаги и картона», one licence
IP = "007800000309"  # ИП: main OKVED 43.39 (construction): unknown by the rule v1 already
SERVICE_PREFIX = "Роль не определена: роли производитель / дистрибьютор / дилер к "


def _rows() -> dict[str, dict]:
    return {r["inn"]: r for r in msp_dump.parse_file(FIXTURE.read_bytes(), "sample.xml")}


@pytest.fixture
def cached(tmp_path) -> tuple[SourceCache, dict[str, Counterparty]]:
    """The dump companies with their pages in the source cache, as src/db/pool.py puts them."""
    cache = SourceCache(tmp_path / "sources.sqlite")
    cps = {}
    for inn in (UL, IP):
        row = _rows()[inn]
        cache.put(msp_dump.page_url(inn), msp_dump.page(row), fetched_at=AT.timestamp())
        cp = msp_dump.counterparty(row, AT)
        assert cp is not None
        cps[inn] = cp
    return cache, cps


def _role_facts(cp: Counterparty) -> list[str]:
    return [e.fact for e in cp.evidence if e.fact.startswith("Роль")]


# --- the role for a service ------------------------------------------------------------------------

@pytest.mark.parametrize("purchase, dative", [("services", "услугам"), ("remote", "услугам"), ("works", "работам")])
def test_manufacturer_is_unknown_for_a_service_with_the_basis_by_its_main_okved(cached, purchase, dative):
    cache, cps = cached
    cp = role.for_purchase(cps[UL], purchase)
    assert cp.kind == "unknown"
    assert _role_facts(cp) == [
        f"{SERVICE_PREFIX}{dative} не применяются; исполнитель по основному ОКВЭД 17.12 "
        "«Производство бумаги и картона»"]
    basis = next(e for e in cp.evidence if e.fact.startswith(SERVICE_PREFIX))
    assert basis.field == "okved" and basis.value == f"row.{UL}.okved1: 17.12"  # the verified main-OKVED line
    assert check_counterparty(cp, cache.page_text) == []
    assert strip_unverified(cp, cache.page_text) == cp  # the card keeps the role and its basis
    assert len(cp.evidence) == len(cps[UL].evidence)  # the role fact is replaced, nothing else changes


def test_unknown_by_the_rule_gets_the_service_basis_too(cached):
    cache, cps = cached
    cp = role.for_purchase(cps[IP], "works")
    assert cp.kind == "unknown"
    assert _role_facts(cp) == [f"{SERVICE_PREFIX}работам не применяются; исполнитель по основному ОКВЭД 43.39 "
                               "«Производство прочих отделочных и завершающих работ»"]
    assert check_counterparty(cp, cache.page_text) == []


@pytest.mark.parametrize("purchase", ["goods", None])
def test_goods_or_unknown_type_keep_the_role_of_the_rule(cached, purchase):
    _, cps = cached
    assert role.for_purchase(cps[UL], purchase) == cps[UL]


def test_no_role_or_no_basis_stays_as_is(cached):
    _, cps = cached
    bare = Counterparty(inn=UL, name="ООО Тест")
    assert role.for_purchase(bare, "services") == bare  # no main OKVED: no role to replace, None stays None
    unbacked = cps[UL].model_copy(update={"evidence": [e for e in cps[UL].evidence if not e.fact.startswith("Роль")]})
    assert role.for_purchase(unbacked, "services") == unbacked  # a role without a basis is not dressed up


def test_service_basis_of_a_forged_line_fails_the_check(cached):
    cache, cps = cached
    cp = role.for_purchase(cps[UL], "services")
    forged = [e.model_copy(update={"value": f"row.{UL}.okved1: 81.21"}) if e.fact.startswith(SERVICE_PREFIX) else e
              for e in cp.evidence]
    bad = cp.model_copy(update={"evidence": forged})
    assert any("роль" in err for err in check_counterparty(bad, cache.page_text))
    assert strip_unverified(bad, cache.page_text).kind is None


def test_other_roles_need_their_own_rule_not_the_service_basis(cached):
    cache, cps = cached
    cp = role.for_purchase(cps[UL], "services")
    claimed = cp.model_copy(update={"kind": "distributor"})  # 17.12 gives manufacturer; the service text is no basis
    assert any("роль" in err for err in check_counterparty(claimed, cache.page_text))
    plain = cps[UL].model_copy(update={"kind": "unknown"})  # unknown without the service basis: 17.12 is a maker
    assert any("роль" in err for err in check_counterparty(plain, cache.page_text))


# --- the pipeline: the role of the shown card follows the purchase type ---------------------------

@pytest.fixture
def discovered(search_data, cached, tmp_path, monkeypatch):
    """«Новые» = the dump companies, the source cache = their pages; history from the fixture data."""
    cache, cps = cached
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    found = [CounterpartyMatch(counterparty=cp, score=0.5, components=[], origin="discovered") for cp in cps.values()]
    monkeypatch.setattr(discover_mod, "discover", lambda q, h, **k: discover_mod.Discovery(
        matches=found, candidates=found, history_checked=True))
    return cps


def _query(okpd2: list[str], **kw) -> Query:
    return Query(query_id="q", text="бумага офисная А4", okpd2=okpd2, **kw)


def _new(picked: pipeline.Picked) -> dict[str, Counterparty]:
    return {m.counterparty.inn or "": m.counterparty for m in pipeline.verified(picked.discovered)}


def test_pipeline_service_query_shows_unknown_with_the_basis(discovered):
    res = pipeline.pick(_query(["81.21.10.000"]))  # cleaning: a service
    shown = _new(res)
    assert shown[UL].kind == "unknown" and shown[IP].kind == "unknown"
    assert any(f.startswith(SERVICE_PREFIX + "услугам") for f in _role_facts(shown[UL]))
    assert res.facets["discovered"]["kind"].get("manufacturer", 0) == 0  # the chips count what is shown
    only_makers = pipeline.pick(_query(["81.21.10.000"], filters={"kinds": "manufacturer"}))
    assert only_makers.discovered == []


def test_pipeline_goods_query_keeps_the_manufacturer(discovered):
    shown = _new(pipeline.pick(_query(["17.12.14.110"])))
    assert shown[UL].kind == "manufacturer" and _role_facts(shown[UL])[0].startswith("Роль — производитель")


# --- licences of the МСП dump as card facts -------------------------------------------------------

def test_licence_is_a_card_fact_on_the_cached_page(cached):
    cache, cps = cached
    lic = [e for e in cps[UL].evidence if e.field == "licence"]
    text = "Заготовка, хранение, переработка и реализация лома черных металлов"
    assert [e.value for e in lic] == [f"{UL}.licence: {text}"]
    assert lic[0].fact == f"Лицензия: {text} (реестр МСП, выгрузка ФНС на {msp_dump.AS_OF})"
    assert lic[0].url == msp_dump.page_url(UL) and lic[0].source == msp_dump.SOURCE
    assert strip_unverified(cps[UL], cache.page_text).evidence == cps[UL].evidence  # it reaches the card
    assert not [e for e in cps[IP].evidence if e.field == "licence"]  # no licence in the record — no fact


def test_forged_licence_is_dropped_from_the_card(cached):
    cache, cps = cached
    fake = Evidence(source=msp_dump.SOURCE, url=msp_dump.page_url(UL), fetched_at=AT, field="licence",
                    value=f"{UL}.licence: Деятельность по сбору и транспортированию отходов I-IV классов",
                    fact="Лицензия: Деятельность по сбору и транспортированию отходов I-IV классов")
    cp = cps[UL].model_copy(update={"evidence": [*cps[UL].evidence, fake]})
    assert any("licence" in err for err in check_counterparty(cp, cache.page_text))
    assert fake not in strip_unverified(cp, cache.page_text).evidence
