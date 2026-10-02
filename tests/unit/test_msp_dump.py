"""MSP registry dump (#185): parse records, resumable build, raw record for Evidence."""

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from src.enrich.evidence import check_counterparty, strip_unverified
from src.sources import msp_dump
from src.sources.cache import SourceCache

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "msp_dump" / "sample.xml"


def _rows() -> dict[str, dict]:
    return {r["doc_id"]: r for r in msp_dump.parse_file(FIXTURE.read_bytes(), "sample.xml")}


def test_parse_legal_entity_all_fields():
    r = _rows()["doc-ul-1"]
    assert r["inn"] == "0078000013" and r["inn_valid"] and r["ogrn"] == "1007800000013"
    assert r["entity"] == "ЮЛ"
    assert r["name"] == 'ООО "ТЕСТОВАЯ БУМАГА"'
    assert r["name_full"] == 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ТЕСТОВАЯ БУМАГА"'
    assert r["region_code"] == "78" and r["region"] == "г. Санкт-Петербург"
    assert r["okved_main"] == "17.12" and r["okved_main_name"] == "Производство бумаги и картона"
    assert r["okved_extra"] == ["46.49.33", "46.76.1"]
    assert r["okved_report"] == ["17.12.1"]
    assert r["products"] == ["17.12.14"] and r["product_names"] == ["Бумага прочая для печати и письма"]
    assert r["licences"] == ["Заготовка, хранение, переработка и реализация лома черных металлов"]
    assert r["msp_category"] == 2 and r["employees"] == 57
    assert r["included"] == "2021-07-10" and r["state_date"] == "2026-09-10"
    assert r["src_file"] == "sample.xml"


def test_parse_individual_entrepreneur():
    r = _rows()["doc-ip-1"]
    assert r["entity"] == "ИП" and r["inn"] == "007800000309" and r["inn_valid"]
    assert r["name"] == "ИП ТЕСТОВ ТЕСТ ТЕСТОВИЧ" and r["name_full"] == "ТЕСТОВ ТЕСТ ТЕСТОВИЧ"
    assert r["region"] == "обл. Ленинградская" and r["city"] == "г. Мурино"
    assert r["okved_extra"] == [] and r["okved_report"] == [] and r["licences"] == []
    assert r["employees"] is None and r["new_msp"] is True


def test_bad_checksum_kept_but_marked_and_empty_okved():
    r = _rows()["doc-bad-inn"]
    assert r["inn"] == "0078000014" and not r["inn_valid"]
    assert r["okved_main"] is None and r["okved_extra"] == []


def _zip(tmp_path: Path, members: dict[str, bytes]) -> Path:
    path = tmp_path / "dump.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return path


def test_build_writes_one_part_per_member_and_resumes(tmp_path):
    xml = FIXTURE.read_bytes()
    path = _zip(tmp_path, {"a.xml": xml, "b.xml": xml, "readme.txt": b"x"})
    out = tmp_path / "parts"
    first = msp_dump.build(path, out, workers=1)
    assert first == {"members": 2, "built": 2, "skipped": 0, "rows": 6}
    assert sorted(p.name for p in out.glob("*.parquet")) == ["a.parquet", "b.parquet"]
    assert pq.read_table(out / "a.parquet").num_rows == 3
    (out / "b.parquet").unlink()
    again = msp_dump.build(path, out, workers=1)
    assert again == {"members": 2, "built": 1, "skipped": 1, "rows": 3}


def test_record_returns_the_raw_document_for_evidence(tmp_path):
    path = _zip(tmp_path, {"a.xml": FIXTURE.read_bytes()})
    raw = msp_dump.record(path, "a.xml", "doc-ip-1")
    assert raw is not None and raw.startswith('<Документ ИдДок="doc-ip-1"') and raw.endswith("</Документ>")
    assert "007800000309" in raw and "doc-ul-1" not in raw
    assert msp_dump.record(path, "a.xml", "no-such-doc") is None


AT = datetime(2026, 9, 12, tzinfo=UTC)


def _cache(tmp_path: Path) -> SourceCache:
    return SourceCache(tmp_path / "sources.sqlite")


