"""Block «Новые»: product_fit v0 (off) vs product_fit_new (on), #77 increment 1.

    uv run python -m src.eval.new_fit_compare prepare [--n 12] [--offline]
        -> eval/labels/new_fit_off.csv, eval/labels/new_fit_on.csv: top-10 of the block per query in each mode
           (the format of src/eval/label_new.py, `label` empty; the «on» table adds product_fit_new and why);
           eval/labels/new_fit_todo.csv: rows of «on» not yet in new_suppliers_todo.csv — to label as well;
           eval/labels/new_fit_compare.json: per query overlap@10, entered / left, rank moves
    uv run python -m src.eval.new_fit_compare score [--exp N-new-fit] [--labels F ...]
        -> eval/results/<exp>-off.json, <exp>-on.json: precision@10 of each table on the same labels
           (label_new.precision_body; unlabeled rows are reported, not counted)

The queries and the block are those of label_new (select_cases, run_block: discover -> strip_unverified),
so labels of #21 (eval/labels/new_suppliers.csv) are reused by (query_id, INN). No label is written here.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from src.contracts import CounterpartyMatch, Query
from src.enrich import discover as discover_mod
from src.enrich.evidence import strip_unverified
from src.eval import label_new
from src.eval import run as eval_run
from src.eval.split import ROOT, load_cases
from src.match import new_fit
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)
LABELS_DIR = label_new.LABELS_DIR
MODES = ("off", "on")
COMPARE_PATH = LABELS_DIR / "new_fit_compare.json"
TODO_PATH = LABELS_DIR / "new_fit_todo.csv"
FIT_COLUMNS = ["product_fit_new", "product_fit_new_why"]


def table_path(mode: str) -> Path:
    return LABELS_DIR / f"new_fit_{mode}.csv"


def block(query: Query, cache: SourceCache, offline: bool, mode: str
          ) -> tuple[list[CounterpartyMatch], discover_mod.Discovery]:
    """The block as /api/search shows it, with product_fit_new in `mode` (off | on)."""
    res = discover_mod.discover(query, (), cache=cache, offline=offline, budget_s=60.0, fit_mode=mode)
    if not res.history_checked:  # as label_new.run_block: not «новые» without the organizers' data check
        return [], res
    shown = [m.model_copy(update={"counterparty": strip_unverified(m.counterparty, cache.page_text)})
             for m in res.matches]
    return shown, res


def compare(off: Sequence[dict[str, str]], on: Sequence[dict[str, str]]) -> dict:
    """Per query: overlap of the two top-10 lists, who entered / left, rank moves of the common ones."""
    def by_query(rows: Iterable[dict[str, str]]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in sorted(rows, key=lambda r: (r["query_id"], int(r["rank"]))):
            out.setdefault(r["query_id"], []).append(label_new._key(r))
        return out

    a, b = by_query(off), by_query(on)
    per = []
    for q in sorted(set(a) | set(b)):
        x, y = a.get(q, []), b.get(q, [])
        common = [k for k in x if k in y]
        per.append({
            "query_id": q, "off": len(x), "on": len(y), "overlap": len(common),
            "entered": [k for k in y if k not in x], "left": [k for k in x if k not in y],
            "moved": {k: [x.index(k) + 1, y.index(k) + 1] for k in common if x.index(k) != y.index(k)},
        })
    n = len(per)
    return {
        "n_queries": n,
        "mean_overlap@10": sum(p["overlap"] for p in per) / n if n else None,
        "queries_changed_set": sum(bool(p["entered"] or p["left"]) for p in per),
        "queries_changed_order": sum(bool(p["moved"]) for p in per),
        "per_query": per,
    }


def _write(path: Path, rows: Sequence[dict[str, str]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # as label_new: opens in Russian Excel
        w = csv.DictWriter(f, fieldnames=list(columns), delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def prepare(n: int, offline: bool) -> int:
    cases = label_new.select_cases(load_cases("real"), load_cases("dev"), n)
    cache = SourceCache()
    tables: dict[str, list[dict[str, str]]] = {m: [] for m in MODES}
    for case in cases:
        for mode in MODES:
            matches, res = block(case.query, cache, offline, mode)
            rows = label_new.table_rows(case.query, matches)
            for row, m in zip(rows, matches, strict=False):
                fit = res.fit_new.get(m.counterparty.inn or "")
                row["product_fit_new"] = f"{fit.value:.3f}" if fit else ""
                row["product_fit_new_why"] = fit.explanation() if fit else ""
            tables[mode] += rows
            prof = res.profile
            print(f"{case.query.query_id} {mode}: {len(rows)} строк"
                  + (f", профиль ОКПД2 {prof.code}: {', '.join(prof.terms)}; общие с запросом: "
                     f"{', '.join(new_fit.query_overlap(case.query.text, prof)) or 'нет — код и текст расходятся?'}"
                     if prof else ""))
    for mode in MODES:
        _write(table_path(mode), tables[mode], [*label_new.TODO_COLUMNS, *FIT_COLUMNS])
    seen = set()
    if label_new.TODO_PATH.exists():
        seen = {(r["query_id"], label_new._key(r)) for r in label_new.read_labels(label_new.TODO_PATH)}
    todo = [r for r in tables["on"] if (r["query_id"], label_new._key(r)) not in seen]
    _write(TODO_PATH, todo, label_new.TODO_COLUMNS)
    diff = compare(tables["off"], tables["on"])
    COMPARE_PATH.write_text(json.dumps(diff, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"запросов {diff['n_queries']}, среднее пересечение top-10 {diff['mean_overlap@10']}, "
          f"состав изменился в {diff['queries_changed_set']}, порядок — в {diff['queries_changed_order']}; "
          f"новых строк к разметке: {len(todo)} -> {TODO_PATH}")
    return 0


def labeled_rows(table: Sequence[dict[str, str]], labels: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """Rows of a mode table with every label given to the same (query_id, company); unlabeled — label empty."""
    by: dict[tuple[str, str], list[dict[str, str]]] = {}
    for r in labels:
        by.setdefault(((r.get("query_id") or "").strip(), label_new._key(r)), []).append(r)
    out = []
    for row in table:
        got = by.get((row["query_id"], label_new._key(row)), [])
        out += [{**row, "label": g.get("label", ""), "labeled_by": g.get("labeled_by", "")} for g in got] or [row]
    return out


def score(exp: str, label_files: Sequence[Path]) -> int:
    labels = [r for p in label_files if p.exists() for r in label_new.read_labels(p)]
    if not labels:
        print(f"нет меток: {', '.join(str(p) for p in label_files)} — метрика не считается")
        return 1
    for mode in MODES:
        path = table_path(mode)
        if not path.exists():
            print(f"нет {path}: сначала prepare")
            return 1
    for mode in MODES:
        table = label_new.read_labels(table_path(mode))
        rows = labeled_rows(table, labels)
        unlabeled = sum(1 for r in rows if (r.get("label") or "").strip() not in ("0", "1"))
        shown: dict[str, int] = {}
        for r in table:
            shown[r["query_id"]] = shown.get(r["query_id"], 0) + 1
        body = label_new.precision_body(rows, shown)
        if not body["n_queries"]:
            print(f"{mode}: ни одной метки на строках таблицы")
            continue
        cfg = {"block": "discovered", "k": label_new.TOP_K, "new_fit": mode,
               "labels": [p.name for p in label_files], "unlabeled_rows": unlabeled}
        rec = eval_run.result_record(f"{exp}-{mode}", "new-suppliers", cfg, {"elapsed_s": 0.0, **body})
        out = ROOT / "eval" / "results" / f"{exp}-{mode}.json"
        out.write_text(eval_run.dump_record(rec), encoding="utf-8")
        m = body["metrics"]["precision@10"]
        print(f"{mode}: precision@10 = {m['value']:.3f} [{m['ci95'][0]:.3f}; {m['ci95'][1]:.3f}] по "
              f"{body['n_queries']} запросам; без метки строк: {unlabeled} -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Блок «Новые»: product_fit v0 против product_fit_new (#77)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="две таблицы top-10 и сравнение")
    p.add_argument("--n", type=int, default=label_new.DEFAULT_N)
    p.add_argument("--offline", action="store_true", help="только кеш, без сети")
    s = sub.add_parser("score", help="precision@10 обеих таблиц по одним меткам")
    s.add_argument("--exp", default="N-new-fit")
    s.add_argument("--labels", type=Path, action="append",
                   help=f"файл меток (можно несколько); по умолчанию {label_new.LABELS_PATH.name} и "
                        f"{TODO_PATH.name}")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "prepare":
        return prepare(args.n, args.offline or discover_mod.offline_mode())
    return score(args.exp, args.labels or [label_new.LABELS_PATH, TODO_PATH])


if __name__ == "__main__":
    sys.exit(main())
