"""Manual labeling of the block «Новые из открытых источников» (T-14, research/EVAL.md B).

    uv run python -m src.eval.label_new prepare [--n 12] [--offline]
        -> eval/labels/new_suppliers_todo.csv: top-10 of the block per query, `label` empty (the human fills it)
    uv run python -m src.eval.label_new score [--exp N-new-p10] [--labels eval/labels/new_suppliers.csv]
        -> eval/results/<exp>.json: precision@10 of the block with a bootstrap interval

Queries: every `real` case, then dev cases of OKPD2 classes not yet taken (most frequent class first,
smallest query_id inside a class), only single-class cases with a full OKPD2 code (discovery is built
from OKPD2 only, D-041). The block comes through the same code as /api/search: discover() ->
strip_unverified(); every column of the table is an Evidence fact of the company, nothing else.
The tool never writes a label: labels are the human's (and, after agreement >= ~80%, the LLM judge's).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from src.contracts import CounterpartyMatch, Query
from src.enrich import discover as discover_mod
from src.enrich.evidence import strip_unverified
from src.eval import run as eval_run
from src.eval.metrics import bootstrap_ci
from src.eval.split import ROOT, EvalCase, load_cases
from src.sources import rmsp
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)
LABELS_DIR = ROOT / "eval" / "labels"
TODO_PATH = LABELS_DIR / "new_suppliers_todo.csv"
SHOWN_PATH = LABELS_DIR / "new_suppliers_shown.json"  # rows shown per selected query (empty blocks too)
LABELS_PATH = LABELS_DIR / "new_suppliers.csv"
TOP_K = 10
DEFAULT_N = 12
MIN_OVERLAP = 20  # human/LLM pairs needed before LLM labels count
MIN_AGREEMENT = 0.8
RETRIES = 3  # repeats of a query whose registry request timed out (answers already got stay in the cache)
UNCONFIRMED = "ИНН не подтверждён"
HUMAN, LLM = "человек", "llm"

TODO_COLUMNS = ["label", "labeled_by", "labeled_at", "query_id", "rank", "query_text", "okpd2", "inn", "name",
                "okved", "region", "msp", "source_url", "score"]


def _classes(case: EvalCase) -> list[str]:
    return discover_mod.okpd_classes(case.query.okpd2)


def select_cases(real: Sequence[EvalCase], dev: Sequence[EvalCase], n: int = DEFAULT_N) -> list[EvalCase]:
    """Deterministic: all `real`, then per not-yet-taken OKPD2 class (frequent first) its smallest query_id."""
    chosen = list(real)
    taken = {c for case in chosen for c in _classes(case)}
    usable = [c for c in dev if len(_classes(c)) == 1 and all(len(x.strip()) >= 5 for x in c.query.okpd2)]
    freq = Counter(_classes(c)[0] for c in usable)
    first: dict[str, EvalCase] = {}
    for case in sorted(usable, key=lambda c: c.query.query_id):
        first.setdefault(_classes(case)[0], case)
    for cls in sorted(first, key=lambda k: (-freq[k], k)):
        if len(chosen) >= n:
            break
        if cls not in taken:
            chosen.append(first[cls])
            taken.add(cls)
    return chosen[:n]


def _fact(cp, field: str) -> str:
    return "; ".join(e.fact for e in cp.evidence if e.field == field)


def table_rows(query: Query, matches: Sequence[CounterpartyMatch]) -> list[dict[str, str]]:
    """One row per company; every company fact is the `fact` of one of its Evidence items."""
    rows = []
    for rank, m in enumerate(matches[:TOP_K], start=1):
        cp = m.counterparty
        url = next((e.url for e in cp.evidence if e.url.startswith("http")), "")
        rows.append({
            "label": "", "labeled_by": "", "labeled_at": "",
            "query_id": query.query_id, "rank": str(rank), "query_text": query.text[:240],
            "okpd2": ", ".join(query.okpd2),
            "inn": cp.inn or UNCONFIRMED, "name": cp.name,
            "okved": _fact(cp, "okved"), "region": _fact(cp, "region"), "msp": _fact(cp, "msp"),
            "source_url": url, "score": f"{m.score:.3f}",
        })
    return rows


def run_block(query: Query, cache: SourceCache, offline: bool) -> tuple[list[CounterpartyMatch], list[str]]:
    """The block as the API gives it (discover + strip_unverified) and the log of what happened."""
    res = discover_mod.discover(query, (), cache=cache, offline=offline, budget_s=60.0)
    shown = [m.model_copy(update={"counterparty": strip_unverified(m.counterparty, cache.page_text)})
             for m in res.matches]
    log_lines = [*res.requests, *res.notes]
    if not res.history_checked:
        log_lines.append("сверка с данными организаторов не выполнена — «новыми» не называем, блок пуст")
        return [], log_lines
    return shown, log_lines


def prepare(n: int, offline: bool, out: Path = TODO_PATH) -> int:
    cases = select_cases(load_cases("real"), load_cases("dev"), n)
    cache = SourceCache()
    rows: list[dict[str, str]] = []
    empty: list[str] = []
    for case in cases:
        matches, lines = run_block(case.query, cache, offline)
        for _ in range(RETRIES * (not offline)):  # the registry is slow: wait out the pause, the cache keeps progress
            if not any("Timeout" in ln or "в сеть не ходили" in ln for ln in lines):
                break
            time.sleep(rmsp.DOWN_COOLDOWN_S + 1)
            matches, lines = run_block(case.query, cache, offline)
        got = table_rows(case.query, matches)
        print(f"{case.query.query_id} ОКПД2 {','.join(case.query.okpd2)}: {len(got)} строк")
        for ln in lines:
            print(f"    {ln}")
        if not got:
            empty.append(case.query.query_id)
        rows += got
    out.parent.mkdir(parents=True, exist_ok=True)
    shown = Counter(r["query_id"] for r in rows)
    SHOWN_PATH.write_text(json.dumps({c.query.query_id: shown[c.query.query_id] for c in cases},
                                     ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    with out.open("w", encoding="utf-8-sig", newline="") as f:  # BOM + «;»: opens in Russian Excel as is
        w = csv.DictWriter(f, fieldnames=TODO_COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    print(f"запросов {len(cases)}, строк к разметке {len(rows)}, пустой блок у {len(empty)}: {', '.join(empty) or '—'}")
    print(f"таблица: {out}")
    return 0


# ---- score ----------------------------------------------------------------

def read_labels(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    head = text.splitlines()[0] if text.strip() else ""
    delim = ";" if head.count(";") > head.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delim))


def _key(row: dict[str, str]) -> str:
    inn = (row.get("inn") or "").strip()
    return inn if inn and inn != UNCONFIRMED else (row.get("name") or "").strip()


def _kind(row: dict[str, str]) -> str | None:
    """llm / человек; an empty `labeled_by` is nobody's label (None) - it is not counted as a human one."""
    by = (row.get("labeled_by") or "").strip().lower()
    if not by:
        return None
    return LLM if by.startswith(LLM) else HUMAN


