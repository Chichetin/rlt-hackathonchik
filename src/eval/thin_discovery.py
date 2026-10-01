"""Discovery on thin categories: «было → стало» per OKPD2 group (#122, D-078).

    uv run python -m src.eval.thin_discovery T-disc-v1 --thin T-thin-v1 --top 5 --live --max-live 40

For each of the top-N thin groups of eval/results/<thin>.json runs src/enrich/discover.py (the same code
as the «Новые из открытых источников» block of /api/search: МСП registry, СПб and the neighbour region,
class OKVED and the wholesale code) with the query «ОКПД2 <group>» and counts:
- suppliers_before: distinct suppliers of the group in organizers' data (thin result file);
- new_found: companies of the registry that are NOT in organizers' data (any channel, any year);
- evidence_passed: of them those whose every shown fact passes src/enrich/evidence.py (INN checksum,
  the value literally on the cached page, a source URL);
- known / inactive: found in the registry but already in the data / excluded from the registry.
Without --live only the cache is read. Live requests go through SourceCache with the pacing of
src/sources/rmsp.py (>= 1 s); a refusal (403/429/captcha) stops the whole run, nothing is bypassed.
`--max-live` is a hard cap of live requests of ONE run: a group that could exceed it is not started.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.contracts import Query
from src.enrich import discover as D
from src.enrich.evidence import check_counterparty, strip_unverified
from src.eval.run import RESULTS, git
from src.eval.split import ROOT
from src.sources.cache import SourceCache

OFFLINE_MARK = ("кеш,", "нет в кеше")  # not a live request


@dataclass
class GroupResult:
    okpd2: str
    suppliers_before: int
    lots: int
    requests: list[str] = field(default_factory=list)
    live_requests: int = 0
    new_found: int = 0
    evidence_passed: int = 0
    known: int = 0
    inactive: int = 0
    history_checked: bool = False
    new_inns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@contextmanager
def uncapped() -> Iterator[None]:
    """The product block shows at most MAX_SHOWN companies; the statistic counts every new one.
    Only the cap of this process is lifted for the duration of the call; nothing else changes."""
    old = D.MAX_SHOWN
    D.MAX_SHOWN = 10_000
    try:
        yield
    finally:
        D.MAX_SHOWN = old


def live_attempts(lines: list[str]) -> int:
    """Requests that left the machine: every line of Discovery.requests except cache hits / skipped ones
    (a refused or unanswered request is an attempt too)."""
    return sum(1 for ln in lines if not any(m in ln for m in OFFLINE_MARK))


def run_group(g: dict, cache: SourceCache, *, offline: bool, budget_s: float = 60.0,
              processed: Path | None = None,
              discover_fn: Callable[..., D.Discovery] = D.discover) -> GroupResult:
    query = Query(query_id=f"thin-{g['okpd2']}", text=f"ОКПД2 {g['okpd2']}", okpd2=[g["okpd2"]])
    with uncapped():
        res = discover_fn(query, cache=cache, offline=offline, budget_s=budget_s, processed=processed)
    out = GroupResult(okpd2=g["okpd2"], suppliers_before=g["suppliers"], lots=g["lots"], requests=res.requests,
                      live_requests=0 if offline else live_attempts(res.requests), known=res.known,
                      inactive=res.inactive, history_checked=res.history_checked, notes=res.notes)
    if not res.history_checked:
        return out  # «new» is not established without organizers' data
    out.new_found = len(res.matches)
    for m in res.matches:
        shown = strip_unverified(m.counterparty, cache.page_text)
        ok = (not check_counterparty(m.counterparty, cache.page_text) and shown.inn is not None
              and any(e.url.startswith("http") for e in shown.evidence))
        if ok:
            out.evidence_passed += 1
            out.new_inns.append(shown.inn or "")
    return out


def run(groups: list[dict], cache: SourceCache, *, offline: bool, max_live: int,
        discover_fn: Callable[..., D.Discovery] = D.discover, processed: Path | None = None,
        per_group_cap: int = D.MAX_REQUESTS) -> list[GroupResult]:
    results: list[GroupResult] = []
    used = 0
    for g in groups:
        if not offline and used + per_group_cap > max_live:
            print(f"! {g['okpd2']}: до лимита {max_live} живых запросов осталось < {per_group_cap}; "
                  "группа не запускалась", file=sys.stderr)
            break
        r = run_group(g, cache, offline=offline, processed=processed, discover_fn=discover_fn)
        results.append(r)
        used += r.live_requests
        if any("отказал" in ln for ln in r.requests):
            print(f"! {g['okpd2']}: источник отказал — прогон остановлен, обхода нет", file=sys.stderr)
            break
    return results


def build_record(exp_id: str, thin_id: str, results: list[GroupResult], *, offline: bool, max_live: int) -> dict:
    checked = [r for r in results if r.history_checked]
    return {
        "exp_id": exp_id,
        "kind": "thin-discovery",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--", "src", "configs")),
        "config": {"thin_result": thin_id,
                   "block_cap_lifted": f"в блоке /api/search не более {D.MAX_SHOWN}, здесь считаются все",
                   "groups": [r.okpd2 for r in results], "offline": offline,
                   "max_live_requests": max_live, "source": "rmsp.nalog.ru (D-036, D-037)",
                   "regions": "СПб и соседний регион запроса (ЛО), только ЮЛ"},
        "metrics": {
            "groups": len(results),
            "groups_with_history_check": len(checked),
            "live_requests": sum(r.live_requests for r in results),
            "suppliers_before": sum(r.suppliers_before for r in checked),
            "new_found": sum(r.new_found for r in checked),
            "evidence_passed": sum(r.evidence_passed for r in checked),  # company-group pairs
            "distinct_new_companies": len({inn for r in checked for inn in r.new_inns}),  # one company counted once
            "known": sum(r.known for r in checked),
            "inactive": sum(r.inactive for r in checked),
            "groups_with_new": sum(1 for r in checked if r.evidence_passed > 0),
        },
        "groups": [r.__dict__ for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discovery по тонким группам ОКПД2: было -> стало")
    ap.add_argument("exp")
    ap.add_argument("--thin", required=True, help="id результата src.eval.thin, например T-thin-v1")
    ap.add_argument("--skip", type=int, default=0, help="пропустить первые N тонких групп (уже прогнаны)")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--live", action="store_true", help="ходить в сеть (иначе только кеш)")
    ap.add_argument("--max-live", type=int, default=40, help="жёсткий лимит живых запросов за прогон")
    args = ap.parse_args(argv)
    thin_path = RESULTS / f"{args.thin}.json"
    if not thin_path.exists():
        print(f"нет {thin_path.relative_to(ROOT)}: сначала python -m src.eval.thin {args.thin}", file=sys.stderr)
        return 2
    groups = json.loads(thin_path.read_text(encoding="utf-8"))["thin"][args.skip:args.skip + args.top]
    cache = SourceCache()
    results = run(groups, cache, offline=not args.live, max_live=args.max_live)
    rec = build_record(args.exp, args.thin, results, offline=not args.live, max_live=args.max_live)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("ОКПД2  поставщиков в данных (ЭМ) | новых найдено | Evidence прошло | уже в данных | исключены "
          "| живых запросов")
    for r in results:
        print(f"{r.okpd2:6} {r.suppliers_before:10} {r.new_found:18} {r.evidence_passed:14} {r.known:14} "
              f"{r.inactive:10} {r.live_requests:8}"
              + ("" if r.history_checked else "  (сверка с данными не выполнена)"))
    m = rec["metrics"]
    print(f"ИТОГО: групп {m['groups']}, новых с Evidence {m['evidence_passed']} из {m['new_found']} найденных, "
          f"живых запросов {m['live_requests']}")
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
