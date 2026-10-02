"""Fresh dev evaluation excludes the entire tuning set and freezes the paired sample."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from src.contracts import Query
from src.eval import run as R
from src.eval.split import EvalCase


def cases(n: int) -> list[EvalCase]:
    return [EvalCase(query=Query(query_id=f"lot-{i:03}", text="paper"), relevant_inns=[], date=date(2025, 7, 10))
            for i in range(n)]


def test_fresh_sample_excludes_all_dev_and_is_stable_across_input_order():
    every = cases(40)
    dev = every[::2]  # exclude the whole tuning set, not merely its first screening sample
    selected, provenance = R.fresh_dev_sample(every, dev, 12, 318)
    ids = [c.query.query_id for c in selected]
    assert len(ids) == 12 and ids == sorted(ids)
    assert set(ids).isdisjoint(c.query.query_id for c in dev)
    assert (selected, provenance) == R.fresh_dev_sample(every[::-1], dev[::-1], 12, 318)
    assert selected != R.fresh_dev_sample(every, dev, 12, 319)[0]
    assert provenance["query_ids"] == ids
    assert provenance["query_ids_sha256"] == hashlib.sha256(
        json.dumps(ids, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert provenance["source_part"] == "dev_full" and provenance["excluded_part"] == "dev"
    assert (provenance["n"], provenance["of"], provenance["source_n"], provenance["excluded_n"]) == (12, 20, 40, 20)
    assert provenance["seed"] == 318
    # Additional tuning exposure must be visible in provenance even if it does not change this sample.
    extra = next(c for c in every if c not in selected and c not in dev)
    _, changed = R.fresh_dev_sample(every, [*dev, extra], 12, 318)
    assert changed["excluded_ids_sha256"] != provenance["excluded_ids_sha256"]


@pytest.mark.parametrize("duplicate_part", ["dev_full", "dev"])
def test_fresh_sample_rejects_duplicate_ids(duplicate_part):
    every, dev = cases(10), cases(2)
    source = every if duplicate_part == "dev_full" else dev
    source.append(source[0])
    with pytest.raises(ValueError, match=f"{duplicate_part}: query_id повторяется"):
        R.fresh_dev_sample(every, dev, 3, 318)


@pytest.fixture
def eval_cli(tmp_path, monkeypatch):
    """Use real case loading and split guards; replace only expensive ranking and run metadata."""
    dev, every = cases(10), cases(40)
    paths = {part: tmp_path / f"{part}.jsonl" for part in ("dev", "dev_full", "holdout")}
    for part, rows in (("dev", dev), ("dev_full", every)):
        paths[part].write_text("".join(c.model_dump_json() + "\n" for c in rows), encoding="utf-8")
    split = {"cutoff": "2025-07-01", "holdout_from": "2025-10-01", "cases": paths}
    monkeypatch.setattr(R, "split_config", lambda: split)
    monkeypatch.setattr(R, "load_method_config", lambda *args: {"k": 20})
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(R, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(R, "print_table", lambda rec: None)
    monkeypatch.setattr(R, "result_record", lambda exp, part, cfg, body: {"exp_id": exp, "part": part, **body})
    evaluated = []
    loaded = []
    real_load = R.load_cases

    def load(part, cfg):
        loaded.append(part)
        assert part != "holdout"
        return real_load(part, cfg)

    def evaluate(selected, cfg, **kwargs):
        evaluated.append((selected, kwargs))
        return {"n_queries": len(selected), "per_query": [{"query_id": c.query.query_id} for c in selected]}

    monkeypatch.setattr(R, "load_cases", load)
    monkeypatch.setattr(R, "evaluate", evaluate)
    return paths, evaluated, loaded


def test_fresh_cli_persists_frozen_selection_and_passes_candidate_audit(eval_cli):
    _, evaluated, loaded = eval_cli
    assert R.main(["--exp", "fresh", "--fresh-sample", "12", "--sample-seed", "318", "--candidate-audit"]) == 0
    rec = json.loads((R.RESULTS / "fresh.json").read_text())
    assert rec["part"] == "dev-fresh-s12-seed318"
    assert rec["n_queries"] == 12 and rec["sample"]["seed"] == 318
    assert rec["sample"]["query_ids"] == [row["query_id"] for row in rec["per_query"]]
    assert len(evaluated) == 1 and evaluated[0][1] == {"candidate_audit": True}
    assert set(rec["sample"]["query_ids"]).isdisjoint(c.query.query_id for c in cases(10))
    assert loaded == ["dev_full", "dev"]


@pytest.mark.parametrize("args", [
    ["--fresh-sample", "0", "--sample-seed", "318"],
    ["--fresh-sample", "-1", "--sample-seed", "318"],
    ["--fresh-sample", "2"],
    ["--sample-seed", "318"],
    ["--sample", "2", "--sample-seed", "318"],
    ["--fresh-sample", "2", "--sample-seed", "318", "--sample", "2"],
    ["--fresh-sample", "2", "--sample-seed", "318", "--part", "real"],
    ["--fresh-sample", "2", "--sample-seed", "318", "--part", "dev_full"],
])
def test_fresh_cli_rejects_invalid_combinations_before_loading(eval_cli, args):
    _, evaluated, loaded = eval_cli
    assert R.main(["--exp", "invalid", *args]) == 2
    assert not evaluated and not loaded
    assert not R.RESULTS.exists()


def test_fresh_cli_requires_enough_cases_after_exclusion(eval_cli, capsys):
    _, evaluated, _ = eval_cli
    assert R.main(["--exp", "short", "--fresh-sample", "31", "--sample-seed", "318"]) == 2
    assert "доступно 30" in capsys.readouterr().err
    assert not evaluated and not R.RESULTS.exists()


def test_fresh_cli_keeps_dev_date_guard(eval_cli, capsys):
    paths, evaluated, _ = eval_cli
    leaked = cases(1)[0].model_copy(update={"date": date(2025, 10, 1)})
    with paths["dev_full"].open("a", encoding="utf-8") as output:
        output.write(leaked.model_dump_json() + "\n")
    assert R.main(["--exp", "leak", "--fresh-sample", "2", "--sample-seed", "318"]) == 2
    assert "split leak" in capsys.readouterr().err
    assert not evaluated and not R.RESULTS.exists()


def test_ordinary_sample_keeps_fixed_seed_and_original_order(eval_cli):
    _, _, loaded = eval_cli
    assert R.main(["--exp", "screen", "--sample", "3"]) == 0
    rec = json.loads((R.RESULTS / "screen.json").read_text())
    assert rec["part"] == "dev-s3"
    assert [row["query_id"] for row in rec["per_query"]] == ["lot-003", "lot-005", "lot-008"]
    assert "sample" not in rec and loaded == ["dev"]
