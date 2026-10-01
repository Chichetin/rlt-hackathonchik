"""ФНС open data: build → parquet, lookup → Evidence that passes the evidence check.

Fixtures are real documents cut from the 25.09.2026 archives (legal entities only):
7820324037 ООО «Офисмаг СПБ», 7804428656 ООО «БРАСС», debtam — first two documents of a file.
7707049388 (Ростелеком) is in none of the four datasets.
"""

import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from src.contracts import Evidence
from src.enrich.evidence import check_evidence
from src.sources import fns_opendata as F
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "fns_opendata"
OFFICEMAG, BRASS, DEBTOR, ROSTELECOM = "7820324037", "7804428656", "4706050392", "7707049388"
FETCHED = 1_790_000_000.0  # archive mtime → Evidence.fetched_at


@pytest.fixture
def built(tmp_path):
    root = tmp_path / "external"
    root.mkdir()
    for ds in F.DATASETS.values():
        path = F.archive_path(ds, root)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(FIX / f"{ds.id}.xml", f"VO_{ds.id}.xml")
        os.utime(path, (FETCHED, FETCHED))
    out = tmp_path / "fns_opendata.parquet"
    counts = F.build(root, out)
    return out, counts, SourceCache(tmp_path / "sources.sqlite")


def _by_field(evs: list[Evidence]) -> dict[str, list[Evidence]]:
    out: dict[str, list[Evidence]] = {}
    for e in evs:
        out.setdefault(e.field or "", []).append(e)
    return out


def test_build_writes_one_row_per_document(built):
    out, counts, _ = built
    assert counts == {"sshr2019": 2, "revexp": 2, "snr": 1, "debtam": 2}
    rows = duckdb.sql(f"SELECT inn, dataset, as_of, employees, income, expenses, regimes, tax_debt "
                      f"FROM read_parquet('{out}') ORDER BY inn, dataset").fetchall()
    by = {(r[0], r[1]): r for r in rows}
    assert by[(OFFICEMAG, "sshr2019")][3] == 338 and str(by[(OFFICEMAG, "sshr2019")][2]) == "2025-12-31"
    assert by[(OFFICEMAG, "revexp")][4:6] == (3915410000.0, 3819808000.0)
    assert by[(BRASS, "snr")][6] == "УСН" and str(by[(BRASS, "snr")][2]) == "2026-09-01"
    assert by[(DEBTOR, "debtam")][7] == pytest.approx(1074.44)
    assert by[("9723161623", "debtam")][7] == pytest.approx(6634.36)
    assert (ROSTELECOM, "sshr2019") not in by


def test_build_is_idempotent(built):
    out, _, _ = built
    before = out.stat().st_mtime_ns
    assert F.build(out.parent / "external", out) == {}
    assert out.stat().st_mtime_ns == before


def test_lookup_facts_pass_evidence_check(built):
    out, _, cache = built
    evs = F.lookup(BRASS, cache, parquet=out)
    by = _by_field(evs)
    assert set(by) == {"employees", "income", "expenses", "tax_regime"}  # no debtam row → no debt fact
    assert by["employees"][0].fact == "Среднесписочная численность работников за 2025 г. (сведения на 31.12.2025): 8 чел."
    assert by["employees"][0].value == "СведССЧР.КолРаб: 8"
    assert by["income"][0].fact == "Доходы за 2025 г. по бухгалтерской отчётности: 17 367 000 ₽"
    assert by["expenses"][0].value == "СведДохРасх.СумРасход: 12953000.00"
    assert [e.fact for e in by["tax_regime"]] == ["Применяет УСН (сведения на 01.09.2026)"]
    for e in evs:
        assert check_evidence(e, cache.page_text) == []
        assert e.url.startswith("https://file.nalog.ru/opendata/") and e.url.endswith(f"#inn={BRASS}")
        assert e.fetched_at == datetime.fromtimestamp(FETCHED, tz=UTC)
        assert e.source.startswith("ФНС, открытые данные: ")
    page = cache.page_text(F.page_url(F.DATASETS["snr"], BRASS)) or ""
    assert 'СведНП.НаимОрг: ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "БРАСС"' in page


def test_big_money_is_not_mistaken_for_phone_or_inn(built):
    out, _, cache = built
    evs = F.lookup(OFFICEMAG, cache, parquet=out)
    income = _by_field(evs)["income"][0]
    assert income.fact == "Доходы за 2025 г. по бухгалтерской отчётности: 3 915 410 000 ₽"
    assert all(check_evidence(e, cache.page_text) == [] for e in evs)
    assert "tax_regime" not in _by_field(evs)


def test_tax_debt_total_and_largest_item(built):
    out, _, cache = built
    (debt,) = F.lookup(DEBTOR, cache, parquet=out)
    assert debt.field == "tax_debt" and debt.value == "СведНедоим.ОбщСумНедоим: 1067.60"
    assert debt.fact == ("Недоимка, пени и штрафы на 01.09.2026: 1 074,44 ₽ (2 позиции; наибольшая — "
                         "Налог на добавленную стоимость, 1 067,60 ₽)")
    assert check_evidence(debt, cache.page_text) == []


def test_snr_flags_zero_give_no_regime(tmp_path):
    pairs = [("Документ.ДатаСост", "01.09.2026"), ("СведСНР.ПризнЕСХН", "1"), ("СведСНР.ПризнУСН", "0"),
             ("СведСНР.ПризнАУСН", "1"), ("СведСНР.ПризнСРП", "0")]
    evs = F.evidence(F.DATASETS["snr"], pairs, "https://x#inn=1", datetime.now(UTC))
    assert [e.value for e in evs] == ["СведСНР.ПризнЕСХН: 1", "СведСНР.ПризнАУСН: 1"]
    assert F.values("snr", pairs)["regimes"] == "ЕСХН,АУСН"


def test_inn_absent_everywhere_gives_nothing(built):
    out, _, cache = built
    assert F.lookup(ROSTELECOM, cache, parquet=out) == []
    assert all(cache.page_text(F.page_url(ds, ROSTELECOM)) is None for ds in F.DATASETS.values())


def test_tampered_value_is_caught(built):
    out, _, cache = built
    emp = _by_field(F.lookup(BRASS, cache, parquet=out))["employees"][0]
    for fake in ("СведССЧР.КолРаб: 80", "СведССЧР.КолРаб: 9"):
        bad = emp.model_copy(update={"value": fake})
        assert check_evidence(bad, cache.page_text)


def test_invalid_inn_is_valueerror(built):
    out, _, cache = built
    with pytest.raises(ValueError):
        F.lookup("7804428657", cache, parquet=out)


def test_no_parquet_gives_empty(tmp_path):
    assert F.lookup(BRASS, SourceCache(tmp_path / "s.sqlite"), parquet=tmp_path / "missing.parquet") == []