def test_page_is_the_same_from_the_parquet_row_and_from_the_raw_xml(tmp_path):
    # the pool renders pages from parquet rows; that is the source only if it equals the raw record's page
    path = _zip(tmp_path, {"a.xml": FIXTURE.read_bytes()})
    out = tmp_path / "parts"
    msp_dump.build(path, out, workers=1)
    for row in pq.read_table(out / "a.parquet").to_pylist():
        raw = msp_dump.record(path, "a.xml", row["doc_id"])
        assert raw is not None
        assert msp_dump.page(row) == msp_dump.page_from_xml(raw, "a.xml")


def test_page_lines_are_verbatim_values_and_ip_has_no_locality():
    rows = _rows()
    ul = msp_dump.page(rows["doc-ul-1"]).splitlines()
    assert "row.0078000013.okved1: 17.12" in ul
    assert 'row.0078000013.name_short: ООО "ТЕСТОВАЯ БУМАГА"' in ul
    assert "row.0078000013.okved_extra: 46.49.33" in ul and "row.0078000013.okved_extra: 46.76.1" in ul
    assert "row.0078000013.licence: Заготовка, хранение, переработка и реализация лома черных металлов" in ul
    assert "row.0078000013.product: 17.12.14 Бумага прочая для печати и письма" in ul
    ip = msp_dump.page(rows["doc-ip-1"])
    assert "Мурино" not in ip and "row.007800000309.region: обл. Ленинградская" in ip.splitlines()


def test_counterparty_every_fact_passes_the_evidence_check(tmp_path):
    cache = _cache(tmp_path)
    row = _rows()["doc-ul-1"]
    cp = msp_dump.counterparty(row, AT)
    assert cp is not None
    cache.put(msp_dump.page_url(cp.inn or ""), msp_dump.page(row), fetched_at=AT.timestamp())
    assert check_counterparty(cp, cache.page_text) == []
    assert strip_unverified(cp, cache.page_text) == cp  # nothing is dropped: no fact without a source
    assert cp.name == 'ООО "ТЕСТОВАЯ БУМАГА"' and cp.kind == "manufacturer" and cp.is_msp is True
    assert cp.region == "Санкт-Петербург" and cp.okved == ["17.12", "46.49.33", "46.76.1"]
    fields = {e.field for e in cp.evidence}
    assert fields == {"inn", "ogrn", "name", "okved", "licence", "msp", "region"}  # licence: #188
    extra = [e for e in cp.evidence if e.value == "0078000013.okved_extra: 46.49.33"]
    assert len(extra) == 1 and "Дополнительный ОКВЭД 46.49.33" in extra[0].fact
    assert all(e.url == "https://www.nalog.gov.ru/opendata/7707329152-rsmp/#inn=0078000013" for e in cp.evidence)
    assert all(e.fetched_at == AT and e.source == msp_dump.SOURCE for e in cp.evidence)


def test_counterparty_of_an_individual_entrepreneur(tmp_path):
    cache = _cache(tmp_path)
    row = _rows()["doc-ip-1"]
    cp = msp_dump.counterparty(row, AT)
    assert cp is not None
    cache.put(msp_dump.page_url(cp.inn or ""), msp_dump.page(row))
    assert check_counterparty(cp, cache.page_text) == []
    assert cp.name == "ИП ТЕСТОВ ТЕСТ ТЕСТОВИЧ" and cp.region == "Ленинградская область"
    assert cp.kind == "unknown" and cp.contacts == {}  # 43.39 — construction: the rule gives no role


def test_counterparty_region_outside_spb_and_lo_is_the_registry_text():
    row = dict(_rows()["doc-ul-1"], region_code="16", region="Респ. Татарстан")
    cp = msp_dump.counterparty(row, AT)
    assert cp is not None and cp.region == "Респ. Татарстан"


def test_counterparty_with_a_bad_inn_is_not_built():
    assert msp_dump.counterparty(_rows()["doc-bad-inn"], AT) is None


def test_forged_dump_fact_fails_the_check(tmp_path):
    cache = _cache(tmp_path)
    row = _rows()["doc-ul-1"]
    cache.put(msp_dump.page_url("0078000013"), msp_dump.page(row))
    cp = msp_dump.counterparty(dict(row, okved_main="46.49"), AT)  # the page says 17.12
    assert cp is not None and cp.kind == "distributor"
    assert any("роль" in e for e in check_counterparty(cp, cache.page_text))
    assert strip_unverified(cp, cache.page_text).kind is None
