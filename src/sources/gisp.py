"""ГИСП: реестр российской промышленной продукции (ПП РФ № 719, Минпромторг) — offline export (#74). Owner: SOURCES.

The registry lists products made in Russia with the INN of the maker and the OKPD2 code of each product, so it
answers two questions the main OKVED cannot: «does this company make THIS product» (product_fit 1.0 of the block
«Новые», research/METHODOLOGY.md) and «is it a manufacturer» by a state registry, not by a self-declared code
(the first basis of the role, #186; src/sources/role.py).

Source: the registry page https://gisp.gov.ru/pp719v2/pub/prod/ offers «Скачать только действующие (XLSX)»
(DOWNLOAD_URL, ≈ 60 MB, ≈ 480 k products of ≈ 8 k companies on 02.10.2026). gisp.gov.ru sits behind a JS challenge
(ServicePipe): a plain HTTP client gets the challenge page, a headless browser passes it (D-085 allows it). One
download per run of `scripts/gisp_download.mjs`, not a crawl; then everything is offline, like the МСП dump
(src/sources/msp_dump.py):

    node scripts/gisp_download.mjs /mnt/storage/rlt236/gisp/pp719_valid.xlsx
    uv run python -m src.sources.gisp build /mnt/storage/rlt236/gisp/pp719_valid.xlsx   # → data/external/gisp
    uv run python -m src.sources.gisp stats

`build` keeps every row of the export (`inn_valid` marks a bad checksum, such rows are never returned) in
`<GISP_DIR>/products.parquet` and the export stamp («Время выгрузки»), the download time and the URL in
`meta.json` of `DIR` = data/external/gisp (not in git; a symlink to a larger disk is fine). No environment
variable: the environment is read only in src/settings.py (#290); `--out` / `--dir` of the CLI, `DIR` in tests.

Facts: a product record becomes a cached page (`page_url(inn, reg_number)`, the registry URL with `#inn=…&reg=…`,
`page()`: flat `gisp.<inn>.<key>: <value>` lines, values as in the export with whitespace collapsed), put into
src/sources/cache.py when a company is shown, so src/enrich/evidence.py checks every fact against it. The role
basis is the product line `gisp.<inn>.product: <name>` of a page of THIS INN (role.basis_kind).
No export → `available()` is False and every lookup is empty: discovery works as without this source.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from src.contracts import Counterparty, Evidence, inn_is_valid
from src.settings import get_settings
from src.sources import role as role_mod
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
SOURCE = "Минпромторг: реестр российской промышленной продукции (ГИСП)"
REGISTRY_URL = "https://gisp.gov.ru/pp719v2/pub/prod/"
DOWNLOAD_URL = "https://gisp.gov.ru/pp719v2/mptapp/view/dl/production_res_valid_only/"
DIR: Path | None = None  # override (tests, the coverage script); None = next to DATA_PROCESSED_DIR, see gisp_dir()
PRODUCTS = "products.parquet"
META = "meta.json"
SHEET = "Продукция"
MAX_PRODUCTS = 3  # products of one company shown as facts (the best agreeing ones)
MIN_DEPTH = 5  # «XX.XX»: a shorter code (section, subclass) is too wide to say «this product»
KIND_DEPTH = 8  # «XX.XX.XX» (вид): codes that agree down to it name the same product of the classifier
_CODE = re.compile(r"\d{2}\.\d{2}(?:\.\d{1,2}(?:\.\d{1,3})?)?", re.ASCII)  # XX.XX … XX.XX.XX.XXX

# export header → column; the rest of the export (scores, conclusions) is not used and not stored
COLUMNS = {
    "Предприятие": "org_name", "ИНН": "inn", "ОГРН": "ogrn",
    "Первичный регистрационный номер реестровой записи": "reg_number_primary", "Реестровый номер": "reg_number",
    "Дата внесения в реестр": "res_date", "Срок действия": "valid_till",
    "Фактическая дата прекращения действия реестровой записи": "end_date",
    "Наименование продукции": "name", "ОКПД2": "okpd2", "ТН ВЭД": "tnved", "Изготовлена по": "spec",
}
SCHEMA = pa.schema([("inn", pa.string()), ("inn_valid", pa.bool_()),
                    *((c, pa.string()) for c in COLUMNS.values() if c != "inn")])


@dataclass(frozen=True)
class Product:
    inn: str
    ogrn: str | None
    org_name: str
    reg_number: str
    name: str
    okpd2: str
    tnved: str | None
    spec: str | None
    res_date: str | None  # ISO date
    valid_till: str | None  # ISO date


def gisp_dir() -> Path:
    """The one place that says where the export lives: DIR if set, else <DATA_PROCESSED_DIR>/../external/gisp
    (data/external/gisp by default; the e2e server has its own DATA_PROCESSED_DIR, so it never sees the real one)."""
    if DIR is not None:
        return DIR  # read at the call: tests and the coverage script point it elsewhere
    return get_settings().data_processed_dir.parent / "external" / "gisp"


def _text(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime | date):
        return v.strftime("%Y-%m-%d")
    s = " ".join(str(v).split())
    return s or None


def build(xlsx: Path, out: Path, *, fetched_at: float | None = None) -> dict:
    """The XLSX export → `products.parquet` + `meta.json`. `fetched_at` — the download time (default: file mtime)."""
    import openpyxl

    wb = openpyxl.load_workbook(xlsx, read_only=True)
    try:
        rows = wb[SHEET].iter_rows(values_only=True)
        stamp = next(rows)
        header: tuple = ()
        for header in rows:  # the header follows the stamp and a blank row
            if header and header[0] == "Предприятие":
                break
        index = {h: i for i, h in enumerate(header)}
        missing = [h for h in COLUMNS if h not in index]
        if missing:
            raise ValueError(f"ГИСП: в выгрузке нет столбцов {missing}")
        cols: dict[str, list] = {f.name: [] for f in SCHEMA}
        for row in rows:
            if not row or len(row) <= index["ИНН"] or row[index["ИНН"]] is None:
                continue
            for head, col in COLUMNS.items():
                i = index[head]
                cols[col].append(_text(row[i]) if i < len(row) else None)
            cols["inn_valid"].append(inn_is_valid(cols["inn"][-1] or ""))
    finally:
        wb.close()
    out.mkdir(parents=True, exist_ok=True)
    table = pa.table(cols, schema=SCHEMA).sort_by([("inn", "ascending"), ("reg_number", "ascending")])
    pq.write_table(table, out / PRODUCTS)
    exported = str(stamp[0] or "").removeprefix("Время выгрузки:").strip() if stamp else ""
    at = float(xlsx.stat().st_mtime if fetched_at is None else fetched_at)
    meta = {"exported": exported or None, "fetched_at": at, "url": DOWNLOAD_URL, "file": xlsx.name,
            "rows": table.num_rows, "bad_inn": table.num_rows - sum(cols["inn_valid"])}
    (out / META).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def meta(directory: Path | None = None) -> dict | None:
    path = (directory or gisp_dir()) / META
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def available(directory: Path | None = None) -> bool:
    d = directory or gisp_dir()
    return (d / PRODUCTS).is_file() and meta(d) is not None


def agrees(query_code: str, product_code: str) -> bool:
    """The product's OKPD2 and the lot's one name the same product: compared down to the kind XX.XX.XX, one
    holds the other. Both must be codes of at least the class XX.XX (a section or a subclass is too wide)."""
    q, p = query_code.strip(), product_code.strip()
    if not (_CODE.fullmatch(q) and _CODE.fullmatch(p)):
        return False
    # every prefix of a valid code cut to 8 characters (5, 7 or 8 of them) is a level of the classifier
    q, p = q[:KIND_DEPTH], p[:KIND_DEPTH]
    return q.startswith(p) or p.startswith(q)


def match_level(query_code: str, product_code: str) -> str | None:
    """How the product's OKPD2 answers the lot's code (#74, review of PR #429):
    "code" — the same code, or the product's code holds the lot's one and is not wider than the kind XX.XX.XX;
    "kind" — another code of the same kind XX.XX.XX (a neighbour: office paper 17.12.14.160 for 17.12.14.110, but
    also till rolls), or the lot names only the kind; None — a wider lot code (class, subgroup) or another kind."""
    q, p = query_code.strip(), product_code.strip()
    if not (_CODE.fullmatch(q) and _CODE.fullmatch(p)):
        return None
    if p == q or (len(p) >= KIND_DEPTH and q.startswith(p)):  # a prefix of a valid code is a level boundary
        return "code"
    if len(p) >= KIND_DEPTH and len(q) >= KIND_DEPTH and p[:KIND_DEPTH] == q[:KIND_DEPTH]:
        return "kind"
    return None


def _closeness(codes: Sequence[str], product_code: str) -> int:
    """How deep the product's code agrees with the lot's closest code (longer common prefix is better)."""
    best = 0
    for code in codes:
        n = 0
        for a, b in zip(code.strip(), product_code.strip(), strict=False):
            if a != b:
                break
            n += 1
        best = max(best, n)
    return best


def _lit(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


@lru_cache(maxsize=256)
def _class_rows(path: str, mtime: int, prefix: str) -> tuple[Product, ...]:
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT inn, ogrn, org_name, reg_number, name, okpd2, tnved, spec, res_date, valid_till "
            f"FROM read_parquet({_lit(Path(path))}) WHERE inn_valid AND okpd2 LIKE ? || '%' "
            "ORDER BY inn, reg_number", [prefix]).fetchall()
    finally:
        con.close()
    return tuple(Product(*r) for r in rows)


def _products(directory: Path | None) -> Path | None:
    path = (directory or gisp_dir()) / PRODUCTS
    return path if path.is_file() else None


def by_okpd2(codes: Sequence[str], directory: Path | None = None) -> dict[str, list[Product]]:
    """INN → its products that agree with any of the lot's codes, the closest code first. Empty without the export."""
    path = _products(directory)
    codes = [c.strip() for c in codes if len(c.strip()) >= MIN_DEPTH]
    if path is None or not codes:
        return {}
    out: dict[str, list[Product]] = {}
    for prefix in sorted({c[:MIN_DEPTH] for c in codes}):
        for p in _class_rows(str(path), path.stat().st_mtime_ns, prefix):
            if any(agrees(c, p.okpd2) for c in codes):
                out.setdefault(p.inn, []).append(p)
    for products in out.values():
        products.sort(key=lambda p: (-_closeness(codes, p.okpd2), p.reg_number))
    return out


def by_inn(inns: Iterable[str], directory: Path | None = None) -> dict[str, list[Product]]:
    """INN → every product of it in the export (registry number order). Empty without the export."""
    path = _products(directory)
    wanted = sorted({i for i in inns if i and inn_is_valid(i)})
    if path is None or not wanted:
        return {}
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT inn, ogrn, org_name, reg_number, name, okpd2, tnved, spec, res_date, valid_till "
            f"FROM read_parquet({_lit(path)}) WHERE inn_valid AND inn IN (SELECT unnest(?)) "
            "ORDER BY inn, reg_number", [wanted]).fetchall()
    finally:
        con.close()
    out: dict[str, list[Product]] = {}
    for r in rows:
        out.setdefault(r[0], []).append(Product(*r))
    return out


