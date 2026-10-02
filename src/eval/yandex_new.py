"""Live run of the Yandex fallback of the block «Новые» on thin categories (#521, D-161). Owner: SOURCES.

    set -a; . ./.env; set +a          # the key and YANDEX_MAX_PER_LOT / YANDEX_MAX_PER_RUN of a human (D-149)
    uv run python -m src.eval.yandex_new --exp Y-yandex-v1 [--n 30] [--offline]

Lots: dev cases (eval/cases/dev.jsonl) whose OKPD2 group XX.XX is a thin category of eval/results/T-thin-v1.json
(#122), one case per group in a seeded order, then a second round, up to `--n`. The query is the case's query
without `as_of`: «Новые» are new against the product's view of organizers' data, as /api/search sees them.

Per lot: search() → discover() with the full budget of `make predict` (every registry request, no 4 s deadline).
Pass 1 (live): Yandex is asked where the free channels left fewer than `min_strong` strong candidates; the INNs
whose ФНС card is missing are collected through `enqueue` and enriched (pb.nalog.ru, then the fallbacks of
enrich()). Pass 2 (`offline`): the same lots from the caches only — what a search of the lot shows now.
`--offline` runs only pass 2: reproduces the result from the caches without paying.
`--empty-from <exp> --min-strong N`: only the lots where the block «Новые» of that earlier run was empty, and Yandex
is asked below N strong candidates instead of `min_strong` of configs/yandex_discovery.yaml — what the fallback
adds where the free channels found no new company (the trigger counts the history block too, D-161).

A company is «from Yandex» when its product_fit explanation says «Найдена в Яндекс Поиске» (discover.yandex_matches);
it is never one the registry, the pool, a lead file or ГИСП already found. Writes eval/results/<exp>.json: totals,
per lot (trigger line of the funnel, rows by channel) and the Yandex companies for a manual check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from src.contracts import CounterpartyMatch, Query
from src.enrich import discover as discover_mod
from src.enrich import enrich as enrich_mod
from src.enrich import web_discover
from src.match import search as search_mod
from src.sources import yandex_search
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "eval" / "cases" / "dev.jsonl"
THIN = ROOT / "eval" / "results" / "T-thin-v1.json"
MARK = "Найдена в Яндекс Поиске"
BUDGET_S = 180.0  # per lot: the free registry requests, Yandex and the company sites


def thin_cases(n: int, cases: Path = CASES, thin: Path = THIN) -> list[dict]:
    """One case per thin group in a seeded order (hash of the query_id), round after round, up to n."""
    groups = [g["okpd2"] for g in json.loads(thin.read_text(encoding="utf-8"))["thin"]]
    by_group: dict[str, list[dict]] = {g: [] for g in groups}
    for line in cases.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        codes = case["query"].get("okpd2") or []
        group = next((c[:5] for c in codes if c[:5] in by_group), None)
        if group:
            by_group[group].append(case)
    for g in by_group:
        by_group[g].sort(key=lambda c: hashlib.sha256(c["query"]["query_id"].encode()).hexdigest())
    out: list[dict] = []
    depth = 0
    while len(out) < n and any(len(v) > depth for v in by_group.values()):
        out += [v[depth] for g, v in by_group.items() if len(v) > depth][: n - len(out)]
        depth += 1
    return out


def from_yandex(m: CounterpartyMatch) -> bool:
    return any(c.name == "product_fit" and MARK in c.explanation for c in m.components)


def site_of(m: CounterpartyMatch) -> str | None:
    return next((e.url for e in m.counterparty.evidence if e.source.startswith("Яндекс") or "сайт" in e.source), None)


def run_lot(case: dict, cache: SourceCache, *, offline: bool, enqueue=None) -> dict:  # type: ignore[no-untyped-def]
    q = Query(**{k: v for k, v in case["query"].items() if k != "as_of"})
    history = search_mod.search(q, k=20)
    code = search_mod.predicted_code()
    res = discover_mod.discover(q, history, cache=cache, offline=offline, budget_s=BUDGET_S, max_requests=None,
                                predicted_code=code, enqueue=enqueue)
    rows = res.matches
    ya = [m for m in rows if from_yandex(m)]
    return {
        "query_id": q.query_id, "text": q.text[:160], "okpd2": q.okpd2, "classes": res.okpd_classes,
        "history": len(history), "new_total": len(rows), "new_from_yandex": len(ya),
        "yandex_line": next((line for line in res.leads if line.startswith("Яндекс")), None),
        "companies": [{"inn": m.counterparty.inn, "name": m.counterparty.name, "kind": m.counterparty.kind,
                       "site": site_of(m), "score": round(m.score, 3),
                       "product_fit": next((c.explanation for c in m.components if c.name == "product_fit"), "")}
                      for m in ya],
    }


def commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Живой прогон запасного пути Яндекс блока «Новые» (#521, D-161)")
    ap.add_argument("--exp", default="Y-yandex-v1")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--offline", action="store_true", help="только кеш: воспроизвести результат без оплаты")
    ap.add_argument("--empty-from", help="только лоты, где в этом прогоне блок «Новые» был пуст")
    ap.add_argument("--min-strong", type=int, help="порог триггера вместо min_strong конфигурации")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    cases = thin_cases(args.n)
    if args.empty_from:
        prev = json.loads((ROOT / "eval" / "results" / f"{args.empty_from}.json").read_text(encoding="utf-8"))
        empty = {lot["query_id"] for lot in prev["lots"] if lot["new_total"] == 0}
        cases = [c for c in cases if c["query"]["query_id"] in empty]
    if args.min_strong is not None:
        base = web_discover.load_config()
        web_discover.load_config = lambda path=web_discover.CONFIG: {**base, "min_strong": args.min_strong}
    cache = SourceCache()
    why_off = yandex_search.disabled_reason()
    ycache = SourceCache(yandex_search.cache_path())
    spent_before = yandex_search.spent_today(ycache)
    enriched: dict[str, str] = {}
    started = time.time()
    if not args.offline:
        if why_off:
            print(f"Яндекс выключен: {why_off}", file=sys.stderr)
            return 2
        queued: list[str] = []
        for i, case in enumerate(cases, 1):
            run_lot(case, cache, offline=False, enqueue=queued.append)
            print(f"проход 1: {i}/{len(cases)} {case['query']['query_id']}, к обогащению {len(set(queued))}",
                  file=sys.stderr)
        for inn in dict.fromkeys(queued):
            try:
                cp = enrich_mod.enrich(inn, cache, offline=False)
                enriched[inn] = cp.status or "карточка без статуса"
            except Exception as e:  # noqa: BLE001 — one card failing does not stop the run
                enriched[inn] = f"ошибка: {type(e).__name__}"
    lots = [run_lot(case, cache, offline=True) for case in cases]
    spent_after = yandex_search.spent_today(ycache)

    triggered = [lot for lot in lots if lot["yandex_line"] and "запросов" in lot["yandex_line"]]
    companies = [dict(c, query_id=lot["query_id"], lot_text=lot["text"]) for lot in lots for c in lot["companies"]]
    out = {
        "exp_id": args.exp, "kind": "yandex_new", "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": commit(), "offline_only": args.offline,
        "config": {"n": args.n, "budget_s": BUDGET_S, "empty_from": args.empty_from, "min_strong": args.min_strong,
                   "cases": CASES.relative_to(ROOT).as_posix(),
                   "thin": THIN.relative_to(ROOT).as_posix(), "yandex_disabled": why_off},
        "metrics": {
            "lots": len(lots), "lots_yandex_asked": len(triggered),
            "paid_requests_today_delta": spent_after - spent_before,
            "new_total": sum(lot["new_total"] for lot in lots),
            "new_from_yandex": sum(lot["new_from_yandex"] for lot in lots),
            "lots_with_yandex_company": sum(1 for lot in lots if lot["new_from_yandex"]),
            "enriched_cards": len(enriched),
            "elapsed_s": round(time.time() - started, 1),
        },
        "enriched": enriched, "lots": lots, "companies": companies,
    }
    path = ROOT / "eval" / "results" / f"{args.exp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out["metrics"], ensure_ascii=False), file=sys.stderr)
    print(f"-> {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
