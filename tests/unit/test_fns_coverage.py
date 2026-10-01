"""scripts/fns_coverage.py (#219): parsing of the FNS open-data archives and the coverage table."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fns_coverage", ROOT / "scripts" / "fns_coverage.py")
assert spec and spec.loader
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)

HEAD = '<?xml version="1.0" encoding="UTF-8"?><Файл КолДок="2">'


def doc(inn: str, tail: str) -> str:
    org = f'<СведНП НаимОрг="ООО &quot;А&quot;" ИННЮЛ="{inn}"/>'
    return f'<Документ ИдДок="x" ДатаСост="31.12.2025">{org}{tail}</Документ>'


def make(tmp_path: Path) -> Path:
    d = tmp_path / "fns"
    d.mkdir()
    with zipfile.ZipFile(d / "sshr2019.zip", "w") as z:
        z.writestr("a.xml", HEAD + doc("7801000012", '<СведССЧР КолРаб="300"/>')
                   + doc("7801000020", '<СведССЧР КолРаб="5"/>') + "</Файл>")
        # a repeated INN keeps the largest value
        z.writestr("b.xml", HEAD + doc("7801000012", '<СведССЧР КолРаб="10"/>') + "</Файл>")
    with zipfile.ZipFile(d / "revexp.zip", "w") as z:
        z.writestr("a.xml", HEAD + doc("7801000020", '<СведДохРасх СумДоход="1000.00" СумРасход="1.00"/>')
                   + doc("7802000039", '<СведДохРасх СумДоход="3000000000.00" СумРасход="1.00"/>') + "</Файл>")
    return d


def test_archives_are_parsed_once_and_cached(tmp_path):
    d = make(tmp_path)
    employees, missed = F.read_dataset(d / "sshr2019.zip", "sshr2019")
    assert employees == {"7801000012": 300.0, "7801000020": 5.0} and missed == 0
    path, meta = F.parsed(d)
    assert meta["sshr2019_inns"] == 2 and meta["revexp_inns"] == 2 and meta["any_inns"] == 3
    assert meta["documents_not_parsed"] == {"sshr2019": 0, "revexp": 0}
    (d / "sshr2019.zip").unlink()  # the second call reads the parquet, not the archives
    assert F.parsed(d) == (path, meta)


def test_coverage_splits_by_kind_registry_and_thresholds(tmp_path):
    d = make(tmp_path)
    fns, _ = F.parsed(d)
    processed = tmp_path / "processed"
    processed.mkdir()
    rows = [  # (supplier, lot, winner, valid)
        ("7801000012", 1, True, True),    # legal, 300 employees: above the SME ceiling, not in the registry
        ("7801000020", 1, False, True),   # legal, small, in the registry
        ("7801000020", 2, True, True),
        ("7803000048", 2, False, True),   # legal, in no dataset
        ("780100000090", 3, True, True),  # sole trader: never in the datasets
        ("7801000099", 3, True, False),   # invalid INN: not counted
    ]
    pq.write_table(pa.table({"supplier_inn": [r[0] for r in rows], "lot_id": [r[1] for r in rows],
                             "is_winner": [r[2] for r in rows], "inn_valid": [r[3] for r in rows]}),
                   processed / "participants.parquet")
    msp = tmp_path / "msp.parquet"
    pq.write_table(pa.table({"inn": ["7801000020"]}), msp)
    rec = F.coverage(processed, fns, msp, "inn")
    assert rec["total"] == {"suppliers": 4, "participations": 5, "wins": 3}
    g = rec["groups"]
    assert g["legal:in_sme_registry"] == {"suppliers": 1, "participations": 2, "wins": 1, "suppliers_in_fns": 1,
                                          "participations_in_fns": 2, "wins_in_fns": 1, "suppliers_large": 0,
                                          "participations_large": 0, "wins_large": 0}
    out = g["legal:not_in_sme_registry"]
    assert (out["suppliers"], out["wins"], out["suppliers_in_fns"], out["wins_in_fns"], out["suppliers_large"],
            out["wins_large"]) == (2, 1, 1, 1, 1, 1)
    assert g["sole_trader:not_in_sme_registry"]["suppliers_in_fns"] == 0
    # 7802000039: income above 2 bn, INN 78, not among the suppliers → a «large and new» candidate
    assert rec["candidates"] == {"above_sme_thresholds": 2, "of_them_inn_78_47": 2,
                                 "of_them_not_among_organizers_suppliers": 1}
    assert set(F.coverage(processed, fns)["groups"]) == {"legal:registry_unknown", "sole_trader:registry_unknown"}
