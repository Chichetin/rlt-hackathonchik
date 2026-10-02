"""make eval: the backtest passes as_of = split cutoff to search() (D-018), no leak of the dev period."""

from __future__ import annotations

import json
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
        res = real(q, k=k, weights=weights, config=config)
        returned.extend(m.counterparty.inn for m in res)
        return res

    monkeypatch.setattr(search_mod, "search", spy)
    body = R.evaluate([case("бумага", ["470100000088"]), case("бумага офисная", ["7801000012"])],
                      {**search_mod.load_config(), "k": 20})
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
    assert cfg["signals"]["incumbent"] == 0.3 and cfg["k"] == 10
    assert cfg["options"] == {**product.get("options", {}), "tie_break": "bm25"}  # merged one level deep
    assert cfg["weights"] == product["weights"]
    # the same overrides under another id (H-15f-real); an unknown id is an error, not the product config
    assert R.load_method_config("X-1-real", config_of="X-1") == cfg
    assert R.load_method_config("X-1-real") == product
    with pytest.raises(ValueError, match="X-2"):
        R.load_method_config("X-2-real", config_of="X-2")
    assert R.main(["--exp", "X-2-real", "--config-of", "X-2"]) == 2
    assert R.main(["--exp", "X-1", "--sample", "-5"]) == 2  # review of #246: no silent full run on a bad --sample
    assert R.main(["--exp", "X-1", "--sample", "0"]) == 2


def test_eval_passes_experiment_config_to_search(monkeypatch, tmp_path):
    from src.eval import run as R

    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "c.sqlite"))
    seen: list[dict | None] = []
    monkeypatch.setattr(search_mod, "search", lambda q, k=20, weights=None, config=None: seen.append(config) or [])
    cfg = {**search_mod.load_config(), "options": {"tie_break": "bm25"}}
    R.evaluate([case("бумага", ["7801000012"])], cfg)
    assert seen == [cfg]


def test_repository_experiments_are_valid_configs():
    import yaml

    from src.eval import run as R
    from src.match import search as S
    from src.match.signals import signal_shares

    for exp in yaml.safe_load(R.EXPERIMENTS_CONFIG.read_text(encoding="utf-8")):
        cfg = S.check_config(R.load_method_config(exp))
        signal_shares(cfg)
        assert "method" not in cfg


@pytest.mark.parametrize(("exp", "result"), [
    ("v1", "E-002"), ("H-15a-03", "H-15a-03"), ("H-15a-06", "H-15a-06"), ("H-15b", "H-15b"), ("H-15c", "H-15c"),
    ("H-15d-tie", "H-15d-tie"), ("H-15e", "H-15e"), ("H-15f", "H-15f"), ("H-15g", "H-15g"), ("H-15f-geo", "H-15f-geo"),
    ("H-196-sat100", "H-196-sat100"), ("H-196-sat500", "H-196-sat500"), ("v2", "v2"), ("R-198-gbm", "R-198-gbm"),
    ("S-282-spell", "S-282-on-dev"), ("S-282-nospell", "S-282-off-dev"),
])
def test_experiment_entries_reproduce_recorded_configs(exp, result):
    """The product default moved to H-15f (#174): every entry of configs/experiments.yaml still gives the
    effective configuration stored in its result file, so `make eval EXP=<id>` reproduces the slide numbers."""
    rec = json.loads((R.RESULTS / f"{result}.json").read_text(encoding="utf-8"))["config"]
    cfg = R.load_method_config(exp)
    assert cfg["weights"] == rec["weights"]
    assert search_mod.scoring_options(cfg) == search_mod.scoring_options(rec)
    from src.match.signals import signal_shares
    assert signal_shares(cfg) == signal_shares(rec)
    assert (cfg["retriever"], cfg["k"]) == (rec["retriever"], rec["k"])
    from src.match import ranker
    assert ranker.spec(cfg) == ranker.spec(rec)  # the default has the ranker on (#198): older entries turn it off
    assert cfg.get("spell", False) == rec.get("spell", False)  # the speller is on (#282): older entries turn it off


