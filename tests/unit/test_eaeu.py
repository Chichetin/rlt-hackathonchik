"""EAEU companies in the block «Новые» (#200): goods only, registry answer from the cache, every fact checked."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.enrich import eaeu
from src.enrich.evidence import check_counterparty
from src.sources import by_grp
from src.sources.cache import SourceCache

UNP = "100063724"
WEIGHTS = {"product_fit": 0.5, "experience": 0.2, "activity": 0.1, "geography": 0.2}


REAL_LEADS = eaeu.LEADS  # tests/unit/conftest.py points the module at an empty file


@pytest.fixture(autouse=True)
def _real_leads(monkeypatch):
    monkeypatch.setattr(eaeu, "LEADS", REAL_LEADS)


@pytest.fixture
def cache(tmp_path) -> SourceCache:
    return SourceCache(tmp_path / "c.sqlite")


def test_lead_file_holds_only_checked_identifiers():
    leads = eaeu.load()
    assert leads and all(lead.country in eaeu.COUNTRY_NAME for lead in leads)
    assert all(by_grp.unp_is_valid(lead.national_id) for lead in leads if lead.country == "BY")
    # every lead has the registry's recorded answer next to it: the demo shows it without the network
    recorded = {"BY": eaeu.RECORDED, "AM": eaeu.RECORDED_AM}
    assert all((recorded[lead.country] / f"{lead.national_id}.json").is_file() for lead in leads)


def test_goods_query_gets_the_registry_company_after_the_block(cache):
    rows, line = eaeu.matches(["17.12.14.110"], ["17.12"], cache, WEIGHTS, False)
    assert line is not None and line.endswith("в блок 1")
    (m,) = rows
    cp = m.counterparty
    assert (m.origin, cp.inn, cp.country, cp.foreign_id) == ("discovered", None, "BY", f"BY:{UNP}")
    assert cp.kind is None  # no registry names the role: not guessed
    assert check_counterparty(cp, cache.page_text) == []
    assert m.score == 0.0 and [c.name for c in m.components] == list(eaeu.COMPONENT_ORDER)
    fit = next(c for c in m.components if c.name == "product_fit")
    assert fit.value == 0.0 and "не подтверждено" in fit.explanation and "https://oboi.by/contact/" in fit.explanation
    assert "Беларусь" in next(c for c in m.components if c.name == "geography").explanation


@pytest.mark.parametrize(("codes", "classes"), [
    (["81.21.10"], ["81.21"]),  # a service: geography matters, the EAEU row is not offered
    (["17.12.14", "81.21.10"], ["17.12", "81.21"]),  # a mixed lot is not «goods only»
    (["26.20.11"], ["26.20"]),  # goods, but no lead of this class
])
def test_no_row_for_services_or_other_classes(cache, codes, classes):
    assert eaeu.matches(codes, classes, cache, WEIGHTS, False)[0] == []


def test_lead_without_a_cached_registry_answer_is_not_shown(cache, tmp_path: Path):
    lead = eaeu.Lead("17.12", "BY", UNP, "как показал поиск", "https://example.by/")
    rows, line = eaeu.matches(["17.12"], ["17.12"], cache, WEIGHTS, False, leads=[lead], recorded=False)
    assert rows == [] and line is not None and "без ответа реестра в кеше 1" in line
    kz = eaeu.Lead("17.12", "KZ", "123456789012", "x", "https://example.kz/")  # no registry connected: never shown
    assert eaeu.matches(["17.12"], ["17.12"], cache, WEIGHTS, False, leads=[kz])[0] == []


def test_switch(monkeypatch):
    monkeypatch.setenv("DISCOVERY_EAEU", "0")
    assert not eaeu.enabled()
    monkeypatch.delenv("DISCOVERY_EAEU")
    assert eaeu.enabled()


def test_discover_appends_the_eaeu_row_after_its_own_block(fixture_processed, cache, monkeypatch):
    """Through discover(): the row stands after the block's rows and survives the filters; DISCOVERY_EAEU=0 — no row."""
    from src.contracts import Query
    from src.enrich import discover as discover_mod

    q = Query(query_id="q-eaeu", text="Бумага офисная А4", okpd2=["17.12.14.110"])
    res = discover_mod.discover(q, cache=cache, weights=WEIGHTS, processed=fixture_processed, offline=True)
    if not res.history_checked:
        pytest.skip("фикстура без данных организаторов")
    assert res.eaeu and res.matches[-1].counterparty.foreign_id == f"BY:{UNP}"
    assert all(m.counterparty.country is None for m in res.matches[:-1])
    monkeypatch.setenv("DISCOVERY_EAEU", "0")
    off = discover_mod.discover(q, cache=cache, weights=WEIGHTS, processed=fixture_processed, offline=True)
    assert all(m.counterparty.foreign_id is None for m in off.matches) and off.eaeu == []
