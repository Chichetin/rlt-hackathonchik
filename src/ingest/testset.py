"""Organizers' test export (#121): a folder with CSV of the training format -> one Query per lot.

The expert (checkpoint 1, #120): «тестовая выгрузка, формат CSV и структура такие же, как у данных
для обучения». Required: `Извещения*.csv`; optional: `ТРУ*.csv` (OKPD2 of the lot positions);
`Поставщики*.csv` is ignored (the answer must not be read from it). `;`-separated, UTF-8 (BOM ok).

Format problems raise TestDataError with a human message (file, column, what to do) — never a
traceback. Per-lot problems (empty OKPD2, empty subject, bad price) are notes on the lot, not errors.
History of the test lots is NOT needed: search() and discover() run on the training data.

Defense export (#589, «Предзащита_Извещения_1..4.csv» + «Предзащита_Потоварка_1..4.csv»): files are found by a
part of the name (`Потоварка` is ТРУ), several sets are read together; a header cell with «;» inside quotes
(`"reqnum;procedure_name"`) is split. `publish_date` of the lot is the date of its query (Query.as_of): history
strictly before it, activity counted up to it.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
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
    procedure_id: str | None = None


@dataclass
class TestSet:
    lots: list[TestLot]
    warnings: list[str] = field(default_factory=list)

    def find(self, ident: str) -> TestLot | None:
        ident = ident.strip()  # lot_id first: a template's lot_id is a row number, its reqnum may be one too (#501)
        return (next((lt for lt in self.lots if ident == lt.lot_id), None)
                or next((lt for lt in self.lots if ident == lt.reqnum), None))

    def find_procedure(self, ident: str) -> TestLot | None:
        ident = ident.strip()
        return next((lt for lt in self.lots if lt.procedure_id and ident == lt.procedure_id), None)


NOTICE_NAMES = ("Извещения",)
ITEM_NAMES = ("ТРУ", "Потоварка")  # the defense export calls the positions file «Потоварка» (#589)


def _files(folder: Path, names: tuple[str, ...], required: bool) -> list[Path]:
    """CSV files whose name contains one of `names` (any prefix / suffix: «Предзащита_Извещения_1.csv»), sorted."""
    found = sorted({p for p in folder.glob("*.csv") if any(n.lower() in p.name.lower() for n in names)})
    if not found and required:
        raise TestDataError(f"в {folder} нет файла {names[0]}*.csv (нужен формат выгрузки организаторов)")
    return found


def _publish_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()[:10]) if value.strip() else None
    except ValueError:
        return None


def _rows(path: Path, columns: tuple[str, ...], required: tuple[str, ...], warnings: list[str]) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise TestDataError(f"{path.name}: не UTF-8 (байт {e.start}); пересохраните файл в UTF-8 "
                            "(в Excel: «CSV UTF-8»)") from e
    except OSError as e:
        raise TestDataError(f"{path.name}: не читается ({e.strerror})") from e
    lines = text.splitlines()
    if lines:  # «"reqnum;procedure_name"» — two columns in one quoted cell (#589): a header has no quotes inside
        lines[0] = lines[0].replace('"', "")
    reader = csv.DictReader(lines, delimiter=";", quotechar='"')
    header = [h.strip() for h in (reader.fieldnames or [])]
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
    n_paths = _files(folder, NOTICE_NAMES, required=True)
    i_paths = _files(folder, ITEM_NAMES, required=False)
    if not i_paths:
        warnings.append("нет ТРУ*.csv: ОКПД2 лотов неизвестен, поиск только по тексту предмета")
    notices: list[dict] = []
    for p in n_paths:
        notices += [{**r, "_file": p.name} for r in _rows(p, NOTICE_COLUMNS, NOTICE_REQUIRED, warnings)]
    items = [r for p in i_paths for r in _rows(p, ITEM_COLUMNS, ITEM_REQUIRED, warnings)]
    n_name = ", ".join(p.name for p in n_paths)
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
            warnings.append(f"{row['_file']}, строка {n}: пустой lot_id — строка пропущена")
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
        published = _publish_date(row.get("publish_date", ""))
        if published is not None:  # the lot is searched as of its own date (#589)
            q = q.model_copy(update={"as_of": published})
        lots.append(TestLot(lot_id=lot_id, reqnum=row.get("reqnum") or None, query=q, notes=notes,
                            procedure_id=row.get("procedure_id") or None))
    if not lots:
        raise TestDataError(f"{n_name}: ни одного лота с lot_id")
    for w in warnings:
        log.warning("тестовая выгрузка %s: %s", folder, w)
    return TestSet(lots=lots, warnings=warnings)
