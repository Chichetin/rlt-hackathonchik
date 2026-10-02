"""ФНС open data: build → parquet, lookup → Evidence that passes the evidence check.

Fixtures are real documents cut from the 25.09.2026 archives (legal entities only):
7820324037 ООО «Офисмаг СПБ», 7804428656 ООО «БРАСС», debtam — first two documents of a file.
7707049388 (Ростелеком) is in none of the four datasets.
"""

import logging
import os
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from src.api import app as app_mod
from src.contracts import Counterparty, CounterpartyMatch, Evidence, ScoreComponent
from src.enrich.enrich import enrich
from src.enrich.evidence import check_counterparty, check_evidence
from src.match import search as search_mod
from src.sources import fns_opendata as F
from src.sources import pb_nalog
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "fns_opendata"
PB = FIX.parent / "pb_nalog"
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
    assert by["employees"][0].fact == (
        "Среднесписочная численность работников за 2025 г. (сведения на 31.12.2025): 8 чел.")
    assert by["employees"][0].value == "СведССЧР.КолРаб: 8"
    assert by["income"][0].fact == "Доходы за 2025 г. по бухгалтерской отчётности: 17 367 000 ₽"
    assert by["expenses"][0].value == "СведДохРасх.СумРасход: 12953000.00"
    assert [e.fact for e in by["tax_regime"]] == ["Применяет УСН (сведения на 01.09.2026)"]
    for e in evs:
        assert check_evidence(e, cache.page_text) == []
        assert e.url.startswith("https://www.nalog.gov.ru/opendata/7707329152-") and e.url.endswith(f"#inn={BRASS}")
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


def test_enrich_adds_fns_facts_that_pass_the_check(built, monkeypatch):
    out, _, cache = built
    monkeypatch.setenv("FNS_OPENDATA_PARQUET", str(out))
    cache.put(pb_nalog.card_url(BRASS), (PB / f"{BRASS}.txt").read_text(encoding="utf-8"))
    cp = enrich(BRASS, cache, offline=True)
    fields = {e.field for e in cp.evidence}
    assert {"inn", "name", "status", "employees", "income", "expenses", "tax_regime"} <= fields
    assert "tax_debt" not in fields
    assert check_counterparty(cp, cache.page_text) == []


def test_enrich_without_fns_parquet_is_unchanged(built):
    _, _, cache = built  # conftest points FNS_OPENDATA_PARQUET at a missing file
    cache.put(pb_nalog.card_url(BRASS), (PB / f"{BRASS}.txt").read_text(encoding="utf-8"))
    cp = enrich(BRASS, cache, offline=True)
    assert all(e.url == pb_nalog.card_url(BRASS) for e in cp.evidence)


def test_individual_entrepreneur_inn_gives_nothing(built):
    out, _, cache = built
    assert F.lookup("500100732259", cache, parquet=out) == []  # 12 digits: the datasets list legal entities only


def test_unreadable_parquet_gives_no_facts_and_the_card_survives(built, tmp_path, monkeypatch, caplog):
    _, _, cache = built
    broken = tmp_path / "broken.parquet"
    broken.write_bytes(b"PAR1 not a parquet file")
    with caplog.at_level(logging.WARNING):
        assert F.lookup(BRASS, cache, parquet=broken) == []
    assert "факты ФНС пропущены" in caplog.text
    monkeypatch.setenv("FNS_OPENDATA_PARQUET", str(broken))
    cache.put(pb_nalog.card_url(BRASS), (PB / f"{BRASS}.txt").read_text(encoding="utf-8"))
    cp = enrich(BRASS, cache, offline=True)
    assert cp.name == 'ООО "БРАСС"' and all(e.url == pb_nalog.card_url(BRASS) for e in cp.evidence)


def test_missing_parquet_warns_once(built, tmp_path, monkeypatch, caplog):
    _, _, cache = built
    monkeypatch.setattr(F, "_missing_warned", set())
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert F.lookup(BRASS, cache, parquet=tmp_path / "missing.parquet") == []
    assert caplog.text.count("не собраны") == 1


def test_document_without_state_date_is_skipped_in_build(tmp_path):
    root = tmp_path / "external"
    root.mkdir()
    for ds in F.DATASETS.values():
        xml = (FIX / f"{ds.id}.xml").read_text(encoding="utf-8")
        if ds.id == "sshr2019":  # the first document loses ДатаСост
            xml = xml.replace(' ДатаСост="31.12.2025"', "", 1)
        with zipfile.ZipFile(F.archive_path(ds, root), "w") as z:
            z.writestr(f"VO_{ds.id}.xml", xml)
    counts = F.build(root, tmp_path / "out.parquet")
    assert counts["sshr2019"] == 1 and counts["revexp"] == 2


