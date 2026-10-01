"""Full FNS MSP registry dump (opendata 7707329152-rsmp) → parquet, one part per XML member (#185).

    uv run python -m src.sources.msp_dump [--zip PATH] [--out DIR] [--workers N]   # make msp-dump
    uv run python -m src.sources.msp_dump --stats                                   # coverage numbers

The dump is a ZIP of ~7.5k XML files (~22.7 GB unpacked, ~6.8M records). Members are parsed
straight from the ZIP, in parallel, each into `<out>/<member>.parquet`; an existing part is skipped,
so a rerun continues where it stopped. One row per record (`Документ`): ЮЛ or ИП, INN (with
`inn_valid`; a bad checksum is kept and marked, never fixed), name, region, main / additional /
reporting OKVED, products (`СвПрод`, rare: ~0.14 % of records), licences, MSP category, headcount.

Every value is copied as is from one record. `record()` returns that record's raw XML, which is
the page an Evidence of a dump fact is checked against (src/enrich/evidence.py): the source is the
dump file + member + `ИдДок`. Licence: FNS open data, commercial use allowed (research/SOURCES.md).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from src.contracts import inn_is_valid

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
DATASET_URL = "https://file.nalog.ru/opendata/7707329152-rsmp/data-10092026-structure-12052026.zip"
ZIP = ROOT / "data" / "external" / "rsmp" / DATASET_URL.rsplit("/", 1)[1]
OUT = ROOT / "data" / "external" / "rsmp" / "parts"

SCHEMA = pa.schema([
    ("inn", pa.string()), ("inn_valid", pa.bool_()), ("ogrn", pa.string()), ("entity", pa.string()),
    ("name", pa.string()), ("name_full", pa.string()),
    ("region_code", pa.string()), ("region", pa.string()), ("city", pa.string()),
    ("okved_main", pa.string()), ("okved_main_name", pa.string()),
    ("okved_extra", pa.list_(pa.string())), ("okved_report", pa.list_(pa.string())),
    ("products", pa.list_(pa.string())), ("product_names", pa.list_(pa.string())),
    ("licences", pa.list_(pa.string())),
    ("msp_category", pa.int8()), ("employees", pa.int32()), ("new_msp", pa.bool_()),
    ("included", pa.string()), ("state_date", pa.string()),
    ("src_file", pa.string()), ("doc_id", pa.string()),
])


def _date(d: str | None) -> str | None:
    """dd.mm.yyyy → ISO yyyy-mm-dd."""
    if not d:
        return None
    day, month, year = d.split(".")
    return f"{year}-{month}-{day}"


def _place(el: ET.Element | None) -> str | None:
    return f"{el.get('Тип', '')} {el.get('Наим', '')}".strip() if el is not None else None


def _record(doc: ET.Element, src_file: str) -> dict:
    org, ip = doc.find("ОргВклМСП"), doc.find("ИПВклМСП")
    if org is not None:
        inn, ogrn, entity = org.get("ИННЮЛ"), org.get("ОГРН"), "ЮЛ"
        name_full = org.get("НаимОрг")
        name = org.get("НаимОргСокр") or name_full
    else:
        assert ip is not None, f"{src_file}: record {doc.get('ИдДок')} is neither ЮЛ nor ИП"
        fio = ip.find("ФИОИП")
        parts = [fio.get(k, "") for k in ("Фамилия", "Имя", "Отчество")] if fio is not None else []
        inn, ogrn, entity = ip.get("ИННФЛ"), ip.get("ОГРНИП"), "ИП"
        name_full = " ".join(p for p in parts if p) or None
        name = f"ИП {name_full}" if name_full else None
    place = doc.find("СведМН")
    city = next((el for tag in ("Город", "НаселПункт") if (el := doc.find(f"СведМН/{tag}")) is not None), None)
    main = doc.find("СвОКВЭД/СвОКВЭДОсн")
    staff = doc.get("ССЧР")
    return {
        "inn": inn, "inn_valid": bool(inn) and inn_is_valid(inn), "ogrn": ogrn, "entity": entity,
        "name": name, "name_full": name_full,
        "region_code": place.get("КодРегион") if place is not None else None,
        "region": _place(doc.find("СведМН/Регион")), "city": _place(city),
        "okved_main": main.get("КодОКВЭД") if main is not None else None,
        "okved_main_name": main.get("НаимОКВЭД") if main is not None else None,
        "okved_extra": [e.get("КодОКВЭД", "") for e in doc.findall("СвОКВЭД/СвОКВЭДДоп")],
        "okved_report": [e.get("КодОКВЭД", "") for e in doc.findall("СвОКВЭДотч/*")],
        "products": [e.get("КодПрод", "") for e in doc.findall("СвПрод")],
        "product_names": [e.get("НаимПрод", "") for e in doc.findall("СвПрод")],
        "licences": [(e.text or "").strip() for e in doc.findall("СвЛиценз/НаимЛицВД")],
        "msp_category": int(doc.get("КатСубМСП", "0")) or None,
        "employees": int(staff) if staff else None,
        "new_msp": doc.get("ПризНовМСП") == "1",
        "included": _date(doc.get("ДатаВклМСП")), "state_date": _date(doc.get("ДатаСост")),
        "src_file": src_file, "doc_id": doc.get("ИдДок"),
    }


def parse_file(xml: bytes, src_file: str) -> list[dict]:
    """All records of one XML member of the dump."""
    return [_record(doc, src_file) for doc in ET.fromstring(xml).iter("Документ")]


def _part(name: str) -> str:
    return Path(name).stem + ".parquet"


def _build_one(zip_path: str, member: str, out: str) -> int:
    with zipfile.ZipFile(zip_path) as z:
        rows = parse_file(z.read(member), member)
    tmp = Path(out) / (_part(member) + ".tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), tmp, compression="zstd")
    tmp.replace(Path(out) / _part(member))  # a part either exists whole or not at all
    return len(rows)


def build(zip_path: Path, out: Path, workers: int | None = None) -> dict[str, int]:
    """Parse every XML member not yet in `out`; returns counters of this run."""
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if n.endswith(".xml")]
    todo = [m for m in members if not (out / _part(m)).exists()]
    log.info("%s: %d файлов, готово %d, в этом запуске %d", zip_path.name, len(members), len(members) - len(todo),
             len(todo))
    rows, started = 0, time.monotonic()
    if workers == 1:
        rows = sum(_build_one(str(zip_path), m, str(out)) for m in todo)
    else:
        with ProcessPoolExecutor(workers or os.cpu_count()) as ex:
            futures = [ex.submit(_build_one, str(zip_path), m, str(out)) for m in todo]
            for i, f in enumerate(futures, 1):
                rows += f.result()
                if i % 200 == 0 or i == len(todo):
                    rate = i / max(time.monotonic() - started, 1e-3)
                    log.info("%d/%d файлов, %d записей (осталось ≈ %.1f мин)", i, len(todo), rows,
                             (len(todo) - i) / rate / 60)
    return {"members": len(members), "built": len(todo), "skipped": len(members) - len(todo), "rows": rows}


def record(zip_path: Path, member: str, doc_id: str) -> str | None:
    """Raw XML of one record (`<Документ ИдДок=…>…</Документ>`): the page a dump Evidence is checked against."""
    with zipfile.ZipFile(zip_path) as z:
        text = z.read(member).decode("utf-8")
    start = text.find(f'<Документ ИдДок="{doc_id}"')
    if start < 0:
        return None
    end = text.find("</Документ>", start)
    return text[start:end + len("</Документ>")]


def stats(out: Path, suppliers: Path) -> dict:
    """Dump totals and coverage of organizers' suppliers (who of them the dump names)."""
    import duckdb

    parts = (out / "*.parquet").as_posix()
    con = duckdb.connect()
    q = f"""SELECT count(*) AS records, count(*) FILTER (WHERE entity = 'ЮЛ') AS ul,
              count(*) FILTER (WHERE region_code = '78') AS spb, count(*) FILTER (WHERE region_code = '47') AS lo,
              count(*) FILTER (WHERE NOT inn_valid) AS bad_inn,
              count(*) FILTER (WHERE len(products) > 0) AS with_products,
              count(*) FILTER (WHERE len(licences) > 0) AS with_licences,
              count(*) FILTER (WHERE len(okved_report) > 0) AS with_okved_report,
              count(*) FILTER (WHERE employees IS NOT NULL) AS with_employees,
              round(avg(len(okved_extra)), 1) AS avg_okved_extra,
              count(DISTINCT inn) AS distinct_inn
            FROM read_parquet('{parts}')"""
    cur = con.execute(q)
    res = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))  # type: ignore[arg-type]
    if suppliers.exists():
        cov = con.execute(f"""SELECT count(*), count(m.inn) FROM read_parquet('{suppliers.as_posix()}') s
                              LEFT JOIN (SELECT DISTINCT inn FROM read_parquet('{parts}')) m ON m.inn = s.inn
                              WHERE s.inn_valid""").fetchone()
        res["suppliers_valid_inn"], res["suppliers_in_dump"] = cov  # type: ignore[misc]
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FNS MSP registry dump → parquet parts (#185)")
    ap.add_argument("--zip", type=Path, default=ZIP)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--workers", type=int)
    ap.add_argument("--stats", action="store_true", help="print totals and suppliers' coverage, no parsing")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.stats:
        print(json.dumps(stats(args.out, ROOT / "data" / "processed" / "suppliers.parquet"), ensure_ascii=False))
        return 0
    if not args.zip.exists():
        print(f"нет {args.zip}: скачать {DATASET_URL} (2.1 ГБ, подтверждение №4 — #185)", file=sys.stderr)
        return 2
    print(json.dumps(build(args.zip, args.out, args.workers), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
