"""make eval: the backtest passes as_of = split cutoff to search() (D-018), no leak of the dev period."""

from __future__ import annotations

from datetime import date

import pytest

from src.contracts import Query
from src.eval import run as R
from src.eval.split import EvalCase, split_config
from src.match import search as search_mod

CUTOFF = date.fromisoformat(str(split_config()["cutoff"]))


def case(text: str, relevant: list[str], as_of: date | None = None) -> EvalCase:
    return EvalCase(query=Query(query_id=f"c-{text}", text=text, as_of=as_of), relevant_inns=relevant,
                    date=date(2025, 7, 10))


def test_eval_passes_split_cutoff_as_of(monkeypatch):
    seen: list[date | None] = []
    monkeypatch.setattr(search_mod, "search", lambda q, k=20, weights=None, config=None: seen.append(q.as_of) or [])
    R.evaluate([case("бумага", ["7801000012"])], {"k": 20})
    assert seen == [CUTOFF]


def test_eval_refuses_as_of_after_cutoff(monkeypatch):
    monkeypatch.setattr(search_mod, "search", lambda q, k=20, weights=None, config=None: [])
    with pytest.raises(ValueError, match="позже cutoff"):
        R.evaluate([case("бумага", [], as_of=date(2025, 8, 1))], {"k": 20})
    assert R.with_cutoff(Query(query_id="q", text="x", as_of=date(2025, 1, 1)), CUTOFF).as_of == date(2025, 1, 1)


def test_eval_on_fixture_hides_supplier_seen_only_after_cutoff(search_data, monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "c.sqlite"))
    real = search_mod.search
    returned: list[str | None] = []

    def spy(q, k=20, weights=None, config=None):
        res = real(q, k=k, weights=weights)
        returned.extend(m.counterparty.inn for m in res)
        return res

    monkeypatch.setattr(search_mod, "search", spy)
    body = R.evaluate([case("бумага", ["470100000088"]), case("бумага офисная", ["7801000012"])],
                      {"k": 20, "weights": search_mod.load_config()["weights"]})
    # 470100000088's only paper lot (7) is after the cutoff: invisible to the backtest
    assert "470100000088" not in returned and "7801000012" in returned
    assert [q["recall@10"] for q in body["per_query"]] == [0.0, 1.0]


def test_experiment_overrides_are_merged_over_search_config(tmp_path, monkeypatch):
    from src.eval import run as R

    exp = tmp_path / "experiments.yaml"
    exp.write_text("X-1:\n  signals: {incumbent: 0.3}\n  options: {tie_break: bm25}\n  k: 10\n", encoding="utf-8")
    monkeypatch.setattr(R, "EXPERIMENTS_CONFIG", exp)
    product = R.load_method_config("no-such-experiment")
    cfg = R.load_method_config("X-1")
    assert cfg["signals"]["incumbent"] == 0.3 and cfg["options"] == {"tie_break": "bm25"} and cfg["k"] == 10
    assert cfg["weights"] == product["weights"] and "options" not in product


def test_repository_experiments_are_valid_configs():
    import yaml

    from src.eval import run as R
    from src.match import search as S
    from src.match.signals import signal_shares

    for exp in yaml.safe_load(R.EXPERIMENTS_CONFIG.read_text(encoding="utf-8")):
        cfg = S.check_config(R.load_method_config(exp))
        signal_shares(cfg)
        assert "method" not in cfg
