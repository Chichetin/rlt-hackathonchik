"""ФНС open data (nalog.gov.ru/opendata): offline facts by INN. Owner: SOURCES.

pb.nalog.ru asks for a captcha since 01.10 ~14:50; these datasets are open ZIP/XML dumps
without key or captcha (research/SOURCES.md, #59). Commands:

    python -m src.sources.fns_opendata download   # 4 archives → data/external/fns_opendata/
    python -m src.sources.fns_opendata build      # streaming parse → data/processed/fns_opendata.parquet
    python -m src.sources.fns_opendata lookup <inn>

XML (checked on the 25.09.2026 archives): `Файл/Документ[@ДатаСост]` → `СведНП[@НаимОрг, @ИННЮЛ]` +
one dataset element: `СведССЧР[@КолРаб]` (sshr2019), `СведДохРасх[@СумДоход, @СумРасход]`
(revexp), `СведСНР[@ПризнЕСХН, @ПризнУСН, @ПризнАУСН, @ПризнСРП]` (snr), one or more
`СведНедоим[@НаимНалог, @СумНедНалог, @СумПени, @СумШтраф, @ОбщСумНедоим]` (debtam). Legal
entities only (`ИННЮЛ`), one document per INN.

Cached text (src/sources/cache.py, url `<archive url>#inn=<inn>`): one `Элемент.Атрибут: значение`
line per XML attribute of the document, values verbatim (only XML escaping removed), `ИдДок`
skipped. Every Evidence `value` is one whole line of it, so src/enrich/evidence.py checks it.

An INN missing from a dataset gives no fact. In particular absence from debtam is NOT «долгов
нет»: the dataset lists only organisations with debt on its date, and nothing in its structure
says that every debtor is listed; Ростелеком is in none of the four sets.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from src.contracts import Evidence, inn_is_valid
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; RLTHack-Enrich/0.1; hackathon, cached, paced)"
PAUSE_S = 5.0
BATCH = 200_000
REGIMES = {"ПризнЕСХН": "ЕСХН", "ПризнУСН": "УСН", "ПризнАУСН": "АУСН", "ПризнСРП": "СРП"}

SCHEMA = pa.schema([
    ("inn", pa.string()), ("dataset", pa.string()), ("as_of", pa.date32()),
    ("fetched_at", pa.float64()),  # unix time of the archive download (file mtime)
    ("employees", pa.int64()), ("income", pa.float64()), ("expenses", pa.float64()),
    ("regimes", pa.string()), ("tax_debt", pa.float64()), ("text", pa.string()),
])


@dataclass(frozen=True)
class Dataset:
    id: str
    title: str
    url: str


DATASETS = {
    d.id: d
    for d in (
        Dataset("sshr2019", "среднесписочная численность работников",
                "https://file.nalog.ru/opendata/7707329152-sshr2019/data-20260925-structure-20200408.zip"),
        Dataset("revexp", "доходы и расходы по данным бухгалтерской отчётности",
                "https://file.nalog.ru/opendata/7707329152-revexp/data-20260925-structure-20180110.zip"),
        Dataset("snr", "специальные налоговые режимы",
                "https://file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip"),
        Dataset("debtam", "задолженность по налогам и сборам",
                "https://file.nalog.ru/opendata/7707329152-debtam/data-20260925-structure-20181201.zip"),
    )
}


# read at call time: tests and the demo point these elsewhere after import (as SourceCache does)
def _external() -> Path:
    return Path(os.environ.get("FNS_OPENDATA_DIR", "data/external/fns_opendata"))


def _parquet() -> Path:
    return Path(os.environ.get("FNS_OPENDATA_PARQUET", "data/processed/fns_opendata.parquet"))


def archive_path(ds: Dataset, root: Path | None = None) -> Path:
    return (root or _external()) / ds.url.rsplit("/", 1)[1].replace("data-", f"{ds.id}-", 1)


def page_url(ds: Dataset, inn: str) -> str:
    return f"{ds.url}#inn={inn}"


def download(root: Path | None = None) -> None:
    """Sequential, paced; an archive with the server's size is skipped (no resume)."""
    root = root or _external()
    root.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": UA}, timeout=60.0, follow_redirects=True) as client:
        for i, ds in enumerate(DATASETS.values()):
            path = archive_path(ds, root)
            size = int(client.head(ds.url).headers.get("content-length", "0"))
            if path.exists() and path.stat().st_size == size:
                log.info("%s: есть, %d байт — пропуск", ds.id, size)
                continue
            if i:
                time.sleep(PAUSE_S)
            tmp = path.with_suffix(".part")
            t0, done, next_log = time.monotonic(), 0, 0
            with client.stream("GET", ds.url) as r, tmp.open("wb") as f:
                r.raise_for_status()
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if done >= next_log:
                        log.info("%s: %d / %d МБ", ds.id, done >> 20, size >> 20)
                        next_log += 10 << 20
            if size and done != size:
                raise OSError(f"{ds.id}: получено {done} байт, ожидалось {size}")
            tmp.replace(path)
            log.info("%s: скачано %d МБ за %.0f с → %s", ds.id, done >> 20, time.monotonic() - t0, path)


