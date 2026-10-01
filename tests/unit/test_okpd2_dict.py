"""OKPD2 dictionary (#154): import of the data.mos.ru export, lookup with fallback to the parent code
for codes of older editions, and the code → name table for the items of the organizers' data."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import duckdb

from src.ingest import okpd2

# data.mos.ru export format: two header rows (tech names, Russian names), trailing `;`, section rows
# without a code.
EXPORT = (
    '"Name";"Idx";"Razdel";"Kod";"Nomdescr";"global_id";\n'
    '"Наименование";"Индекс";"Код раздела";"Код";"Описание";"global_id";\n'
    '"УСЛУГИ ОБРАЗОВАНИЯ";"P";"P";"";"";"1";\n'
    '"Услуги в области образования";"P.85";"P";"85";"";"2";\n'
    '"Бумага и картон";"C.17.12";"C";"17.12";"Эта группировка включает: ...";"3";\n'
    '"Бумага для печати";"C.17.12.14";"C";"17.12.14";"";"4";\n'
    '"Бумага офисная ""для принтеров""";"C.17.12.14.110";"C";"17.12.14.110";"";"5";\n'
)
NAMES = {"85": "Услуги в области образования", "17.12": "Бумага и картон", "17.12.14": "Бумага для печати",
         "17.12.14.110": 'Бумага офисная "для принтеров"'}


def _zip(tmp_path: Path) -> Path:
    path = tmp_path / "okpd2.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("data-2752-01-10-2026.csv", ("﻿" + EXPORT).encode("utf-8"))
    return path


def test_import_export_writes_snapshot_and_meta(tmp_path: Path):
    snap, meta = tmp_path / "okpd2.csv", tmp_path / "okpd2.meta.json"
    assert okpd2.import_export(_zip(tmp_path), snap, meta) == 4
    assert okpd2.read_snapshot(snap) == NAMES
    m = json.loads(meta.read_text(encoding="utf-8"))
    assert m["file"] == "data-2752-01-10-2026.csv" and m["rows"] == 4 and m["url"].startswith("https://data.mos.ru/")


def test_resolve_exact_parent_unknown():
    assert okpd2.resolve("17.12.14.110", NAMES) == ("17.12.14.110", NAMES["17.12.14.110"], "exact")
    # a code of an older edition: the nearest parent that the dictionary has
    assert okpd2.resolve("17.12.14.190", NAMES) == ("17.12.14", "Бумага для печати", "parent")
    assert okpd2.resolve(" 85.11 ", NAMES) == ("85", NAMES["85"], "parent")
    assert okpd2.resolve("99.99.99.999", NAMES) is None
    assert okpd2.resolve("", NAMES) is None


def test_build_table_covers_codes_of_items(tmp_path: Path):
    snap, meta = tmp_path / "okpd2.csv", tmp_path / "okpd2.meta.json"
    okpd2.import_export(_zip(tmp_path), snap, meta)
    items = tmp_path / "items.parquet"
    duckdb.sql("SELECT * FROM (VALUES (1, '17.12.14.110'), (2, '17.12.14.110'), (3, '17.12.14.190'), "
               "(4, '99.99.99.999'), (5, NULL)) t(lot_id, okpd2_code)").write_parquet(items.as_posix())
    out = tmp_path / "okpd2.parquet"

    stats = okpd2.build_table(items, out, snap)

    assert stats == {"codes": 3, "exact": 1, "parent": 1, "missing": 1, "rows": 4, "rows_named": 3}
    rows = dict(duckdb.sql(f"SELECT code, (name, matched_code, match) FROM '{out.as_posix()}'").fetchall())
    assert len(rows) == len(NAMES) + 1  # the dictionary plus the old code resolved to its parent
    assert rows["17.12.14.110"] == (NAMES["17.12.14.110"], "17.12.14.110", "exact")
    assert rows["17.12.14.190"] == ("Бумага для печати", "17.12.14", "parent")
    assert "99.99.99.999" not in rows


def test_committed_snapshot_is_readable():
    names = okpd2.read_snapshot()
    assert len(names) > 20_000
    assert names["17.12.14"].startswith("Бумага")
