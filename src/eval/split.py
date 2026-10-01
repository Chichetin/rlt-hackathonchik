"""Time split of eval cases and the seal on the holdout period (research/EVAL.md).

Split by time (section 7): features only from data before `cutoff`, queries from after.
Parts:
  dev      — queries in [cutoff, holdout_from): tuning, `make eval EXP=<id>` (a stratified sample);
  dev_full — all dev queries (outside git), `real` — 5–10 hand-readable dev cases (same window);
  holdout  — queries from `holdout_from`: SEALED. Read only inside `make eval-final` for the
             frozen configuration (src/eval/final.py), every run logged in eval/final-log.md.

Dates in configs/split.yaml come from the data profile (research/PROBLEM.md); case builder —
src/eval/build_cases.py (T-07): which lots, which outcome (winner / offers) is "relevant".
"""

from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.contracts import Query

ROOT = Path(__file__).resolve().parents[2]
SPLIT_CONFIG = ROOT / "configs" / "split.yaml"

_unsealed: contextvars.ContextVar[bool] = contextvars.ContextVar("holdout_unsealed", default=False)


class SealedError(PermissionError):
    pass


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Query
    relevant_inns: list[str]
    date: date  # procurement date; decides dev/holdout
    # Graded relevance per INN (winner 2, other offer 1); empty = every relevant INN has grade 1.
    grades: dict[str, int] = Field(default_factory=dict)


def split_config(path: Path = SPLIT_CONFIG) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


PARTS = ("dev", "dev_full", "real", "holdout")


def _case_path(part: str, cfg: dict) -> Path:
    p = Path(cfg["cases"][part])
    return p if p.is_absolute() else ROOT / p


@contextmanager
def unseal():
    """Only src/eval/final.py calls this, after the sha256 + clean-tree gate."""
    token = _unsealed.set(True)
    try:
        yield
    finally:
        _unsealed.reset(token)


def load_cases(part: str, cfg: dict | None = None) -> list[EvalCase]:
    cfg = cfg or split_config()
    if part == "holdout" and not _unsealed.get():
        raise SealedError("holdout запечатан: только `make eval-final` для замороженной конфигурации")
    if part not in PARTS:
        raise ValueError(part)
    path = _case_path(part, cfg)
    cases = [EvalCase(**json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    d = {k: date.fromisoformat(str(cfg[k])) for k in ("cutoff", "holdout_from")}
    lo, hi = (d["holdout_from"], date.max) if part == "holdout" else (d["cutoff"], d["holdout_from"])
    leaked = [c.query.query_id for c in cases if not (lo <= c.date < hi)]
    if leaked:
        raise ValueError(f"{part}: cases outside [{lo}, {hi}): {leaked[:5]} — split leak")
    return cases


def write_cases(part: str, cases: list[EvalCase], cfg: dict) -> Path:
    """The case builder writes every part here, so the holdout path stays inside this module."""
    if part not in PARTS:
        raise ValueError(part)
    path = _case_path(part, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(c.model_dump_json() + "\n" for c in cases), encoding="utf-8")
    return path