Pairs = list[tuple[str, str]]


def _documents(path: Path) -> Iterator[Pairs]:
    """Streaming: one document at a time as (`Элемент.Атрибут`, value) pairs in XML order."""
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            with z.open(name) as f:
                for _, el in ET.iterparse(f):
                    if el.tag == "Документ":
                        yield [(f"{e.tag}.{k}", v) for e in el.iter() for k, v in e.attrib.items() if k != "ИдДок"]
                        el.clear()


def _get(pairs: Pairs, key: str) -> str | None:
    return next((v for k, v in pairs if k == key), None)


def _date(s: str) -> date:
    return datetime.strptime(s, "%d.%m.%Y").date()


def _debts(pairs: Pairs) -> list[tuple[str, str]]:
    """debtam: (НаимНалог, ОбщСумНедоим) per `СведНедоим` element."""
    names = [v for k, v in pairs if k == "СведНедоим.НаимНалог"]
    sums = [v for k, v in pairs if k == "СведНедоим.ОбщСумНедоим"]
    return list(zip(names, sums, strict=True))


def values(dataset: str, pairs: Pairs) -> dict[str, int | float | str | None]:
    """Parquet value columns of one document."""
    if dataset == "sshr2019":
        n = _get(pairs, "СведССЧР.КолРаб")
        return {"employees": int(n) if n is not None else None}
    if dataset == "revexp":
        inc, exp = _get(pairs, "СведДохРасх.СумДоход"), _get(pairs, "СведДохРасх.СумРасход")
        return {"income": float(inc) if inc else None, "expenses": float(exp) if exp else None}
    if dataset == "snr":
        on = [name for k, v in pairs if v == "1" and (name := REGIMES.get(k.removeprefix("СведСНР.")))]
        return {"regimes": ",".join(on)}
    if dataset == "debtam":
        return {"tax_debt": float(sum(Decimal(s) for _, s in _debts(pairs)))}
    raise KeyError(dataset)


def _rub(amount: Decimal) -> str:
    q = amount.quantize(Decimal("0.01"))
    whole = f"{int(q):,}".replace(",", " ")
    return whole if q == int(q) else f"{whole},{str(q).split('.')[1]}"


def evidence(ds: Dataset, pairs: Pairs, url: str, at: datetime) -> list[Evidence]:
    """Human facts of one document; `value` is the whole `key: value` line the fact rests on."""
    out: list[Evidence] = []
    as_of = _get(pairs, "Документ.ДатаСост") or ""
    source = f"ФНС, открытые данные: {ds.title}"

    def add(field: str, key: str, value: str, fact: str) -> None:
        out.append(Evidence(source=source, url=url, fetched_at=at, fact=fact, field=field, value=f"{key}: {value}"))

    for key, v in pairs:
        if ds.id == "sshr2019" and key == "СведССЧР.КолРаб":
            add("employees", key, v, f"Среднесписочная численность работников за {as_of[-4:]} г. "
                                     f"(сведения на {as_of}): {v} чел.")
        elif ds.id == "revexp" and key == "СведДохРасх.СумДоход":
            add("income", key, v, f"Доходы за {as_of[-4:]} г. по бухгалтерской отчётности: {_rub(Decimal(v))} ₽")
        elif ds.id == "revexp" and key == "СведДохРасх.СумРасход":
            add("expenses", key, v, f"Расходы за {as_of[-4:]} г. по бухгалтерской отчётности: {_rub(Decimal(v))} ₽")
        elif ds.id == "snr" and v == "1" and (name := REGIMES.get(key.removeprefix("СведСНР."))):
            add("tax_regime", key, v, f"Применяет {name} (сведения на {as_of})")
    if ds.id == "debtam" and (debts := _debts(pairs)):
        total = sum((Decimal(s) for _, s in debts), Decimal(0))
        top_name, top = max(debts, key=lambda d: Decimal(d[1]))
        n = len(debts)
        noun = "позиция" if n % 10 == 1 and n % 100 != 11 else (
            "позиции" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "позиций")
        add("tax_debt", "СведНедоим.ОбщСумНедоим", top,
            f"Недоимка, пени и штрафы на {as_of}: {_rub(total)} ₽ ({n} {noun}; наибольшая — "
            f"{top_name}, {_rub(Decimal(top))} ₽)")
    return out


