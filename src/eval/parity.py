"""Bit-for-bit parity of the rankings of two eval runs: the safety net of a refactoring (#286).

    make parity A=parity-base-<sha> B=parity-new-<sha>
    uv run python -m src.eval.parity A B [-n 5]

Reads eval/results/<id>.json; for every query id compares `per_query[].ranked` — the INNs in the order the
user sees them — exactly: composition and order. `src.eval.compare` says whether the metrics moved beyond
noise; this says whether the issue is the same list for the same case (research/EVAL.md, «Чистый рефакторинг»).

Exit code: 0 — all rankings are identical; 1 — at least one differs; 2 — the comparison is impossible (a result
is missing, other case sets, no `ranked`, no cases) and is never reported as «0 differences».

Not compared: scores and components (they are not in `ranked`). A difference between two runs of the very
same commit is non-determinism of the search; it is reported as such, not masked.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

from src.eval.compare import require_same_cases
from src.eval.run import RANKED_LIMIT, RESULTS

SHOWN = 5  # differences printed in full; the rest only counted
LISTED = 3  # INNs listed per side of a composition difference
RUN_FIELDS = ("part", "store", "tokenizer_version", "vectors", "stub")  # besides `config`: other input, not other code
_END = object()  # the end of a shorter list


class InputError(ValueError):
    """The two results cannot be compared (exit code 2)."""


@dataclass(frozen=True)
class Diff:
    query_id: str
    kind: str  # "composition": other INNs (or another number of them) | "order": the same INNs, another order
    rank: int  # 1-based rank of the first difference
    a: str | None  # INN of A at that rank (None: the list ended)
    b: str | None
    only_a: tuple[str, ...]  # INNs A has more of than B
    only_b: tuple[str, ...]


def load(results: Path, exp_id: str) -> dict:
    path = results / f"{exp_id}.json"
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise InputError(f"{exp_id}: нет {path.name} в {results} — прогон: make eval EXP={exp_id}") from None
    except json.JSONDecodeError as e:
        raise InputError(f"{path}: не JSON ({e})") from None
    if not isinstance(rec, dict):
        raise InputError(f"{path}: не результат make eval")
    rec.setdefault("exp_id", exp_id)
    return rec


def rankings(rec: dict, label: str) -> dict[str, list[str]]:
    """{query_id: ranked} in the file order; InputError for what makes a comparison meaningless."""
    rows = rec.get("per_query")
    if not isinstance(rows, list) or not all(isinstance(q, dict) and "query_id" in q for q in rows):
        raise InputError(f"{label}: нет per_query по кейсам — это не результат make eval")
    if not rows:
        raise InputError(f"{label}: нет кейсов — сравнивать нечего")
    missing = sum(not isinstance(q.get("ranked"), list) for q in rows)
    if missing:
        raise InputError(f"{label}: у {missing} из {len(rows)} кейсов нет ranked (часть больше "
                         f"RANKED_LIMIT={RANKED_LIMIT} или результат старого формата) — выдачи не сравнить; "
                         "прогон: make eval EXP=<id>")
    repeated = sorted(qid for qid, n in Counter(q["query_id"] for q in rows).items() if n > 1)
    if repeated:
        raise InputError(f"{label}: query_id повторяется: {', '.join(map(str, repeated[:3]))} "
                         "— парное сравнение невозможно")
    return {q["query_id"]: q["ranked"] for q in rows}


def diff_case(query_id: str, a: list[str], b: list[str]) -> Diff | None:
    if a == b:
        return None
    rank = next(i for i, (x, y) in enumerate(zip_longest(a, b, fillvalue=_END)) if x != y)
    ca, cb = Counter(a), Counter(b)
    return Diff(query_id=query_id, kind="order" if ca == cb else "composition", rank=rank + 1,
                a=a[rank] if rank < len(a) else None, b=b[rank] if rank < len(b) else None,
                only_a=tuple((ca - cb).elements()), only_b=tuple((cb - ca).elements()))


def diff_rankings(a: dict[str, list[str]], b: dict[str, list[str]]) -> list[Diff]:
    """Differences over the common query ids, in the case order of `a`."""
    return [d for qid, ranked in a.items() if (d := diff_case(qid, ranked, b[qid])) is not None]


def warnings(a: dict, b: dict) -> list[str]:
    """Run-level differences that make «same code, same input» doubtful: not errors — a refactoring that moved
    a config key on purpose still wants the comparison. config_sha256 is not compared: it contains the tree of the
    method code, so it differs for every refactoring by construction."""
    ca: dict = a["config"] if isinstance(a.get("config"), dict) else {}
    cb: dict = b["config"] if isinstance(b.get("config"), dict) else {}
    out = []
    changed = sorted(k for k in {*ca, *cb} if ca.get(k) != cb.get(k))
    if changed:
        out.append(f"config различается по ключам {changed} — для чистого рефакторинга конфигурация должна совпадать")
    out += [f"{f}: A={a.get(f)!r}, B={b.get(f)!r}" for f in RUN_FIELDS if a.get(f) != b.get(f)]
    out += [f"прогон {name} на грязном дереве (dirty): результат не соответствует его коммиту"
            for name, r in (("A", a), ("B", b)) if r.get("dirty")]
    da, db = a.get("data"), b.get("data")  # the ingest snapshot, results since D-144
    if isinstance(da, dict) and isinstance(db, dict):
        out += [f"data.{f}: A={da.get(f)!r}, B={db.get(f)!r} — другой снимок данных (#421)"
                for f in ("built_at", "rows") if da.get(f) != db.get(f)]
    return out


def _inns(inns: tuple[str, ...]) -> str:
    return ", ".join(inns[:LISTED]) + (f" … (+{len(inns) - LISTED})" if len(inns) > LISTED else "")


def format_diff(d: Diff) -> str:
    kind = "состав" if d.kind == "composition" else "порядок"
    head = f"  {d.query_id}  {kind:<7} ранг {d.rank}: A={d.a or '—'} B={d.b or '—'}"
    if d.kind == "order":
        return head
    return f"{head} | только в A: {_inns(d.only_a) or '—'} | только в B: {_inns(d.only_b) or '—'}"


def _title(rec: dict, label: str) -> str:
    return f"{label}={rec['exp_id']} (commit {str(rec.get('commit') or '?')[:8]})"


def main(argv: list[str] | None = None, results: Path = RESULTS) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0],
                                 epilog="exit code: 0 — rankings identical, 1 — differ, 2 — cannot be compared")
    ap.add_argument("a", help="experiment id of the base run, e.g. parity-base-<sha>")
    ap.add_argument("b", help="experiment id of the run after the change, e.g. parity-new-<sha>")
    ap.add_argument("-n", "--show", type=int, default=SHOWN, help=f"differences listed (default {SHOWN})")
    args = ap.parse_args(argv)
    if not args.a or not args.b:
        print("нужны два id прогонов: make parity A=<id> B=<id>", file=sys.stderr)
        return 2
    try:
        a, b = load(results, args.a), load(results, args.b)
        ra, rb = rankings(a, args.a), rankings(b, args.b)
        try:
            require_same_cases(a, b)
        except ValueError as e:
            only_a, only_b = len(ra.keys() - rb.keys()), len(rb.keys() - ra.keys())
            raise InputError(f"{e}: только в A: {only_a}, только в B: {only_b}, всего {len(ra)} и {len(rb)}") from None
    except InputError as e:
        print(e, file=sys.stderr)
        return 2
    diffs = diff_rankings(ra, rb)
    warned = warnings(a, b)
    print(f"parity {_title(a, 'A')} vs {_title(b, 'B')} | part={a.get('part')} | кейсов сравнено: {len(ra)}")
    for w in warned:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {w}")
    print(f"расхождений: {len(diffs)} из {len(ra)} ({100 * len(diffs) / len(ra):.2f}%)")
    if not diffs:
        print("PARITY: OK — выдачи совпали побитово (состав и порядок ИНН у каждого кейса)")
        return 0
    composition = sum(d.kind == "composition" for d in diffs)
    print(f"  из них: состав — {composition}, только порядок — {len(diffs) - composition}")
    for d in diffs[: max(args.show, 0)]:
        print(format_diff(d))
    if len(diffs) > args.show:
        print(f"  … ещё {len(diffs) - max(args.show, 0)} (-n {len(diffs)} — показать все)")
    if a.get("commit") and a.get("commit") == b.get("commit"):
        if warned:  # another config / data / tree: the input differs, so the difference is not the search's own
            print("коммит один, но различаются конфигурация или данные (см. ПРЕДУПРЕЖДЕНИЕ) — "
                  "недетерминизм не установлен")
        else:
            print("один и тот же коммит, конфигурация и данные: расхождение — недетерминизм прогона "
                  "(поиск или хранилище), не следствие правки")
    print("PARITY: FAIL — выдачи различаются")
    return 1


if __name__ == "__main__":
    sys.exit(main())