def test_saturation_summary_counts_distinct_scores():
    from src.eval.saturation import summarize

    rec = summarize([[1.0] * 10, [1.0, 0.9, 0.9, 0.5], [0.7, 0.6, 0.5, 0.4, 0.3], []])
    assert rec == {"cases": 3, "distinct_scores_mean": 3.0, "share_le_3_distinct": 0.667, "share_all_equal": 0.333,
                   "share_first_at_max": 0.667}
    assert summarize([[1.0, 0.5, 0.4]], k=1)["distinct_scores_mean"] == 1.0  # only the top-k is looked at
    assert summarize([])["cases"] == 0
    # the screen shows two decimals: scores that differ in the API can be equal there
    near = [[0.9996, 0.9973, 0.9849, 0.9835, 0.9628]]
    assert summarize(near)["distinct_scores_mean"] == 5.0 and summarize(near)["share_first_at_max"] == 1.0
    assert summarize(near, decimals=2)["distinct_scores_mean"] == 3.0  # 1.00, 1.00, 0.98, 0.98, 0.96


def test_drop_okpd2_keeps_text_and_everything_else():
    c = case("бумага офисная", ["7801000012"])
    c = c.model_copy(update={"query": c.query.model_copy(update={"okpd2": ["17.12.14.110", "17.12"]})})
    (got,) = R.drop_okpd2([c])
    assert got.query.okpd2 == [] and got.query.text == c.query.text and got.relevant_inns == c.relevant_inns
    assert c.query.okpd2 == ["17.12.14.110", "17.12"]  # the input is not mutated


def test_free_text_is_the_subject_without_lot_context():
    c = case("Поставка бумаги; Бумага для офисной техники", ["7801000012"])
    c = c.model_copy(update={"query": c.query.model_copy(update={
        "okpd2": ["17.12.14.110"], "customer_inn": "7800000010", "budget": 45000.0, "channel": "ЭМ"})})
    (got,) = R.free_text([c])
    q = got.query
    assert q.text == "Поставка бумаги" and q.okpd2 == [] and got.relevant_inns == c.relevant_inns
    assert q.customer_inn is None and q.budget is None and q.channel is None
    assert c.query.customer_inn == "7800000010"  # the input is not mutated


def test_free_text_bare_keeps_only_the_subject_and_region():
    """#511: the jury's query — the subject before the first «; », no ОКПД2, customer, budget or channel."""
    c = case("Поставка бумаги для нужд школы; Бумага офисная А4", ["7801000012"])
    c = c.model_copy(update={"query": c.query.model_copy(update={
        "okpd2": ["17.12.14.110"], "customer_inn": "7802084569", "budget": 1000.0, "channel": "ЭМ",
        "region": "Санкт-Петербург"})})
    (got,) = R.free_text_bare([c])
    q = got.query
    assert q.text == "Поставка бумаги для нужд школы" and q.okpd2 == []
    assert q.customer_inn is None and q.budget is None and q.channel is None
    assert q.region == "Санкт-Петербург" and got.relevant_inns == c.relevant_inns
    assert c.query.customer_inn == "7802084569"  # the input is not mutated


def test_result_records_the_data_snapshot(search_data, monkeypatch):
    """A metric level compares only between runs on the same data (#421): the result names the ingest snapshot
    and how many queries the ranker scored without the supplier profile."""
    from src.match import ranker

    monkeypatch.setattr(ranker, "without_profile", 3)
    manifest = json.loads((search_data / "manifest.json").read_text(encoding="utf-8"))
    rec = R.result_record("x", "dev", {}, {})
    assert rec["data"] == {"built_at": manifest["built_at"], "rows": manifest["rows"], "queries_without_profile": 3}


def test_each_eval_run_counts_its_own_queries(search_data, monkeypatch, tmp_path):
    """Several evaluate() in one process (new_fit_compare, parity tests) do not add up the count (D-144)."""
    from src.match import ranker

    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "c.sqlite"))
    monkeypatch.setattr(ranker, "without_profile", 5)  # left over from an earlier run
    R.evaluate([case("бумага офисная", ["7801000012"])], {**search_mod.load_config(), "k": 20})
    assert ranker.without_profile == 0  # the fixture has the profile


def test_every_must_be_positive():
    from src.eval.run import main

    assert main(["--exp", "rel-off-f", "--every", "0"]) == 2
