"""`make eval-final CONFIRM=<sha256>`: the only reader of the sealed holdout period.

Gate (as `make eval-test` in FloodValue):
1. EVAL_FINAL_CONFIRM equals sha256 of the frozen configuration (configs/search.yaml +
   configs/split.yaml + committed method code, run.METHOD_PATHS) — without CONFIRM it prints the current hash;
2. the tree is clean for FROZEN_PATHS: the configuration is reproducible from the commit;
3. this configuration hash has not been run on holdout before (a rerun after changes is a new
   experiment on dev, not a second look at holdout) — checked in eval/final-log.md AND in a
   marker file in the shared git dir, so another worktree or a restored log does not reopen it;
4. the search is not a # STUB.
A git failure refuses (fail closed). The attempt is recorded BEFORE the holdout is read, so a
crash after reading still leaves a trace; the result row is appended after the run.

What is scored: `holdout_sample` cases of configs/split.yaml (null — all), the n lowest
sha256(holdout_seed:query_id). The whole holdout is 88 848 cases at 0.25–0.30 s each — a 6–7.5 h
one-shot run that does not fit the window before code freeze (#145, docs/REVIEW.md); the sample
is fixed in the hashed config before the run, so it cannot depend on any result.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from src.eval import run as R
from src.eval.split import ROOT, EvalCase, load_cases, split_config, unseal
from src.match import search as search_mod
from src.settings import get_settings

CONFIRM_ENV = "EVAL_FINAL_CONFIRM"
FROZEN_PATHS = ("src", "configs", "pyproject.toml", "uv.lock")
FINAL_LOG = ROOT / "eval" / "final-log.md"


def holdout_sample(cases: list[EvalCase], n: int | None, seed: int) -> list[EvalCase]:
    """The n cases with the lowest sha256(seed:query_id), in query_id order; no n or n ≥ all — every case."""
    if not n or n >= len(cases):
        return cases
    picked = sorted(cases, key=lambda c: hashlib.sha256(f"{seed}:{c.query.query_id}".encode()).hexdigest())[:n]
    return sorted(picked, key=lambda c: c.query.query_id)


def seen_file(root: Path) -> Path:
    common = Path(R.git("rev-parse", "--git-common-dir", root=root, strict=True))
    return (common if common.is_absolute() else root / common) / "eval-final-seen"


def gate(confirm: str | None, root: Path = ROOT, log: Path = FINAL_LOG) -> str:
    """Returns the config sha256 if the holdout may be read, else raises PermissionError."""
    if get_settings().bm25_tokenizer is not None:  # an empty value is «not set»: search() ignores it too
        raise PermissionError("BM25_TOKENIZER задан: eval-final оценивает только токенизатор из configs/search.yaml")
    try:
        sha = R.config_sha256(root)
        dirty = R.git("status", "--porcelain", "--", *FROZEN_PATHS, root=root, strict=True)
        seen = seen_file(root)
    except R.GitError as e:
        raise PermissionError(f"git недоступен — гейт не может проверить конфигурацию ({e})") from e
    if not confirm:
        raise PermissionError(f"нет {CONFIRM_ENV}. Текущая конфигурация: {sha}. "
                              f"Запуск: make eval-final CONFIRM={sha}")
    if confirm != sha:
        raise PermissionError(f"{CONFIRM_ENV} не совпадает с sha256 конфигурации {sha}")
    if dirty:
        raise PermissionError(f"дерево не чистое для {FROZEN_PATHS}:\n{dirty}")
    if (log.exists() and sha in log.read_text(encoding="utf-8")) or (
            seen.exists() and sha in seen.read_text(encoding="utf-8")):
        raise PermissionError(f"конфигурация {sha[:12]} уже оценивалась на holdout ({log.name})")
    if getattr(search_mod, "IS_STUB", False):
        raise PermissionError("search() — # STUB: на holdout оценивается только реальный метод")
    return sha


def main() -> int:
    try:
        sha = gate(get_settings().eval_final_confirm)
    except PermissionError as e:
        print(f"eval-final: отказ — {e}", file=sys.stderr)
        return 2
    cfg = yaml.safe_load(R.SEARCH_CONFIG.read_text(encoding="utf-8"))
    split = split_config()
    n, seed = split.get("holdout_sample"), split.get("holdout_seed") or 0
    # checked before the marker: a typo in the config must not burn the configuration without a result
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n <= 0) or not isinstance(seed, int):
        print(f"eval-final: отказ — holdout_sample = {n!r}, holdout_seed = {seed!r}: нужны целые (n > 0 или null)",
              file=sys.stderr)
        return 2
    with seen_file(ROOT).open("a", encoding="utf-8") as f:  # the attempt, before the holdout is read
        f.write(f"{sha} {datetime.now(UTC).isoformat(timespec='seconds')} {get_settings().eval_user}\n")
    with unseal():
        every = load_cases("holdout", split)
        cases = holdout_sample(every, n, seed)
        rec = R.result_record(f"final-{sha[:8]}", "holdout", cfg, R.evaluate(cases, cfg))
    rec["sample"] = {"n": len(cases), "of": len(every), "seed": seed}
    out = R.RESULTS / f"final-{sha[:8]}.json"
    # one line per case: the 3 000-case sample is ≈ 1.2 MiB; the whole holdout (null) ≈ 11 MiB — over the 5 MB guard
    out.write_text(R.dump_record(rec), encoding="utf-8")
    m = rec["metrics"]
    row = (f"| {datetime.now(UTC):%Y-%m-%d %H:%M} UTC | {rec['commit'][:8]} | {sha} | "
           f"{get_settings().eval_user} | {rec['n_queries']} из {len(every)} (seed {seed}) | "
           + " | ".join(f"{m[k]['value']:.3f} [{m[k]['ci95'][0]:.3f}, {m[k]['ci95'][1]:.3f}]" for k in m)
           + f" | {out.relative_to(ROOT)} |\n")
    with FINAL_LOG.open("a", encoding="utf-8") as f:
        f.write(row)
    R.print_table(rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