def parse(text: str) -> Pairs:
    return [(k, v) for k, sep, v in (line.partition(": ") for line in text.splitlines()) if sep]


def build(root: Path | None = None, out: Path | None = None, force: bool = False) -> dict[str, int]:
    """All four archives → one parquet sorted by INN. Up to date (newer than every archive) → {}."""
    root, out = root or _external(), out or _parquet()
    archives = {ds.id: archive_path(ds, root) for ds in DATASETS.values()}
    missing = [str(p) for p in archives.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"нет архивов, сначала download: {missing}")
    if not force and out.exists() and out.stat().st_mtime >= max(p.stat().st_mtime for p in archives.values()):
        log.info("%s новее архивов — пропуск (force=True — пересобрать)", out)
        return {}
    t0 = time.monotonic()
    tmp_dir = out.parent / f".{out.name}.parts"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    counts: dict[str, int] = {}
    for ds_id, path in archives.items():
        fetched = path.stat().st_mtime
        n, rows = 0, []
        with pq.ParquetWriter(tmp_dir / f"{ds_id}.parquet", SCHEMA) as writer:
            for pairs in _documents(path):
                inn, as_of = _get(pairs, "СведНП.ИННЮЛ"), _get(pairs, "Документ.ДатаСост")
                if not inn or not as_of:
                    log.warning("%s: документ без ИННЮЛ или ДатаСост — пропуск", ds_id)
                    continue
                rows.append({"inn": inn, "dataset": ds_id, "as_of": _date(as_of), "fetched_at": fetched,
                             "text": "\n".join(f"{k}: {v}" for k, v in pairs), **values(ds_id, pairs)})
                n += 1
                if len(rows) >= BATCH:
                    writer.write_table(pa.Table.from_pylist(rows, schema=SCHEMA))
                    rows = []
                    log.info("%s: %d документов, %.0f с", ds_id, n, time.monotonic() - t0)
            if rows:
                writer.write_table(pa.Table.from_pylist(rows, schema=SCHEMA))
        counts[ds_id] = n
        log.info("%s: всего %d документов, %.0f с", ds_id, n, time.monotonic() - t0)
    tmp_out = out.with_suffix(".tmp")
    duckdb.execute(
        f"COPY (SELECT * FROM read_parquet('{tmp_dir}/*.parquet') ORDER BY inn, dataset) TO '{tmp_out}' "
        "(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)")
    tmp_out.replace(out)
    shutil.rmtree(tmp_dir)
    log.info("%s: %d строк, %.1f МБ, %.0f с", out, sum(counts.values()), out.stat().st_size / 2**20,
             time.monotonic() - t0)
    return counts


def lookup(inn: str, cache: SourceCache | None = None, parquet: Path | None = None) -> list[Evidence]:
    """Facts by INN from the built parquet; the document text goes to the cache under page_url()."""
    if not inn_is_valid(inn):
        raise ValueError(f"INN {inn!r} fails the checksum")
    path = parquet or _parquet()
    if not path.exists():
        log.warning("%s нет — открытые данные ФНС не собраны (download, build)", path)
        return []
    rows = duckdb.execute(f"SELECT dataset, fetched_at, text FROM read_parquet('{path}') WHERE inn = ? "
                          "ORDER BY dataset", [inn]).fetchall()
    cache = cache or SourceCache()
    out: list[Evidence] = []
    for ds_id, fetched_at, text in rows:
        ds = DATASETS[ds_id]
        page = cache.put(page_url(ds, inn), text, fetched_at=fetched_at)
        out += evidence(ds, parse(page.body), page.url, datetime.fromtimestamp(page.fetched_at, tz=UTC))
    return out


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = argv[0] if argv else ""
    if cmd == "download":
        download()
        return 0
    if cmd == "build":
        build(force="--force" in argv)
        return 0
    if cmd == "lookup" and len(argv) == 2:
        from src.enrich.evidence import check_evidence

        cache = SourceCache()
        for e in lookup(argv[1], cache):
            errors = check_evidence(e, cache.page_text)
            print(f"{e.fact}\n  {e.source} | {e.url} | {e.fetched_at:%Y-%m-%d %H:%M} UTC | {e.field} = {e.value!r}"
                  f" | проверка: {'OK' if not errors else errors}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