def page_url(inn: str, reg_number: str) -> str:
    return f"{REGISTRY_URL}#inn={inn}&reg={reg_number}"


def page(p: Product, exported: str | None) -> str:
    """Cached page of one registry record: `gisp.<inn>.<key>: <value>` lines, empty values skipped."""
    pairs = [("org", p.org_name), ("inn", p.inn), ("ogrn", p.ogrn), ("reg_number", p.reg_number),
             ("product", p.name), ("okpd2", p.okpd2), ("tnved", p.tnved), ("spec", p.spec),
             ("res_date", p.res_date), ("valid_till", p.valid_till)]
    head = [f"gisp.export: {exported}"] if exported else []
    return "\n".join([*head, *(f"gisp.{p.inn}.{k}: {v}" for k, v in pairs if v)])


def basis_value(p: Product) -> str:
    """The page line that makes this INN a manufacturer (src/sources/role.py::basis_kind)."""
    return f"gisp.{p.inn}.product: {p.name}"


def _ru(iso: str | None) -> str | None:
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y") if iso else None
    except ValueError:
        return iso


def _remember(p: Product, cache: SourceCache, info: dict) -> tuple[str, datetime]:
    url, body = page_url(p.inn, p.reg_number), page(p, info.get("exported"))
    if cache.page_text(url) != body:
        cache.put(url, body, fetched_at=info["fetched_at"])
    return url, datetime.fromtimestamp(info["fetched_at"], UTC)


