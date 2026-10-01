"""OKPD2 dictionary: code → official name (#154), so that ТРУ.okpd2_code is readable.

    uv run python -m src.ingest.okpd2                       # configs/okpd2.csv → data/processed/okpd2.parquet
    uv run python -m src.ingest.okpd2 --import <export.zip> # refresh the snapshot from a data.mos.ru export

Source: «Общероссийский классификатор продукции по видам экономической деятельности ОКПД 2
(ОК 034-2014 (КПЕС 2008))», Портал открытых данных Правительства Москвы, dataset 2752 (`URL`).
robots.txt of data.mos.ru closes `/api` and `/opendata/download` to robots, so the refresh is manual:
«Экспорт → csv (архив zip)» on the dataset page, then `--import`. The snapshot lives in git (≈ 1 МБ),
the product never goes to the site.

okpd2.parquet: the whole dictionary (match = "exact") plus the codes of items.parquet that the current
edition no longer has (older editions), each named after its nearest parent code (match = "parent").
Join: `items.okpd2_code = okpd2.code`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "configs/okpd2.csv"
META = ROOT / "configs/okpd2.meta.json"
URL = ("https://data.mos.ru/opendata/7710168515-obshcherossiyskiy-klassifikator-produktsii-po-vidam-"
       "ekonomicheskoy-deyatelnosti-okpd-2-ok-034-2014-kpes-2008")


def _export_text(path: Path) -> tuple[str, str]:
    """(file name, text) of the export: the zip from the site or the CSV inside it."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            return name, z.read(name).decode("utf-8-sig")
    return path.name, path.read_text(encoding="utf-8-sig")


def import_export(path: Path, dest: Path = SNAPSHOT, meta: Path = META) -> int:
    """data.mos.ru export → snapshot `code;name;section` sorted by code; returns the number of codes."""
    name, text = _export_text(path)
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    next(reader)  # second header row: Russian column names
    rows = {r["Kod"].strip(): (r["Name"].strip(), r["Razdel"].strip()) for r in reader if r["Kod"].strip()}
    with dest.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";", lineterminator="\n")
        w.writerow(["code", "name", "section"])
        w.writerows((code, nm, sec) for code, (nm, sec) in sorted(rows.items()))
    meta.write_text(json.dumps({
        "url": URL, "file": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": len(rows), "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(rows)


def read_snapshot(path: Path = SNAPSHOT) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as f:
        return {r["code"]: r["name"] for r in csv.DictReader(f, delimiter=";")}


def resolve(code: str, names: dict[str, str]) -> tuple[str, str, str] | None:
    """(matched code, name, "exact" | "parent"); the parent is the longest prefix the dictionary has."""
    code = code.strip()
    if code in names:
        return code, names[code], "exact"
    while code:
        code = code[:-1].rstrip(".")
        if code in names:
            return code, names[code], "parent"
    return None


def build_table(items: Path, out: Path, snapshot: Path = SNAPSHOT) -> dict[str, int]:
    """okpd2.parquet for the codes of items.parquet; returns coverage of codes and of item rows."""
    names = read_snapshot(snapshot)
    counts = duckdb.execute(
        "SELECT trim(okpd2_code), count(*) FROM read_parquet(?) "
        "WHERE okpd2_code IS NOT NULL AND trim(okpd2_code) <> '' GROUP BY 1", [items.as_posix()]).fetchall()
    rows = [(c, nm, c, "exact") for c, nm in names.items()]
    stats = {"codes": len(counts), "exact": 0, "parent": 0, "missing": 0, "rows": 0, "rows_named": 0}
    for code, n in counts:
        hit = resolve(code, names)
        stats["rows"] += n
        stats[hit[2] if hit else "missing"] += 1
        if hit:
            stats["rows_named"] += n
            if hit[2] == "parent":
                rows.append((code, hit[1], hit[0], "parent"))
    con = duckdb.connect()
    con.execute("CREATE TABLE okpd2 (code VARCHAR, name VARCHAR, matched_code VARCHAR, match VARCHAR)")
    con.executemany("INSERT INTO okpd2 VALUES (?, ?, ?, ?)", rows)
    con.execute("COPY (SELECT * FROM okpd2 ORDER BY code) TO ? (FORMAT parquet)", [out.as_posix()])
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="справочник ОКПД2: код → наименование (#154)")
    ap.add_argument("--import", dest="export", type=Path, help="выгрузка data.mos.ru (zip или csv) → снимок")
    ap.add_argument("--items", type=Path, default=ROOT / "data/processed/items.parquet")
    ap.add_argument("--out", type=Path, default=ROOT / "data/processed/okpd2.parquet")
    args = ap.parse_args(argv)
    if args.export:
        print(f"снимок {SNAPSHOT.relative_to(ROOT).as_posix()}: {import_export(args.export)} кодов")
    if not args.items.exists():
        print(f"нет {args.items} — сначала python -m src.ingest.build", file=sys.stderr)
        return 2
    s = build_table(args.items, args.out)
    print(f"{args.out}: кодов в позициях {s['codes']} — точно {s['exact']}, по родителю {s['parent']}, "
          f"без названия {s['missing']}; позиций с названием {s['rows_named']} из {s['rows']} "
          f"({s['rows_named'] / max(s['rows'], 1):.2%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
