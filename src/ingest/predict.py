"""Test export -> recommendations per lot (#121): `make predict CSV_DIR=<folder>`.

    uv run python -m src.ingest.predict --csv-dir DIR [--out eval/predict/<name>.csv] [--top 10] [--offline]

One CSV (`;`, UTF-8 BOM for Excel): per lot the top-N suppliers from organizers' history
(block «История») and the companies from open sources (block «Новые»), score and every component
(value × weight, explanation). A lot without candidates gets one row with the reason — never a
silent gap. Idempotent: lots already in the output are skipped (resume after a crash); progress
goes to the log. Exit 2 on a broken export (message, no traceback). A text cell starting with
= + - @ (tab, CR) gets a leading «'» so Excel does not run it as a formula (docs/REVIEW.md F-08).
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from src.contracts import CounterpartyMatch
from src.ingest.testset import TestDataError, TestLot, load_testset

log = logging.getLogger("predict")
ROOT = Path(__file__).resolve().parents[2]
COLUMNS = ("lot_id", "reqnum", "query_text", "okpd2", "block", "rank", "inn", "name", "score", "components", "note")
BLOCKS = {"history": "История", "discovered": "Новые"}
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: object) -> object:
    """Excel reads «- Аренда…», «=…», «+7…» as a formula (F-08): prefix «'»; a number stays a number."""
    if not isinstance(value, str) or not value.startswith(FORMULA_START):
        return value
    try:
        float(value)
    except ValueError:
        return "'" + value
    return value


def _components(m: CounterpartyMatch) -> str:
    return " | ".join(f"{c.name}={c.value:.3f}×{c.weight:g}: {c.explanation}" for c in m.components)


def recommend(lot: TestLot, top: int, offline: bool | None = None) -> tuple[list[CounterpartyMatch], list[str]]:
    """History top-N + «Новые» for one lot; notes explain an empty block. Errors become notes."""
    from src.enrich import discover as discover_mod
    from src.match import search as search_mod

    notes = list(lot.notes)
    try:
        history = search_mod.search(lot.query, k=top)
    except FileNotFoundError:
        raise  # training data absent: nothing can be predicted, the caller stops
    except Exception as e:  # noqa: BLE001 — one bad lot must not stop the whole export
        log.exception("search, лот %s", lot.lot_id)
        notes.append(f"ошибка подбора по истории: {type(e).__name__}")
        history = []
    if not history:
        notes.append("в истории нет поставщиков по этому предмету")
    discovered: list[CounterpartyMatch] = []
    try:
        res = discover_mod.discover(lot.query, history, offline=offline)
        if res.history_checked:
            discovered = res.matches[:top]
        else:
            notes.append("«Новые» не установлены: нет сверки с данными организаторов")
        if not discovered:
            notes.extend(res.notes or ["открытые источники не дали новых компаний"])
    except Exception as e:  # noqa: BLE001 — an open source failing never breaks the history block
        log.exception("discover, лот %s", lot.lot_id)
        notes.append(f"блок «Новые» недоступен: {type(e).__name__}")
    return [*history, *discovered], notes


def _done(out: Path) -> set[str]:
    if not out.exists():
        return set()
    with out.open(encoding="utf-8-sig", newline="") as f:
        return {r["lot_id"] for r in csv.DictReader(f, delimiter=";")}


def run(csv_dir: Path, out: Path, top: int = 10, offline: bool | None = None) -> dict[str, int]:
    ts = load_testset(csv_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = _done(out)
    new_file = not out.exists()
    stats = {"lots": len(ts.lots), "skipped": 0, "with_history": 0, "with_discovered": 0, "empty": 0}
    with out.open("a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as f:
        csv_w = csv.writer(f, delimiter=";")

        def row(cells: list[object]) -> None:
            csv_w.writerow([csv_cell(c) for c in cells])

        if new_file:
            row(list(COLUMNS))
        for i, lot in enumerate(ts.lots, start=1):
            if csv_cell(lot.lot_id) in done:  # the file holds the escaped id
                stats["skipped"] += 1
                continue
            matches, notes = recommend(lot, top, offline)
            base = [lot.lot_id, lot.reqnum or "", lot.query.text, ",".join(lot.query.okpd2)]
            note = "; ".join(notes)
            if not matches:
                stats["empty"] += 1
                row([*base, "", "", "", "", "", "", note or "кандидатов нет"])
            ranks = {"history": 0, "discovered": 0}
            for m in matches:
                ranks[m.origin] += 1
                cp = m.counterparty
                row([*base, BLOCKS[m.origin], ranks[m.origin], cp.inn or "", cp.name, f"{m.score:.4f}",
                     _components(m), note])
            stats["with_history"] += ranks["history"] > 0
            stats["with_discovered"] += ranks["discovered"] > 0
            f.flush()  # a crash keeps every finished lot: the next run resumes after it
            log.info("predict %d/%d лот %s: история %d, новые %d", i, len(ts.lots), lot.lot_id,
                     ranks["history"], ranks["discovered"])
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--csv-dir", type=Path, required=True, help="папка тестовой выгрузки (Извещения*.csv, ТРУ*.csv)")
    ap.add_argument("--out", type=Path, help="CSV результата (по умолчанию eval/predict/<имя папки>.csv)")
    ap.add_argument("--top", type=int, default=10, help="кандидатов в каждом блоке")
    ap.add_argument("--offline", action="store_true", help="«Новые» только из кеша, без сети")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = args.out or ROOT / "eval" / "predict" / f"{args.csv_dir.resolve().name}.csv"
    try:
        stats = run(args.csv_dir, out, args.top, True if args.offline else None)
    except TestDataError as e:
        print(f"ошибка формата тестовой выгрузки: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"нет обучающих данных: {e}", file=sys.stderr)
        return 2
    print(f"{out}: лотов {stats['lots']} (пропущено как готовые {stats['skipped']}), с историей "
          f"{stats['with_history']}, с «Новыми» {stats['with_discovered']}, без кандидатов {stats['empty']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
