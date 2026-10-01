"""MSP registry dump (#185): parse records, resumable build, raw record for Evidence."""

import zipfile
from pathlib import Path

import pyarrow.parquet as pq

from src.sources import msp_dump

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