def resolve(rows: Sequence[dict[str, str]], use_llm: bool = True) -> dict[tuple[str, str], tuple[int, str]]:
    """(query_id, company) -> (label, kind). A human label wins over an LLM one; empty labels are skipped.

    Rows without `labeled_by` are skipped with a warning; two human labels that differ are logged (first wins).
    """
    out: dict[tuple[str, str], tuple[int, str]] = {}
    for row in rows:
        label = (row.get("label") or "").strip()
        if label not in ("0", "1") or not _key(row):
            continue
        k = ((row.get("query_id") or "").strip(), _key(row))
        kind = _kind(row)
        if kind is None:
            log.warning("строка без labeled_by пропущена: %s %s", *k)
            continue
        if kind == LLM and not use_llm:
            continue
        if k not in out or (kind == HUMAN and out[k][1] == LLM):
            out[k] = (int(label), kind)
        elif kind == HUMAN and out[k][1] == HUMAN and out[k][0] != int(label):
            log.warning("две разные метки человека на %s %s: берём первую (%d)", *k, out[k][0])
    return out


def agreement(rows: Sequence[dict[str, str]]) -> tuple[int, float | None]:
    """Human vs LLM on the overlap: (companies labeled by both, share of equal labels)."""
    by: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        label = (row.get("label") or "").strip()
        if label in ("0", "1") and _key(row) and _kind(row):
            by.setdefault(((row.get("query_id") or "").strip(), _key(row)), {})[str(_kind(row))] = int(label)
    both = [v for v in by.values() if HUMAN in v and LLM in v]
    return len(both), (sum(v[HUMAN] == v[LLM] for v in both) / len(both) if both else None)