def product_fact(p: Product) -> str:
    till = _ru(p.valid_till)
    return (f"Производит: «{p.name}» — ОКПД2 {p.okpd2}, реестровая запись № {p.reg_number}"
            f"{f', действует до {till}' if till else ''} (реестр российской промышленной продукции Минпромторга, ГИСП)")


def role_text(p: Product, okved: str | None = None) -> str:
    """The role by role.decide(): the registry first (#186); a main OKVED of another role is named as a disagreement."""
    registry = role_mod.registry_basis(f"«{p.name}», ОКПД2 {p.okpd2}, реестровая запись № {p.reg_number} "
                                       "(реестр российской промышленной продукции Минпромторга, ПП РФ № 719)")
    return role_mod.decide(registry=registry, okved=role_mod.okved_basis(okved)).explanation()


def product_evidence(products: Sequence[Product], cache: SourceCache, directory: Path | None = None,
                     *, role: bool = True, okved: str | None = None) -> list[Evidence]:
    """Facts of the best products (up to MAX_PRODUCTS) and, with `role`, the basis of the role on the first one
    (its text — role_text() with the company's main OKVED). Every page goes to the cache first. Empty without the
    export's meta."""
    info = meta(directory)
    if info is None or not products:
        return []
    ev: list[Evidence] = []
    for i, p in enumerate(products[:MAX_PRODUCTS]):
        url, at = _remember(p, cache, info)
        ev.append(Evidence(source=SOURCE, url=url, fetched_at=at, fact=product_fact(p), field="product",
                           value=basis_value(p)))
        if role and i == 0:
            ev.append(Evidence(source=SOURCE, url=url, fetched_at=at, field="product", value=basis_value(p),
                               fact=role_text(p, okved)))
    return ev


