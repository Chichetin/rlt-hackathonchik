"""Test export -> recommendations per lot (#121, #229): `make predict CSV_DIR=<folder>`.

    uv run python -m src.ingest.predict --csv-dir DIR [--out eval/predict/<name>.csv] [--top 10] [--offline]
        [--mode full|demo] [--no-rnp]

Mode `full` (default, #229 — the organizers judge the file, time is not limited): discovery without
the /api/search budget (every OKVED × region, several registry pages); every shown candidate of both
blocks is checked now (src/enrich/verify.py: ФНС card / ГИР БО status, RNP of the ЕИС) and only verified
ones are written; «Новые» by product_fit steps — 1.0 / 0.8 up to TOP, 0.6 only while strong < 5, lower
never. A lot whose OKPD2 code disagrees with its text (another section) or whose class is new to the
2024–2025 data is searched by the text, and the file says so. Next to the CSV — `<name>.report.csv`:
per lot, candidates from each source, how many were checked and shown, which sources were down.
Mode `demo` — the old behaviour (cache, the budget of /api/search).

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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from src.contracts import Counterparty, CounterpartyMatch, Query
from src.enrich.role import LABEL as ROLE_LABEL
from src.enrich.verify import Verdict
from src.ingest.testset import TestDataError, TestLot, load_testset

log = logging.getLogger("predict")
ROOT = Path(__file__).resolve().parents[2]
COLUMNS = ("lot_id", "reqnum", "query_text", "okpd2", "block", "rank", "inn", "name", "status", "rnp", "role",
           "score", "components", "check", "note")
REPORT_COLUMNS = ("lot_id", "mode", "okpd2", "text_class", "searched_by", "history_candidates", "history_checked",
                  "history_shown", "new_found", "new_checked", "new_shown", "requests", "unavailable", "notes")
RNP_TEXT = {True: "в РНП", False: "нет", None: ""}
FULL_PAGES = 5  # registry pages per (OKVED, region, type) in the full mode: up to 50 companies each
FULL_BUDGET_S = 600.0  # live discovery of one lot: generous, but a hanging source does not stop the run
CHECK_FACTOR = 4  # at most TOP × CHECK_FACTOR candidates of a block are checked
STRONG_FIT, WEAK_FIT, MIN_STRONG = 0.8, 0.6, 5  # decisions 19–20 (IR630): steps of product_fit in «Новые»
NOT_CONNECTED = ("пул МСП из выгрузки ФНС (#185)", "крупные компании вне МСП (#219)", "Yandex Search (#186)")
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


def _fit(m: CounterpartyMatch) -> float:
    return next((c.value for c in m.components if c.name == "product_fit"), 0.0)


@dataclass
class Checked:
    """A shown row: the match (counterparty after the check) and why it passed."""

    match: CounterpartyMatch
    check: str = ""


@dataclass
class LotReport:
    lot_id: str
    mode: str
    okpd2: str = ""
    text_class: str = ""
    searched_by: str = "код и текст"
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(
        ("history_candidates", "history_checked", "history_shown", "new_found", "new_checked", "new_shown"), 0))
    requests: list[str] = field(default_factory=list)
    unavailable: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def row(self) -> list[object]:
        return [self.lot_id, self.mode, self.okpd2, self.text_class, self.searched_by,
                *(self.counts[k] for k in REPORT_COLUMNS[5:11]),
                " / ".join(self.requests), ", ".join(sorted(self.unavailable)), "; ".join(self.notes)]


def recommend(lot: TestLot, top: int, offline: bool | None = None) -> tuple[list[CounterpartyMatch], list[str]]:
    """Demo mode: history top-N + «Новые» for one lot as /api/search builds them; errors become notes."""
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


# --- full mode (#229) ---------------------------------------------------------------------------------

_KNOWN: dict[tuple[str, int], frozenset[str]] = {}


def known_classes(processed: Path) -> frozenset[str] | None:
    """OKPD2 classes (XX.XX) present in the training items; None when the data is absent."""
    items = processed / "items.parquet"
    if not items.exists():
        return None
    key = (str(items), items.stat().st_mtime_ns)
    if key not in _KNOWN:
        con = duckdb.connect()
        try:
            rows = con.execute("SELECT DISTINCT okpd2_l4 FROM read_parquet(?) WHERE okpd2_l4 IS NOT NULL",
                               [str(items)]).fetchall()
        except duckdb.Error as e:
            log.warning("predict: items.parquet не прочитан: %s", e)
            return None
        finally:
            con.close()
        _KNOWN[key] = frozenset(r[0] for r in rows)
    return _KNOWN[key]


_LOT_URL = "data:items#"
VOTE_MATCHES = 10  # top text matches whose lots vote for the OKPD2 section
MISMATCH_SHARE = 0.1  # the code's section gets less of the text's votes → «код и текст расходятся» (EXP X-code-text)


def section_shares(history: Sequence[CounterpartyMatch], processed: Path) -> dict[str, float]:
    """OKPD2 sections (XX) of the lots behind the top text matches, weighted by the match score; shares sum
    to 1. A lot with several sections splits its vote. {} — nothing to vote with."""
    lots: dict[int, float] = {}
    for m in history[:VOTE_MATCHES]:
        for e in m.counterparty.evidence:
            if e.url.startswith(_LOT_URL) and e.url[len(_LOT_URL):].isdigit():
                lot = int(e.url[len(_LOT_URL):])
                lots[lot] = max(lots.get(lot, 0.0), m.score)
    items = processed / "items.parquet"
    if not lots or not items.exists():
        return {}
    con = duckdb.connect()
    try:
        rows = con.execute("SELECT DISTINCT lot_id, okpd2_l2 FROM read_parquet(?) "
                           "WHERE lot_id IN (SELECT unnest(?)) AND okpd2_l2 IS NOT NULL",
                           [str(items), list(lots)]).fetchall()
    except duckdb.Error as e:
        log.warning("predict: items.parquet не прочитан: %s", e)
        return {}
    finally:
        con.close()
    by_lot: dict[int, list[str]] = {}
    for lot, sec in rows:
        by_lot.setdefault(lot, []).append(sec)
    votes: dict[str, float] = {}
    for lot, secs in by_lot.items():
        for sec in secs:
            votes[sec] = votes.get(sec, 0.0) + max(lots[lot], 1e-9) / len(secs)
    total = sum(votes.values())
    return {k: v / total for k, v in votes.items()} if total > 0 else {}


def code_disagrees(codes: Sequence[str], shares: dict[str, float], text_class: str | None,
                   known: frozenset[str] | None) -> str | None:
    """Why the lot is searched by its text (decision 18), or None: the code is used as given.

    - the code's sections get less than MISMATCH_SHARE of the votes of the lots similar by text —
      «код и текст расходятся» (dev: 8 % of real lots, 96 % of lots with a code of another section;
      recall@10 of the winner unchanged on real lots, +3 п.п. on swapped codes — EXPERIMENTS X-code-text);
    - the code's class never occurs in the 2024–2025 data while the text points at a known class.
    A lot without a code, or a text that matches nothing, keeps its code."""
    from src.enrich.discover import okpd_classes

    classes = okpd_classes(codes)
    if not classes or not shares:
        return None
    shown = ", ".join(codes)
    own = sum(shares.get(sec, 0.0) for sec in {c[:2] for c in classes})
    top = max(shares, key=lambda k: (shares[k], k))
    by_text = f"класс {text_class}" if text_class else f"раздел {top}"
    if own < MISMATCH_SHARE:
        return (f"код и текст расходятся: ОКПД2 {shown} — {own:.0%} похожих по тексту закупок, по тексту — "
                f"{by_text} ({shares[top]:.0%}); кандидаты по тексту")
    if known is not None and text_class and not any(c in known for c in classes) and text_class in known:
        return (f"код ОКПД2 {shown} новый — класса нет в данных 2024–2025; по тексту — класс {text_class}; "
                "кандидаты по тексту")
    return None


def _pick(cands: Sequence[CounterpartyMatch], want: int, limit: int, verify_fn: Callable[[Counterparty], Verdict],
          rep: LotReport, block: str) -> list[Checked]:
    """The first `want` verified candidates, checking at most `limit`; the verdicts go to the report."""
    shown: list[Checked] = []
    for m in cands:
        if len(shown) >= want or rep.counts[f"{block}_checked"] >= limit:
            break
        rep.counts[f"{block}_checked"] += 1
        v = verify_fn(m.counterparty)
        rep.unavailable.update(v.down)
        if v.ok:
            shown.append(Checked(m.model_copy(update={"counterparty": v.counterparty}), v.why))
    return shown


def full_recommend(lot: TestLot, top: int, *, offline: bool = False, rnp: bool = True,
                   verify_fn: Callable[[Counterparty], Verdict] | None = None,
                   processed: Path | None = None) -> tuple[list[Checked], list[str], LotReport]:
    """Full mode for one lot: search by code or by text, discovery without budgets, synchronous check."""
    from src.enrich import discover as discover_mod
    from src.enrich import verify as verify_mod
    from src.match import search as search_mod
    from src.match.index import processed_dir
    from src.sources.cache import SourceCache

    processed = processed or processed_dir()
    if verify_fn is None:
        cache = SourceCache()

        def check(cp: Counterparty) -> Verdict:
            return verify_mod.verify(cp, cache, offline=offline, rnp=rnp)

        verify_fn = check

    rep = LotReport(lot.lot_id, "full", ",".join(lot.query.okpd2))
    notes = list(lot.notes)
    pool_k = top * CHECK_FACTOR
    query: Query = lot.query
    try:
        text_query = query.model_copy(update={"okpd2": []})
        by_text = search_mod.search(text_query, k=pool_k)
        text_class = discover_mod.predict_class(by_text, processed)
        rep.text_class = text_class or ""
        why = code_disagrees(query.okpd2, section_shares(by_text, processed), text_class, known_classes(processed))
        if why:
            notes.append(why)
            rep.searched_by = "текст (код не учтён)"
            query, history = text_query, by_text
        elif query.okpd2:
            history = search_mod.search(query, k=pool_k)
        else:
            history, rep.searched_by = by_text, "текст (кода нет)"
    except FileNotFoundError:
        raise
    except Exception as e:  # noqa: BLE001 — one bad lot must not stop the whole export
        log.exception("search, лот %s", lot.lot_id)
        notes.append(f"ошибка подбора по истории: {type(e).__name__}")
        history = []
    rep.counts["history_candidates"] = len(history)
    if not history:
        notes.append("в истории нет поставщиков по этому предмету")
    shown = _pick(history, top, pool_k, verify_fn, rep, "history")
    if history and not shown:
        notes.append(f"в истории {len(history)} кандидатов, ни один не прошёл проверку (статус, РНП)")

    try:
        res = discover_mod.discover(query, history, offline=offline, budget_s=FULL_BUDGET_S, max_requests=None,
                                    pages=FULL_PAGES, predicted_class=True)
        rep.requests = res.requests
        if any("не ответил" in r or "отказал" in r for r in res.requests):
            rep.unavailable.add("реестр МСП (поиск по ОКВЭД)")
        if not res.history_checked:
            notes.append("«Новые» не установлены: нет сверки с данными организаторов")
            pool: list[CounterpartyMatch] = []
        else:
            pool = res.pool
        rep.counts["new_found"] = len(pool)
        strong = [m for m in pool if _fit(m) >= STRONG_FIT]
        weak = [m for m in pool if WEAK_FIT <= _fit(m) < STRONG_FIT]
        new = _pick(strong, top, pool_k, verify_fn, rep, "new")
        if len(new) < MIN_STRONG:  # 0.6 only when strong ones are few; below 0.6 never
            new += _pick(weak, top - len(new), pool_k, verify_fn, rep, "new")
        if not new:
            notes.extend(res.notes or ["открытые источники не дали проверенных новых компаний"])
    except Exception as e:  # noqa: BLE001 — an open source failing never breaks the history block
        log.exception("discover, лот %s", lot.lot_id)
        notes.append(f"блок «Новые» недоступен: {type(e).__name__}")
        rep.unavailable.add("реестр МСП (поиск по ОКВЭД)")
        new = []
    rep.counts["history_shown"], rep.counts["new_shown"] = len(shown), len(new)
    rep.notes = notes
    return [*shown, *new], notes, rep


def _done(out: Path) -> set[str]:
    if not out.exists():
        return set()
    with out.open(encoding="utf-8-sig", newline="") as f:
        return {r["lot_id"] for r in csv.DictReader(f, delimiter=";")}


class _Csv:
    """Append-only `;` CSV with the header written once; every cell escaped (F-08)."""

    def __init__(self, path: Path, columns: Sequence[str]):
        new_file = not path.exists()
        self.f = path.open("a", encoding="utf-8-sig" if new_file else "utf-8", newline="")
        self.w = csv.writer(self.f, delimiter=";")
        if new_file:
            self.row(list(columns))

    def row(self, cells: list[object]) -> None:
        self.w.writerow([csv_cell(c) for c in cells])

    def close(self) -> None:
        self.f.close()


def report_path(out: Path) -> Path:
    return out.with_name(f"{out.stem}.report.csv")


def run(csv_dir: Path, out: Path, top: int = 10, offline: bool | None = None, mode: str = "demo",
        rnp: bool = True, verify_fn: Callable[[Counterparty], Verdict] | None = None) -> dict[str, int]:
    """Write the answer for every lot not yet in `out`. `mode="full"` — #229 (the CLI default)."""
    ts = load_testset(csv_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = _done(out)
    stats = {"lots": len(ts.lots), "skipped": 0, "with_history": 0, "with_discovered": 0, "empty": 0,
             "rows": 0, "rows_checked": 0}
    if mode == "full":
        log.info("predict full: источники, ещё не подключённые к этому режиму: %s", "; ".join(NOT_CONNECTED))
    w = _Csv(out, COLUMNS)
    rw = _Csv(report_path(out), REPORT_COLUMNS) if mode == "full" else None
    try:
        for i, lot in enumerate(ts.lots, start=1):
            if csv_cell(lot.lot_id) in done:  # the file holds the escaped id
                stats["skipped"] += 1
                continue
            if mode == "full":
                rows, notes, rep = full_recommend(lot, top, offline=bool(offline), rnp=rnp, verify_fn=verify_fn)
            else:
                matches, notes = recommend(lot, top, offline)
                rows, rep = [Checked(m) for m in matches], None
            base = [lot.lot_id, lot.reqnum or "", lot.query.text, ",".join(lot.query.okpd2)]
            note = "; ".join(notes)
            if not rows:
                stats["empty"] += 1
                w.row([*base, "", "", "", "", "", "", "", "", "", "", note or "кандидатов нет"])
            ranks = {"history": 0, "discovered": 0}
            for r in rows:
                m = r.match
                ranks[m.origin] += 1
                cp = m.counterparty
                w.row([*base, BLOCKS[m.origin], ranks[m.origin], cp.inn or "", cp.name, cp.status or "",
                       RNP_TEXT[cp.in_rnp], ROLE_LABEL[cp.kind] if cp.kind else "", f"{m.score:.4f}",
                       _components(m), r.check, note])
                stats["rows"] += 1
                stats["rows_checked"] += bool(r.check)
            stats["with_history"] += ranks["history"] > 0
            stats["with_discovered"] += ranks["discovered"] > 0
            w.f.flush()  # a crash keeps every finished lot: the next run resumes after it
            if rw is not None and rep is not None:
                rw.row(rep.row())
                rw.f.flush()
            log.info("predict %d/%d лот %s: история %d, новые %d", i, len(ts.lots), lot.lot_id,
                     ranks["history"], ranks["discovered"])
    finally:
        w.close()
        if rw is not None:
            rw.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--csv-dir", type=Path, required=True, help="папка тестовой выгрузки (Извещения*.csv, ТРУ*.csv)")
    ap.add_argument("--out", type=Path, help="CSV результата (по умолчанию eval/predict/<имя папки>.csv)")
    ap.add_argument("--top", type=int, default=10, help="кандидатов в каждом блоке")
    ap.add_argument("--offline", action="store_true", help="только кеш, без сети")
    ap.add_argument("--mode", choices=("full", "demo"), default="full",
                    help="full — без бюджетов, синхронная проверка, только проверенные (#229); demo — как /api/search")
    ap.add_argument("--no-rnp", action="store_true", help="full: не проверять РНП ЕИС (crawl-delay 60 с на запрос)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = args.out or ROOT / "eval" / "predict" / f"{args.csv_dir.resolve().name}.csv"
    try:
        stats = run(args.csv_dir, out, args.top, True if args.offline else None, mode=args.mode,
                    rnp=not args.no_rnp)
    except TestDataError as e:
        print(f"ошибка формата тестовой выгрузки: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"нет обучающих данных: {e}", file=sys.stderr)
        return 2
    print(f"{out}: лотов {stats['lots']} (пропущено как готовые {stats['skipped']}), с историей "
          f"{stats['with_history']}, с «Новыми» {stats['with_discovered']}, без кандидатов {stats['empty']}; "
          f"строк {stats['rows']}, из них проверенных {stats['rows_checked']}")
    if args.mode == "full":
        print(f"отчёт прогона: {report_path(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
