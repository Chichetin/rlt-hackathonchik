"""The time split of the backtest — configs/split.yaml (cutoff, dates of the parts, case files).

L0 (#294 п. 1, D-106): the ingest (suppliers as of the cutoff) and the index prebuild read the cutoff from here,
so the product core no longer imports the eval tool. Only the config is read here; the holdout cases are opened
by src/eval/split.py and src/eval/final.py alone (D-010, `make seal-check`).
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPLIT_CONFIG = ROOT / "configs" / "split.yaml"


def split_config(path: Path = SPLIT_CONFIG) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