def counterparty(inn: str, products: Sequence[Product], cache: SourceCache,
                 directory: Path | None = None) -> Counterparty | None:
    """A company found in the registry: identity from its best product's record, every fact an Evidence."""
    info = meta(directory)
    if info is None or not products or not inn_is_valid(inn):
        return None
    p = products[0]
    url, at = _remember(p, cache, info)
    ev = [Evidence(source=SOURCE, url=url, fetched_at=at, field="inn", value=inn,
                   fact=f"ИНН {inn} — изготовитель продукции в реестре российской промышленной продукции Минпромторга"),
          Evidence(source=SOURCE, url=url, fetched_at=at, field="name", value=p.org_name,
                   fact=f"Наименование по реестру Минпромторга: {p.org_name}")]
    if p.ogrn:
        ev.append(Evidence(source=SOURCE, url=url, fetched_at=at, field="ogrn", value=p.ogrn, fact=f"ОГРН {p.ogrn}"))
    ev += product_evidence(products, cache, directory)
    return Counterparty(inn=inn, name=p.org_name, kind="manufacturer", evidence=ev)


def with_role(cp: Counterparty, products: Sequence[Product], cache: SourceCache,
              directory: Path | None = None) -> Counterparty:
    """The registry is the first basis of the role (#186, role.decide): a company with a product in it is a
    manufacturer; a main OKVED of another role is named in the same fact as a disagreement, its own fact stays.
    A card that already has registry facts is returned as it is (the card after discovery: no second role)."""
    if not products or cp.inn is None or any(p.inn != cp.inn for p in products):
        return cp
    if any(e.source == SOURCE for e in cp.evidence):
        return cp
    have = {(e.url, e.fact) for e in cp.evidence}
    okved = cp.okved[0] if cp.okved else None
    new = [e for e in product_evidence(products, cache, directory, okved=okved) if (e.url, e.fact) not in have]
    if not new:
        return cp
    return cp.model_copy(update={"kind": "manufacturer", "evidence": [*cp.evidence, *new]})


def stats(directory: Path | None = None) -> dict:
    path = _products(directory)
    if path is None:
        return {}
    con = duckdb.connect()
    try:
        cur = con.execute(f"""
            SELECT count(*) AS products, count(DISTINCT inn) AS companies,
                   count(*) FILTER (WHERE NOT inn_valid) AS bad_inn,
                   count(DISTINCT inn) FILTER (WHERE left(inn, 2) = '78') AS companies_inn_78,
                   count(DISTINCT inn) FILTER (WHERE left(inn, 2) = '47') AS companies_inn_47,
                   count(DISTINCT okpd2) AS okpd2_codes, count(DISTINCT left(okpd2, 5)) AS okpd2_classes
            FROM read_parquet({_lit(path)})""")
        return dict(zip([d[0] for d in cur.description], cur.fetchone() or (), strict=True))
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ГИСП: выгрузка реестра российской промышленной продукции → parquet")
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="XLSX выгрузки → products.parquet + meta.json")
    b.add_argument("xlsx", type=Path)
    b.add_argument("--out", type=Path, default=None, help="каталог (по умолчанию data/external/gisp)")
    st = sub.add_parser("stats", help="сколько продукции и компаний в выгрузке")
    st.add_argument("--dir", type=Path, default=None, help="каталог (по умолчанию data/external/gisp)")
    args = ap.parse_args(argv)
    if args.command == "build":
        if not args.xlsx.is_file():
            print(f"нет {args.xlsx}: скачать — node scripts/gisp_download.mjs <файл.xlsx>", file=sys.stderr)
            return 2
        print(json.dumps(build(args.xlsx, args.out or gisp_dir()), ensure_ascii=False))
        return 0
    print(json.dumps({**stats(args.dir), **(meta(args.dir) or {})}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
