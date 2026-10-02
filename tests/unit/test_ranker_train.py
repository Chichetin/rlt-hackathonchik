"""Temporal boundaries, candidate parity and reproducible negative controls for ranker training (#318)."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.contracts import Query
from src.eval.build_cases import read_lots
from src.eval.split import EvalCase
from src.match import ranker
from src.match import ranker_train as train
from src.match import search as search_mod

AS_OF, UNTIL = date(2025, 4, 1), date(2025, 7, 1)


@pytest.fixture
def temporal_data(tmp_path):
    # Entirely synthetic: start/end boundary, earlier history, dev and sealed-period sentinels.
    days = [date(2025, 3, 31), AS_OF, date(2025, 6, 30), UNTIL, date(2025, 10, 1)]
    pq.write_table(pa.Table.from_pylist([
        {"lot_id": i, "publish_date": day, "channel": "ЭМ", "subject": "Бумага", "procedure_name": "Бумага",
         "customer_inn": "7801000051", "start_price": 100.0} for i, day in enumerate(days)
    ]), tmp_path / "notices.parquet")
    pq.write_table(pa.Table.from_pylist([
        {"lot_id": i, "product_name": "Бумага", "okpd2_code": "17.12.14.110"} for i in range(len(days))
    ]), tmp_path / "items.parquet")
    pq.write_table(pa.Table.from_pylist([
        {"lot_id": i, "supplier_inn": "7801000012", "is_winner": True, "inn_valid": True}
        for i in range(len(days))
    ]), tmp_path / "participants.parquet")
    return tmp_path


def test_training_window_excludes_dev_before_materializing_cases(temporal_data):
    assert [lot.lot_id for lot in read_lots(temporal_data, AS_OF, UNTIL)] == [1, 2]
    cases = train.training_cases(temporal_data, AS_OF, UNTIL, n=50)
    assert [c.query.query_id for c in cases] == ["lot-1", "lot-2"]
    assert all(c.query.as_of == AS_OF and AS_OF <= c.date < UNTIL for c in cases)


@pytest.mark.parametrize(("as_of", "until", "n"), [
    (AS_OF, date(2025, 7, 2), 50), (UNTIL, UNTIL, 50), (UNTIL, AS_OF, 50),
    (AS_OF, date(2025, 10, 2), 50), (AS_OF, UNTIL, 0),
])
def test_invalid_training_window_fails_before_reading_any_labels(monkeypatch, tmp_path, as_of, until, n):
    def forbidden(*args, **kwargs):
        pytest.fail("an invalid training window reached the labels")

    monkeypatch.setattr(train, "read_lots", forbidden)
    with pytest.raises(ValueError):
        train.training_cases(tmp_path, as_of, until, n)


def test_collect_uses_the_expanded_search_pool_and_never_adds_a_label(search_data):
    query = Query(query_id="training", text="Вода питьевая", okpd2=["17.12.14.110"],
                  customer_inn="7801000051", as_of=UNTIL)
    cfg = {k: v for k, v in search_mod.load_config().items() if k != "ranker"}
    base = {m.counterparty.inn for m in search_mod.search(query, k=10**6, config=cfg)}
    cfg = {**cfg, "candidates": {"customer": 10, "okpd2": 0}}
    got: list = []
    found = search_mod.search(query, k=10**6, config=cfg, features_out=got)
    assert "7801000020" not in base and "7801000020" in {inn for inn, _ in got}
    case = EvalCase(query=query, date=UNTIL, relevant_inns=["absent-winner"], grades={"absent-winner": 2})
    x, y, q, _ = train.collect([case], cfg)
    assert len(x) == len(found) == len(got) and not y.any() and not q.any()
    assert np.array_equal(x, np.asarray([row for _, row in got]), equal_nan=True)
    changed = case.model_copy(update={"grades": {"7801000020": 2}, "relevant_inns": ["7801000020"]})
    other_x, other_y, other_q, _ = train.collect([changed], cfg)
    assert np.array_equal(x, other_x, equal_nan=True) and np.array_equal(q, other_q)
    assert other_y.sum() == 2  # labels can only annotate the candidates already found by search()


def _metadata():
    return train.cache_metadata({"candidates": {"customer": 30, "okpd2": 0}}, AS_OF, UNTIL, 10, 7)


def _cache(path, metadata):
    x = np.zeros((6, len(ranker.NAMES)))
    y, q = np.array([0, 1, 2, 0, 0, 2]), np.array([0, 0, 0, 1, 1, 1])
    np.savez_compressed(path, x=x, y=y, q=q, n_cases=2, metadata=json.dumps(metadata))
    return x, y, q


def test_cache_roundtrip_and_legacy_cache_rejection(tmp_path):
    path, metadata = tmp_path / "features.npz", _metadata()
    expected = _cache(path, metadata)
    loaded = train.read_cache(path, metadata)
    assert loaded[3] == 2
    assert all(np.array_equal(a, b) for a, b in zip(expected, loaded[:3], strict=True))
    np.savez_compressed(path, x=expected[0], y=expected[1], q=expected[2], n_cases=2)
    with pytest.raises(ValueError, match="provenance"):
        train.read_cache(path, metadata)


@pytest.mark.parametrize(("key", "value"), [
    ("config", {"candidates": {"customer": 0}}), ("features", list(reversed(ranker.NAMES))),
    ("as_of", "2025-05-01"), ("until", "2025-06-01"), ("requested_n", 20), ("seed", 8),
    ("tokenizer", "different"),
])
def test_cache_cannot_silently_reuse_a_different_pool_or_sample(tmp_path, key, value):
    path, metadata = tmp_path / "features.npz", _metadata()
    _cache(path, metadata)
    with pytest.raises(ValueError, match=key):
        train.read_cache(path, {**metadata, key: value})


def test_shuffle_preserves_each_query_grade_distribution_and_original_cache():
    y = np.tile([0, 0, 0, 1, 2], 20)
    q = np.repeat(np.arange(20), 5)
    before = y.copy()
    shuffled = train.shuffled_labels(y, q, 7)
    assert np.array_equal(y, before) and not np.array_equal(shuffled, y)
    assert np.array_equal(shuffled, train.shuffled_labels(y, q, 7))
    assert not np.array_equal(shuffled, train.shuffled_labels(y, q, 8))
    for group in np.unique(q):
        assert np.array_equal(np.sort(shuffled[q == group]), np.sort(y[q == group]))


def test_ablation_model_ignores_dropped_feature_at_inference():
    rng = np.random.default_rng(7)
    x = np.zeros((1600, len(ranker.NAMES)))
    q = np.repeat(np.arange(80), 20)
    kept, dropped = ranker.NAMES.index("cust_lots"), ranker.NAMES.index("bm25")
    x[:, kept] = rng.random(len(q))
    y = np.where(x[:, kept] > 0.8, 2, 0)
    x[:, dropped] = y  # the strongest shortcut must be removed before fitting
    ablated = train.ablated_features(x, ["bm25"])
    assert np.array_equal(x[:, dropped], y) and not ablated[:, dropped].any()
    model, _ = train.fit_lgbm(ablated, y, q, seed=7)
    assert model.feature_name() == ranker.NAMES
    assert model.feature_importance("split")[dropped] == 0
    assert model.feature_importance("split")[kept] > 0
    expected = np.asarray(model.predict(ablated, num_threads=1))
    x[:, dropped] = rng.normal(1e6, 1e4, len(q))
    assert np.array_equal(expected, np.asarray(model.predict(x, num_threads=1)))


def test_unknown_ablation_feature_is_rejected():
    with pytest.raises(ValueError, match="misspelled"):
        train.ablated_features(np.zeros((1, len(ranker.NAMES))), ["misspelled"])


def test_experiment_tokenizer_is_effective_scoped_and_recorded(monkeypatch):
    from src.match.text import tokenizer_name

    monkeypatch.delenv("BM25_TOKENIZER", raising=False)
    with train.configured_tokenizer({"tokenizer": "snowball"}):
        assert tokenizer_name() == "snowball"
        assert _metadata()["tokenizer"] == "snowball"
    assert "BM25_TOKENIZER" not in os.environ
    monkeypatch.setenv("BM25_TOKENIZER", "prefix5")
    with train.configured_tokenizer({"tokenizer": "snowball"}):
        assert tokenizer_name() == "prefix5"  # an explicit environment override has priority
    assert os.environ["BM25_TOKENIZER"] == "prefix5"


def test_input_fingerprint_detects_changed_snapshot_without_reading_lots(tmp_path):
    (tmp_path / "manifest.json").write_text('{"built_at": "synthetic"}', encoding="utf-8")
    supplier = tmp_path / "suppliers_2025-04-01.parquet"
    supplier.write_bytes(b"snapshot 1")
    before = train.input_fingerprint(tmp_path, AS_OF)
    supplier.write_bytes(b"snapshot 2")
    after = train.input_fingerprint(tmp_path, AS_OF)
    assert before["suppliers"] != after["suppliers"] and before["manifest"] == after["manifest"]
    assert before["src/match/search.py"]


def test_cli_experiment_controls_preserve_cache_and_record_model_provenance(tmp_path, monkeypatch):
    from src.eval import run

    cfg = {"retriever": "bm25", "candidates": {"customer": 30, "okpd2": 0},
           "ranker": {"kind": "lgbm", "model": "irrelevant.txt"}}

    def load(exp, *, config_of):
        assert exp == config_of == "E6"
        return cfg.copy()

    monkeypatch.setattr(run, "load_method_config", load)
    monkeypatch.setattr(train, "processed_dir", lambda: tmp_path)
    cases = [EvalCase(query=Query(query_id=f"q-{i}", text="бумага", as_of=AS_OF), date=AS_OF,
                      relevant_inns=[]) for i in range(10)]
    monkeypatch.setattr(train, "training_cases", lambda *args: cases)
    x = np.ones((50, len(ranker.NAMES)))
    y, q = np.tile([0, 0, 0, 1, 2], 10), np.repeat(np.arange(10), 5)

    def collect(got_cases, got_cfg):
        assert got_cases == cases and "ranker" not in got_cfg and got_cfg["candidates"] == cfg["candidates"]
        return x.copy(), y.copy(), q.copy(), np.zeros(len(cases))

    monkeypatch.setattr(train, "collect", collect)

    class Booster:
        best_iteration = 1
        best_score = {"valid_0": {"ndcg@10": 0.2}}

        def feature_importance(self, kind):
            return np.zeros(len(ranker.NAMES))

        def model_to_string(self):
            return "synthetic-model\n"

    def fit(got_x, got_y, got_q, seed):
        assert seed == 7 and not got_x[:, ranker.NAMES.index("bm25")].any()
        assert np.array_equal(got_q, q) and not np.array_equal(got_y, y)
        return Booster(), 2

    monkeypatch.setattr(train, "fit_lgbm", fit)
    monkeypatch.setattr(train, "fit_logreg", lambda *a: pytest.fail("--lgbm-only fitted logreg"))
    cache, out = tmp_path / "features.npz", tmp_path / "models"
    args = ["--experiment", "E6", "--n", "10", "--seed", "7", "--cache", str(cache), "--out", str(out),
            "--model-prefix", "E6-control", "--lgbm-only", "--shuffle-labels", "--drop-features", "bm25"]
    assert train.main(args) == 0
    with np.load(cache) as saved:
        assert np.array_equal(saved["x"], x) and np.array_equal(saved["y"], y)
    meta = json.loads((out / "E6-control-gbm.meta.json").read_text(encoding="utf-8"))
    assert meta["config"]["candidates"] == cfg["candidates"] and meta["experiment"] == "E6"
    assert meta["shuffle_labels"] is True and meta["drop_features"] == ["bm25"]
    assert set(meta["feature_importance_gain"]) == set(ranker.NAMES)
    assert meta["model_sha256"] == hashlib.sha256(b"synthetic-model\n").hexdigest()
    assert not (out / "E6-control-lr.json").exists()
    monkeypatch.setattr(train, "collect", lambda *a: pytest.fail("--from-cache recollected features"))
    assert train.main([*args, "--from-cache"]) == 0