def test_evidence_url_is_the_dataset_passport_not_the_archive(built):
    out, _, cache = built
    for e in F.lookup(BRASS, cache, parquet=out):
        assert e.url.startswith("https://www.nalog.gov.ru/opendata/7707329152-") and ".zip" not in e.url


def test_default_parquet_follows_data_processed_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FNS_OPENDATA_PARQUET")
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(tmp_path / "processed"))
    assert F._parquet() == tmp_path / "processed" / "fns_opendata.parquet"


def test_amount_keeps_its_sign():
    assert F._rub(Decimal("-0.50")) == "-0,50"
    assert F._rub(Decimal("-1234567.5")) == "-1 234 567,50"
    assert F._rub(Decimal("0.50")) == "0,50" and F._rub(Decimal("1000")) == "1 000"


@pytest.mark.parametrize(("n", "noun"), [(1, "позиция"), (2, "позиции"), (5, "позиций"), (11, "позиций"),
                                         (12, "позиций"), (21, "позиция"), (22, "позиции")])
def test_debt_items_noun(n, noun):
    pairs = [("Документ.ДатаСост", "01.09.2026")]
    for i in range(n):
        pairs += [("СведНедоим.НаимНалог", f"Налог {i}"), ("СведНедоим.ОбщСумНедоим", "10.00")]
    (debt,) = F.evidence(F.DATASETS["debtam"], pairs, "https://x#inn=1", datetime.now(UTC))
    assert f"({n} {noun};" in debt.fact


def _match(inn: str) -> CounterpartyMatch:
    ev = Evidence(source="data:participants", url="data:participants#1", fetched_at=datetime.now(UTC),
                  fact="ИНН", field="inn", value=inn)
    cp = Counterparty(inn=inn, name=f"ИНН {inn}", kpp="780601001", stats={"lots_won": 3}, evidence=[ev])
    return CounterpartyMatch(counterparty=cp, score=0.5, origin="history", components=[
        ScoreComponent(name="product_fit", value=0.5, weight=0.35, explanation="x")])


def test_search_looks_up_open_data_for_the_shown_page_only(built, tmp_path, monkeypatch):
    out, _, _ = built
    monkeypatch.setenv("FNS_OPENDATA_PARQUET", str(out))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/q.sqlite")
    monkeypatch.setattr(app_mod, "_engine", None)
    monkeypatch.setattr(search_mod, "search", lambda q, k=20: [_match(BRASS), _match(OFFICEMAG)])
    SourceCache().put(pb_nalog.card_url(BRASS), (PB / f"{BRASS}.txt").read_text(encoding="utf-8"))
    looked_up: list[str] = []
    real = F.lookup_many

    def spy(inns, *a, **k):
        inns = list(inns)
        looked_up.extend(inns)
        return real(inns, *a, **k)
    monkeypatch.setattr(F, "lookup_many", spy)
    client = TestClient(app_mod.app)

    r = client.post("/api/search", params={"limit": 1}, json={"query_id": "q", "text": "x"})
    (card,) = r.json()["history"]
    assert looked_up == [BRASS]  # one shown card, not both candidates of the loop
    assert {"employees", "income", "expenses", "tax_regime"} <= {e["field"] for e in card["counterparty"]["evidence"]}
    assert all(e["url"].startswith("http") or e["url"].startswith("data:") for e in card["counterparty"]["evidence"])

    looked_up.clear()
    client.post("/api/search", params={"limit": 20}, json={"query_id": "q", "text": "x"})
    assert sorted(looked_up) == sorted([BRASS, OFFICEMAG])


def test_lookup_many_is_one_query_with_facts_per_inn(built):
    out, _, cache = built
    got = F.lookup_many([BRASS, OFFICEMAG, ROSTELECOM, BRASS, "500100732259"], cache, parquet=out)
    assert set(got) == {BRASS, OFFICEMAG}  # no document / an entrepreneur → absent, a repeated INN → once
    assert got[BRASS] == F.lookup(BRASS, cache, parquet=out)
    assert all(check_evidence(e, cache.page_text) == [] for evs in got.values() for e in evs)
    assert F.lookup_many([], cache, parquet=out) == {}
    with pytest.raises(ValueError):
        F.lookup_many([BRASS, "7804428657"], cache, parquet=out)


def test_lookup_many_is_safe_across_threads(built):
    from concurrent.futures import ThreadPoolExecutor
    out, _, cache = built
    want = F.lookup_many([BRASS, OFFICEMAG], cache, parquet=out)
    with ThreadPoolExecutor(8) as pool:
        got = list(pool.map(lambda _: F.lookup_many([BRASS, OFFICEMAG], cache, parquet=out), range(64)))
    assert all(g == want for g in got)
