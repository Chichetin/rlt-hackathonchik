"""Supplier map aggregate (#178): region of a supplier from INN/KPP, the same rules as the UI
(src/ui/src/geo/regions.ts), and the per-region counts from participants.parquet."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_supplier_regions.py"
GEO = ROOT / "src" / "ui" / "src" / "geo"


@pytest.fixture(scope="module")
def agg() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_supplier_regions_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CASES = json.loads((GEO / "region-code-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["why"] for c in CASES])
def test_region_code_matches_ui_cases(agg: ModuleType, case: dict) -> None:
    assert agg.region_code_of(case["inn"], case["kpp"]) == case["code"]


def test_codes_and_aliases_match_regions_ts(agg: ModuleType) -> None:
    ts = (GEO / "regions.ts").read_text(encoding="utf-8")
    table = ts.split("export const REGIONS", 1)[1].split("};", 1)[0]
    aliases = ts.split("export const CODE_ALIASES", 1)[1].split("};", 1)[0]
    assert set(re.findall(r'^\s*"(\d\d)": \{', table, re.M)) == set(agg.CODES)
    assert dict(re.findall(r'^\s*"(\d\d)": "(\d\d)"', aliases, re.M)) == agg.CODE_ALIASES


def _participants(tmp_path: Path, rows: list[tuple[str, str | None, bool]]) -> Path:
    path = tmp_path / "participants.parquet"
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (lot_id BIGINT, supplier_inn VARCHAR, supplier_kpp VARCHAR, "
        "is_winner BOOLEAN, publish_date DATE)"
    )
    con.executemany(
        "INSERT INTO t VALUES (?, ?, ?, ?, DATE '2024-03-01' + CAST(? AS INTEGER))",
        [(i, inn, kpp, won, i) for i, (inn, kpp, won) in enumerate(rows)],
    )
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT parquet)")
    return path


ROWS: list[tuple[str, str | None, bool]] = [
    # organisation from Moscow with an SPb branch: 2 lots via the branch, 1 via the head office → SPb
    ("7700123459", "784201001", True), ("7700123459", "784201001", False), ("7700123459", "770001001", False),
    # largest taxpayer: KPP 99… says nothing about the place → INN (SPb)
    ("7800123452", "997650001", False),
    # individual entrepreneur: KPP changes from lot to lot → INN (Samara), a winner once
    ("630012345661", "780601001", False), ("630012345661", "781101001", True), ("630012345661", None, False),
    # new Moscow series, Crimea
    ("9701123455", None, True), ("9100123450", "910201001", False),
    # foreign: INN 9909…, 9-character identifiers (synthetic, not in the data): a valid UNP of an
    # organisation and of an IP, a check digit that fits with a non-region first digit, a wrong check
    # digit, letters outside the UNP set
    ("9909123454", "773851001", False), ("123456783", None, True), ("AB1234563", None, False),
    ("812345679", None, False), ("123456789", None, False), ("XY1234567", None, False),
    # unknown: bad checksum, 15 digits, zeros
    ("7700123450", "784201001", False), ("300000000000000", None, False), ("0000123450", None, False),
]


def test_aggregate(agg: ModuleType, tmp_path: Path) -> None:
    out = agg.aggregate(_participants(tmp_path, ROWS))
    assert out["total_suppliers"] == 14
    assert out["by_region"] == {
        "63": {"suppliers": 1, "winners": 1},
        "77": {"suppliers": 1, "winners": 1},
        "78": {"suppliers": 2, "winners": 1},
        "91": {"suppliers": 1, "winners": 0},
    }
    foreign = out["foreign"]
    note = foreign.pop("note")
    assert foreign == {"count": 6, "inn_9909": 1, "nine_char": 5, "nine_digit": 3, "unp_check_ok": 2, "unp_valid": 2}
    assert "ИНН 9909… (иностранная организация на учёте в РФ): 1." in note
    assert "сходится у 2 из 3 цифровых" in note and "проходят 2 из 5" in note and "не проверено" in note
    assert out["unknown"] == 3
    assert out["multi_region_suppliers"] == 1
    assert out["period"] == {"from": "2024-03-01", "to": "2024-03-18", "rows": len(ROWS)}
    assert "только для отображения" in out["method"].lower() and "holdout" in out["method"]
    regions = sum(v["suppliers"] for v in out["by_region"].values())
    assert regions + out["foreign"]["count"] + out["unknown"] == out["total_suppliers"]
    assert list(out["by_region"]) == sorted(out["by_region"])


def test_tie_prefers_the_inn_region(agg: ModuleType, tmp_path: Path) -> None:
    # one lot via a Leningrad oblast KPP, one via an SPb KPP: the INN (SPb) decides, not the smaller code
    rows: list[tuple[str, str | None, bool]] = [("7800123452", "470001001", False), ("7800123452", "784201001", False)]
    assert agg.aggregate(_participants(tmp_path, rows))["by_region"] == {"78": {"suppliers": 1, "winners": 0}}


# Synthetic values; the verdicts are cross-checked with python-stdnum 1.20 (stdnum.by.unp).
UNP_CASES = [
    ("123456783", True, True),  # organisation
    ("AB1234563", True, True),  # individual entrepreneur: 2 letters + 7 digits
    ("ME1234561", True, True),  # E is 3 in the UNP letter set (14 in the plain alphabet)
    ("812345679", True, False),  # check digit fits, 8 is not a region
    ("123456789", False, False),  # wrong check digit
    ("XY1234567", False, False),  # letters outside the UNP set
    ("12345678", False, False),  # 8 characters
    ("1234567\uff183", False, False),  # full-width digit
]


@pytest.mark.parametrize(("value", "check_ok", "valid"), UNP_CASES)
def test_unp(agg: ModuleType, value: str, check_ok: bool, valid: bool) -> None:
    assert agg.unp_check_ok(value) is check_ok
    assert agg.unp_is_valid(value) is valid


def test_main_writes_reproducible_json(agg: ModuleType, tmp_path: Path) -> None:
    participants = _participants(tmp_path, ROWS)
    out = tmp_path / "supplier-regions.json"
    agg.main(["--participants", str(participants), "--out", str(out)])
    first = out.read_bytes()
    data = json.loads(first)
    assert data["total_suppliers"] == 14 and data["source"].startswith("participants.parquet")
    assert data["data_built_at"] is None  # no manifest next to the parquet
    agg.main(["--participants", str(participants), "--out", str(out)])
    assert out.read_bytes() == first  # no wall-clock time in the output

    (tmp_path / "manifest.json").write_text('{"built_at": "2026-10-01T18:23:26+00:00"}', encoding="utf-8")
    agg.main(["--participants", str(participants), "--out", str(out)])
    assert json.loads(out.read_text(encoding="utf-8"))["data_built_at"] == "2026-10-01T18:23:26+00:00"