def llm_trusted(rows: Sequence[dict[str, str]]) -> bool:
    """EVAL.md B: LLM labels count only after >= MIN_OVERLAP common pairs agree with the human at >= MIN_AGREEMENT."""
    n, share = agreement(rows)
    return n >= MIN_OVERLAP and share is not None and share >= MIN_AGREEMENT


def precision_body(rows: Sequence[dict[str, str]], shown: dict[str, int] | None = None,
                   allow_llm: bool = False) -> dict:
    """precision@10 per query = labeled 1 / labeled (the table has <= 10 rows per query), mean over queries.

    `shown` = rows per selected query from `prepare` (empty blocks included): n_selected, empty_blocks, shown.
    Human labels only by default (D-132: a third-party LLM judge is not part of the solution, its labels do not
    go to slides); `allow_llm` adds `llm:*` labels once they agree with the human (EVAL.md B).
    """
    has_llm = any(_kind(r) == LLM and (r.get("label") or "").strip() in ("0", "1") for r in rows)
    use_llm = allow_llm and llm_trusted(rows)
    if has_llm and not use_llm:
        why = (f"согласие с человеком не подтверждено (нужно >= {MIN_OVERLAP} пар, >= {MIN_AGREEMENT * 100:.0f}%)"
               if allow_llm else "только метки человека (D-132)")
        log.warning("метки LLM исключены из метрики: %s", why)
    resolved = resolve(rows, use_llm)
    per: dict[str, list[int]] = {}
    for (qid, _), (label, _) in resolved.items():
        per.setdefault(qid, []).append(label)
    per_query = [{"query_id": q, "precision@10": sum(v) / len(v), "labeled": len(v), "relevant": sum(v),
                  "shown": (shown or {}).get(q)}
                 for q, v in sorted(per.items())]
    values = [p["precision@10"] for p in per_query]
    lo, hi = bootstrap_ci(values)
    n_both, agree = agreement(rows)
    return {
        "n_queries": len(per_query),
        "metrics": {"precision@10": {"value": sum(values) / len(values) if values else 0.0, "ci95": [lo, hi]}},
        "labeled": len(resolved),
        "labeled_by": dict(Counter(kind for _, kind in resolved.values())),
        "agreement_human_llm": {"overlap": n_both, "share": agree, "llm_used": use_llm and has_llm},
        "n_selected": len(shown) if shown else None,
        "empty_blocks": sorted(q for q, n in (shown or {}).items() if n == 0),
        "per_query": per_query,
    }


def score(exp: str, labels: Path, allow_llm: bool = False) -> int:
    if not labels.exists():
        print(f"нет файла разметки {labels}: сначала заполните label в {TODO_PATH.name} и сохраните как {labels.name}")
        return 1
    shown = json.loads(SHOWN_PATH.read_text(encoding="utf-8")) if SHOWN_PATH.exists() else None
    body = precision_body(read_labels(labels), shown, allow_llm)
    if not body["n_queries"]:
        print("в файле нет ни одной метки 0/1 — считать нечего")
        return 1
    cfg = {"block": "discovered", "k": TOP_K, "labels": labels.name}
    rec = eval_run.result_record(exp, "new-suppliers", cfg, {"elapsed_s": 0.0, **body})
    out = ROOT / "eval" / "results" / f"{exp}.json"
    out.write_text(eval_run.dump_record(rec), encoding="utf-8")
    m = body["metrics"]["precision@10"]
    print(f"precision@10 = {m['value']:.3f} [{m['ci95'][0]:.3f}; {m['ci95'][1]:.3f}] "
          f"по {body['n_queries']} запросам, меток {body['labeled']} {body['labeled_by']} -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Разметка блока «Новые»: prepare / score")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="таблица для ручной разметки")
    p.add_argument("--n", type=int, default=DEFAULT_N)
    p.add_argument("--offline", action="store_true", help="только кеш, без сети")
    s = sub.add_parser("score", help="precision@10 по eval/labels/new_suppliers.csv")
    s.add_argument("--exp", default="N-new-p10")
    s.add_argument("--labels", type=Path, default=LABELS_PATH)
    s.add_argument("--with-llm", action="store_true",
                   help="add llm:* labels after >= 20 pairs agree >= 80 %% with the human; not for slides (D-132)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "prepare":
        return prepare(args.n, args.offline or discover_mod.offline_mode())
    return score(args.exp, args.labels, args.with_llm)


if __name__ == "__main__":
    sys.exit(main())
