"""`make eval-final CONFIRM=<sha256>`: the only reader of the sealed holdout period.

Gate (as `make eval-test` in FloodValue):
1. EVAL_FINAL_CONFIRM equals sha256 of the frozen configuration (configs/search.yaml +
   configs/split.yaml + the committed src/ tree) — without CONFIRM it prints the current hash;
2. the tree is clean for FROZEN_PATHS: the configuration is reproducible from the commit;
3. this configuration hash has not been run on holdout before (a rerun after changes is a new
   experiment on dev, not a second look at holdout) — checked in eval/final-log.md AND in a
   marker file in the shared git dir, so another worktree or a restored log does not reopen it;
4. the search is not a # STUB.
A git failure refuses (fail closed). The attempt is recorded BEFORE the holdout is read, so a
crash after reading still leaves a trace; the result row is appended after the run.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from src.eval import run as R
from src.eval.split import ROOT, load_cases, split_config, unseal
from src.match import search as search_mod

CONFIRM_ENV = "EVAL_FINAL_CONFIRM"
FROZEN_PATHS = ("src", "configs", "pyproject.toml", "uv.lock")
FINAL_LOG = ROOT / "eval" / "final-log.md"


def seen_file(root: Path) -> Path:
    common = Path(R.git("rev-parse", "--git-common-dir", root=root, strict=True))
    return (common if common.is_absolute() else root / common) / "eval-final-seen"


def gate(confirm: str | None, root: Path = ROOT, log: Path = FINAL_LOG) -> str:
    """Returns the config sha256 if the holdout may be read, else raises PermissionError."""
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
        sha = gate(os.environ.get(CONFIRM_ENV))
    except PermissionError as e:
        print(f"eval-final: отказ — {e}", file=sys.stderr)
        return 2
    cfg = yaml.safe_load(R.SEARCH_CONFIG.read_text(encoding="utf-8"))
    with seen_file(ROOT).open("a", encoding="utf-8") as f:  # the attempt, before the holdout is read
        f.write(f"{sha} {datetime.now(UTC).isoformat(timespec='seconds')} {os.environ.get('USER', '?')}\n")
    with unseal():
        cases = load_cases("holdout", split_config())
        rec = R.result_record(f"final-{sha[:8]}", "holdout", cfg, R.evaluate(cases, cfg))
    out = R.RESULTS / f"final-{sha[:8]}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    m = rec["metrics"]
    row = (f"| {datetime.now(UTC):%Y-%m-%d %H:%M} UTC | {rec['commit'][:8]} | {sha} | "
           f"{os.environ.get('USER', '?')} | {rec['n_queries']} | "
           + " | ".join(f"{m[k]['value']:.3f} [{m[k]['ci95'][0]:.3f}, {m[k]['ci95'][1]:.3f}]" for k in m)
           + f" | {out.relative_to(ROOT)} |\n")
    with FINAL_LOG.open("a", encoding="utf-8") as f:
        f.write(row)
    R.print_table(rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
