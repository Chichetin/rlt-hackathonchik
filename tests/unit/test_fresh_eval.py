"""scripts/fresh_eval.py: the check runs on dev cases outside the 2 000-case tuning sample (#145, docs/REVIEW.md)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from src.eval.split import EvalCase, split_config

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fresh_eval.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fresh_eval", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _case(i: int) -> dict:
    return {"query": {"query_id": f"lot-{i}", "text": "Поставка бумаги; Бумага офисная А4", "okpd2": ["17.12.14.110"]},
            "relevant_inns": [], "date": "2025-08-01"}


def _cfg(tmp_path: Path, tuned: range, full: range) -> dict:
    for name, ids in (("dev", tuned), ("dev_full", full)):
        (tmp_path / f"{name}.jsonl").write_text("".join(json.dumps(_case(i)) + "\n" for i in ids), encoding="utf-8")
    paths = {name: str(tmp_path / f"{name}.jsonl") for name in ("dev", "dev_full")}
    return {**split_config(), "cases": paths}


def test_fresh_cases_exclude_the_tuning_sample_and_are_seeded(tmp_path):
    fe = _script()
    cfg = _cfg(tmp_path, tuned=range(10), full=range(40))
    ids = [c.query.query_id for c in fe.fresh_cases(cfg, n=12, seed=1)]
    assert len(ids) == 12 and ids == sorted(ids)
    assert not set(ids) & {f"lot-{i}" for i in range(10)}  # none of the cases the weights were tuned on
    assert [c.query.query_id for c in fe.fresh_cases(cfg, n=12, seed=1)] == ids
    with pytest.raises(ValueError):
        fe.fresh_cases(cfg, n=31, seed=1)  # only 30 cases are outside the tuning sample


def test_free_text_is_the_subject_without_okpd2():
    fe = _script()
    q = fe.free_text(EvalCase.model_validate(_case(1))).query
    assert q.text == "Поставка бумаги" and q.okpd2 == []


def test_cli_refuses_to_overwrite_a_result_or_mix_methods(tmp_path, monkeypatch):
    """eval/results/H-15f.json is cited by D-063 and pitch/facts.md: a check run never replaces it silently."""
    fe = _script()
    monkeypatch.setattr(fe, "RESULTS", tmp_path)
    (tmp_path / "H-15f.json").write_text("{}", encoding="utf-8")
    assert fe.main(["--exp", "H-15f", "--config-of", "H-15f"]) == 2
    assert (tmp_path / "H-15f.json").read_text(encoding="utf-8") == "{}"
    assert fe.main(["--exp", "R-x", "--baseline", "B-bm25", "--config-of", "H-15f"]) == 2
    assert fe.main(["--exp", "final-x"]) == 2
