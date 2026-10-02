"""Organizers' test export (#121): a folder with CSV of the training format -> one Query per lot.

The expert (checkpoint 1, #120): «тестовая выгрузка, формат CSV и структура такие же, как у данных
для обучения». Required: `Извещения*.csv`; optional: `ТРУ*.csv` (OKPD2 of the lot positions);
`Поставщики*.csv` is ignored (the answer must not be read from it). `;`-separated, UTF-8 (BOM ok).

Format problems raise TestDataError with a human message (file, column, what to do) — never a
traceback. Per-lot problems (empty OKPD2, empty subject, bad price) are notes on the lot, not errors.
History of the test lots is NOT needed: search() and discover() run on the training data.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.contracts import Query
from src.ingest.notice import notice_query

log = logging.getLogger(__name__)

NOTICE_COLUMNS = ("publish_date", "procedure_id", "lot_id", "start_price", "reqnum", "procedure_name",
                  "subject", "is_smp", "customer_inn", "customer_kpp", "is_eshop_or_aisgz")
NOTICE_REQUIRED = ("lot_id", "subject")
ITEM_COLUMNS = ("lot_id", "product_name", "okpd2_code")
ITEM_REQUIRED = ("lot_id", "okpd2_code")


class TestDataError(ValueError):
    """The export cannot be read: the message says which file and what is wrong."""

    __test__ = False  # not a pytest class


@dataclass
class TestLot:
    lot_id: str
    reqnum: str | None
    query: Query
    notes: list[str] = field(default_factory=list)


@dataclass
class TestSet:
    lots: list[TestLot]
    warnings: list[str] = field(default_factory=list)

    def find(self, ident: str) -> TestLot | None:
        ident = ident.strip()  # lot_id first: a template's lot_id is a row number, its reqnum may be one too (#501)
        return (next((lt for lt in self.lots if ident == lt.lot_id), None)
                or next((lt for lt in self.lots if ident == lt.reqnum), None))


def _one(folder: Path, prefix: str, required: bool) -> Path | None:
    found = sorted(folder.glob(f"{prefix}*.csv"))
    if not found:
        if required:
            raise TestDataError(f"в {folder} нет файла {prefix}*.csv (нужен формат выгрузки организаторов)")
        return None
    if len(found) > 1:
        raise TestDataError(f"в {folder} несколько файлов {prefix}*.csv: {', '.join(p.name for p in found)} — "
                            "оставьте один")
    return found[0]


def _rows(path: Path, columns: tuple[str, ...], required: tuple[str, ...], warnings: list[str]) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise TestDataError(f"{path.name}: не UTF-8 (байт {e.start}); пересохраните файл в UTF-8 "
                            "(в Excel: «CSV UTF-8»)") from e
    except OSError as e:
        raise TestDataError(f"{path.name}: не читается ({e.strerror})") from e
    lines = text.splitlines()
    reader = csv.DictReader(lines, delimiter=";", quotechar='"')
    header = [h.strip() for h in (reader.fieldnames or [])]
    glued = [h for h in header if ";" in h]
    if glued:  # a quoted header cell hiding the delimiter: "reqnum;procedure_name" (#590)
        split = [p.strip().strip('"') for h in header for p in h.split(";") if p.strip().strip('"')]
        first = next(csv.reader(lines[1:2], delimiter=";", quotechar='"'), [])
        if len(first) == len(split):  # only when the data rows are as wide as the split header
            header = split
            warnings.append(f"{path.name}: в заголовке склеены колонки {', '.join(glued)} — разделены по «;»")
    if not header:
        raise TestDataError(f"{path.name}: пустой файл, нет строки заголовка")
    missing = [c for c in required if c not in header]
    if missing:
        hint = " (разделитель должен быть «;»)" if len(header) == 1 else ""
        raise TestDataError(f"{path.name}: нет колонок {', '.join(missing)}{hint}; "
                            f"ожидаются {';'.join(columns)}, в файле {';'.join(header)}")
    absent = [c for c in columns if c not in header and c not in required]
    extra = [c for c in header if c not in columns]
    if absent:
        warnings.append(f"{path.name}: нет колонок {', '.join(absent)} — поля останутся пустыми")
    if extra:
        warnings.append(f"{path.name}: лишние колонки {', '.join(extra)} — не используются")
    reader.fieldnames = header
    out = []
    for n, row in enumerate(reader, start=2):
        if None in row:  # more cells than header
            warnings.append(f"{path.name}, строка {n}: ячеек больше, чем колонок — лишние отброшены")
        out.append({k: (v or "").strip() for k, v in row.items() if k is not None})
    return out


def load_testset(folder: Path) -> TestSet:
    """Read the export; TestDataError on a broken format, TestSet with warnings otherwise."""
    if not folder.is_dir():
        raise TestDataError(f"папка {folder} не найдена")
    warnings: list[str] = []
    n_path = _one(folder, "Извещения", required=True)
    assert n_path is not None
    i_path = _one(folder, "ТРУ", required=False)
    if i_path is None:
        warnings.append("нет ТРУ*.csv: ОКПД2 лотов неизвестен, поиск только по тексту предмета")
    notices = _rows(n_path, NOTICE_COLUMNS, NOTICE_REQUIRED, warnings)
    items = _rows(i_path, ITEM_COLUMNS, ITEM_REQUIRED, warnings) if i_path else []
    codes: dict[str, list[str]] = {}
    for it in items:
        if it["lot_id"] and it["okpd2_code"]:
            lst = codes.setdefault(it["lot_id"], [])
            if it["okpd2_code"] not in lst:
                lst.append(it["okpd2_code"])
    lots: list[TestLot] = []
    seen: set[str] = set()
    for n, row in enumerate(notices, start=2):
        lot_id = row.get("lot_id", "")
        if not lot_id:
            warnings.append(f"{n_path.name}, строка {n}: пустой lot_id — строка пропущена")
            continue
        if lot_id in seen:
            continue  # the training ingest keeps the first row of a lot too
        seen.add(lot_id)
        notes: list[str] = []
        okpd2 = sorted(codes.get(lot_id, []))
        if not okpd2:
            notes.append("ОКПД2 не указан — подбор по тексту предмета, блок «Новые» может не строиться")
        subject, pname = row.get("subject", ""), row.get("procedure_name", "")
        if not subject and not pname:
            notes.append("пустой предмет закупки — текста для поиска нет")
        price = row.get("start_price", "").replace(",", ".").replace(" ", "")
        try:
            float(price) if price else None
        except ValueError:
            notes.append(f"НМЦК «{row.get('start_price')}» не число — не учитывается")
            price = ""
        q = notice_query(lot_id, subject or None, pname or None, price or None, row.get("customer_inn") or None,
                         row.get("is_eshop_or_aisgz") or None, okpd2=okpd2)
        lots.append(TestLot(lot_id=lot_id, reqnum=row.get("reqnum") or None, query=q, notes=notes))
    if not lots:
        raise TestDataError(f"{n_path.name}: ни одного лота с lot_id")
    for w in warnings:
        log.warning("тестовая выгрузка %s: %s", folder, w)
    return TestSet(lots=lots, warnings=warnings)
