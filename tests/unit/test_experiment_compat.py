"""The customer route and its formula must preserve measured experiment configurations."""

import json
from datetime import date

import pytest

from src.contracts import Query
from src.eval import run
from src.match import ranker
from src.match import search as search_mod

FREE = Query(query_id="q", text="бумага офисная", as_of=date(2025, 7, 1))


def test_legacy_saturation_experiment_keeps_its_recorded_formula(search_data):
    cfg = run.load_method_config("H-196-sat500")
    recorded = json.loads((run.RESULTS / "H-196-sat500.json").read_text(encoding="utf-8"))["config"]
    effective = search_mod.no_customer_options(cfg)
    assert (effective["sat_lots"], effective["sat_wins"]) == (500, 250)
    expected = search_mod.search(FREE, config=recorded)
    assert expected
    assert search_mod.search(FREE, config=cfg) == expected


def test_product_without_customer_uses_the_measured_sat100_formula(search_data):
    # the F-511-sat100 / F-511-v2 overrides over the current product formula: the entries themselves are pinned to
    # the scoring defaults before D-197 / D-198 (#588), the product moved on
    product = search_mod.load_config()
    measured = {**product, "ranker": None, "no_customer_options": product["no_customer_options"]}
    baseline = {**product, "ranker": None, "no_customer_options": None}
    expected = search_mod.search(FREE, config=measured)
    assert expected
    assert search_mod.search(FREE, config=product) == expected
    assert expected != search_mod.search(FREE, config=baseline)
    features: list = []
    assert search_mod.search(FREE, config=product, features_out=features) == search_mod.search(FREE, config=baseline)
    assert features  # collecting ranker features keeps the original 20/10 inputs


def test_customer_route_preserves_ranker_and_original_components(search_data, tmp_path):
    # Fixed coefficients exercise inference without optional LightGBM or any model training.
    n = len(ranker.NAMES)
    coef = [0.0] * n
    coef[ranker.NAMES.index("n_lots")] = 1.0
    path = tmp_path / "fixed-ranker.json"
    path.write_text(json.dumps({"features": ranker.NAMES, "fill": [0.0] * n, "mean": [0.0] * n,
                                "std": [1.0] * n, "coef": coef, "intercept": 0.0}), encoding="utf-8")
    cfg = search_mod.load_config()
    cfg["ranker"] = {**cfg["ranker"], "kind": "logreg", "model": str(path)}
    before = {**cfg, "no_customer_options": None,
              "ranker": {**cfg["ranker"], "customer_only": False}}
    query = FREE.model_copy(update={"customer_inn": "7800000010"})
    expected = search_mod.search(query, config=before)
    assert expected and any(m.rank_reason for m in expected)
    assert search_mod.search(query, config=cfg) == expected


@pytest.mark.parametrize("name", ["G-490-free", "G-490-dev", "G-490L-free"])
def test_recorded_routing_experiments_keep_the_original_formula(name):
    cfg = run.load_method_config(name)
    recorded = json.loads((run.RESULTS / f"{name}.json").read_text(encoding="utf-8"))["config"]
    assert search_mod.no_customer_options(cfg) == search_mod.scoring_options(recorded)
    assert cfg["no_customer_options"] is None


def test_product_without_customer_orders_as_f511_sat100_and_scores_by_the_product_formula(search_data):
    """#612: free text without a customer goes in the order of F-511-sat100 (weights before #576 + sat100, the best
    measured order of this path); the score and the card stay the product formula (D-198)."""
    product = search_mod.load_config()
    got = search_mod.search(FREE, config=product)
    assert len(got) > 2
    measured = search_mod.search(FREE, config=run.load_method_config("F-511-sat100"))
    assert [m.counterparty.inn for m in got] == [m.counterparty.inn for m in measured]
    by_activity_only = {**product, "no_customer_options": {**product["no_customer_options"],
                                                           "order_weights": {"activity": 1.0}}}
    # eval (pipeline.pick with a config) passes the config's own weights: the same order as the product
    assert search_mod.search(FREE, config=by_activity_only, weights=product["weights"]) == search_mod.search(
        FREE, config=by_activity_only)
    plain = {**product, "no_customer_options": {k: v for k, v in product["no_customer_options"].items()
                                                if k != "order_weights"}}
    by_score = {m.counterparty.inn: m for m in search_mod.search(FREE, config=plain)}
    assert all(m == by_score[m.counterparty.inn] for m in got if m.counterparty.inn in by_score)
    by_activity = {**product, "no_customer_options": {**product["no_customer_options"],
                                                      "order_weights": {"activity": 1.0}}}
    acts = [next(c.value for c in m.components if c.name == "activity")
            for m in search_mod.search(FREE, config=by_activity)]
    assert acts == sorted(acts, reverse=True) and len(set(acts)) > 1
    with pytest.raises(ValueError, match="order_weights"):
        search_mod.check_config({**product, "no_customer_options": {"order_weights": {"bm25": 1.0}}})


def test_order_weights_explain_a_row_above_a_higher_score(search_data):
    """#612: «Выше следующего» names the order key only where the next supplier has a higher score."""
    got = sorted(search_mod.search(FREE, config=search_mod.load_config()), key=lambda m: m.score)[:3]
    assert len(got) == 3 and got[0].score < got[1].score
    ranker.set_order(got, [0.9, 0.5, 0.1])
    shown = ranker.reasons(got)
    assert shown[0].rank_reason == "выше следующего: соответствие товару и опыт — 0.90 против 0.50"
    assert shown[-1].rank_reason is None
    ranker.forget()
    assert ranker.reasons(got) == got
