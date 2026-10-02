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
    product = search_mod.load_config()
    measured = run.load_method_config("F-511-sat100")
    baseline = run.load_method_config("F-511-v2")
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
