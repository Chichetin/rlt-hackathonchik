"""Learned ranker (#198): off = v2 unchanged, features deterministic, a model reorders and explains."""

from __future__ import annotations

import json
import logging
import math
import re

import numpy as np
import pytest

from src.contracts import Query
from src.match import ranker
from src.match import search as search_mod
from src.match.search import search

Q = Query(query_id="q", text="бумага офисная", okpd2=["17.12.14.110"], customer_inn="7800000010")


def test_off_is_v2_and_features_are_deterministic(search_data):
    cfg = {k: v for k, v in search_mod.load_config().items() if k != "ranker"}
    plain = search(Q, config=cfg)
    got: list = []
    assert search(Q, config=cfg, features_out=got) == plain  # collecting features changes nothing
    again: list = []
    search(Q, config=cfg, features_out=again)
    assert got and len(got) >= len(plain) and all(len(r) == len(ranker.NAMES) for _, r in got)
    a, b = np.asarray([r for _, r in got]), np.asarray([r for _, r in again])
    assert [i for i, _ in got] == [i for i, _ in again] and np.array_equal(a, b, equal_nan=True)
    fit = a[:, ranker.NAMES.index("fit")]
    by_inn = {m.counterparty.inn: m for m in plain}
    for (inn, row), f in zip(got, fit, strict=True):
        if inn in by_inn:
            comps = {c.name: c for c in by_inn[inn].components}
            assert f == comps["product_fit"].value
            # v2 experience counts lots with similar positions: the same counts are the raw features
            found = re.findall(r"(\d+) лот\(ов\).*побед: (\d+)", comps["experience"].explanation)
            lots, wins = (int(x) for x in found[0])
            assert (row[ranker.NAMES.index("n_lots")], row[ranker.NAMES.index("n_wins")]) == (
                pytest.approx(math.log1p(lots)), pytest.approx(math.log1p(wins)))


def test_model_reorders_and_explains(search_data, tmp_path):
    n = len(ranker.NAMES)
    coef = np.zeros(n)
    coef[ranker.NAMES.index("n_lots")] = 1.0  # rank by matched lots only
    model = tmp_path / "lr.json"
    mean, std = np.zeros(n), np.ones(n)
    mean[ranker.NAMES.index("n_lots")], std[ranker.NAMES.index("n_lots")] = 0.5, 2.0
    body = {"features": ranker.NAMES, "fill": [0.0] * n, "mean": mean.tolist(), "std": std.tolist(),
            "coef": coef.tolist(), "intercept": 0.0}
    model.write_text(json.dumps(body), encoding="utf-8")
    cfg = {**search_mod.load_config(), "ranker": {"kind": "logreg", "model": str(model)}}
    plain_cfg = {k: v for k, v in cfg.items() if k != "ranker"}
    got: list = []
    search(Q, config=plain_cfg, features_out=got)
    res = search(Q, config=cfg)
    lots = {inn: r[ranker.NAMES.index("n_lots")] for inn, r in got}
    assert [lots[m.counterparty.inn] for m in res] == sorted(lots.values(), reverse=True)[:len(res)]
    top = lots[res[0].counterparty.inn]
    formula = {m.counterparty.inn: m.score for m in search(Q, config=plain_cfg, k=50)}
    scores = [m.score for m in res]
    assert scores == sorted(scores, reverse=True)  # the number never contradicts the order
    assert res[0].score == formula[res[0].counterparty.inn]  # the top keeps its formula score (D-077 scale)
    assert all(m.score <= formula[m.counterparty.inn] for m in res)
    fit = next(c for c in res[0].components if c.name == "product_fit")
    assert "почему на этом месте — модель логистическая регрессия, обученная на истории закупок" in fit.explanation
    n_top = round(math.expm1(top))
    assert f"за — лоты с похожими позициями: {n_top}" in fit.explanation  # the value, not only the name
    with pytest.raises(ValueError, match="ranker.kind"):
        search_mod.check_config({**cfg, "ranker": {"kind": "svm", "model": "x"}})


def test_missing_model_falls_back_to_v2(search_data, tmp_path):
    """No model file (or no lightgbm) never breaks the demo: the v2 order and a note saying so."""
    from src.match import fallback

    plain_cfg = {k: v for k, v in search_mod.load_config().items() if k != "ranker"}
    cfg = {**plain_cfg, "ranker": {"kind": "lgbm", "model": str(tmp_path / "absent.txt")}}
    assert search(Q, config=cfg) == search(Q, config=plain_cfg)
    search(Q, config=cfg)
    assert "ранжировщик недоступен" in (fallback.search_note() or "")


def test_value_text_reads_like_a_card():
    assert ranker.value_text("cust_lots", math.log1p(7)) == "лоты у этого заказчика: 7"
    assert ranker.value_text("days_last", math.log1p(240)) == "давность последнего участия: 240 дн."
    assert ranker.value_text("top_class_share", 0.85) == "доля главного класса ОКПД2: 85%"
    assert ranker.value_text("region_kpp", 1.0).endswith(": да")
    assert ranker.value_text("cust_lots", math.nan).endswith(": нет данных")
    row = np.zeros(len(ranker.NAMES))
    row[ranker.NAMES.index("cust_lots")] = math.log1p(7)
    row[ranker.NAMES.index("days_last")] = math.log1p(240)
    contrib = np.zeros(len(ranker.NAMES))
    contrib[ranker.NAMES.index("cust_lots")], contrib[ranker.NAMES.index("days_last")] = 0.9, -0.4
    text = ranker.explain(ranker.Model("lgbm", ranker.ROOT, {}), contrib, row)
    assert text.endswith(": за — лоты у этого заказчика: 7; против — давность последнего участия: 240 дн.")
    # a missing value is no reason: «нет данных» never shows up as «за» or «против» (review of #274)
    row[ranker.NAMES.index("channel_share")] = math.nan
    contrib[ranker.NAMES.index("channel_share")] = -2.0
    assert ranker.explain(ranker.Model("lgbm", ranker.ROOT, {}), contrib, row) == text


def test_ablate_feeds_features_as_missing():
    import numpy as np
    import pytest

    sp = ranker.spec({"ranker": {"kind": "logreg", "model": "m.json", "ablate": ["win_rate"]}})
    assert sp is not None and sp["ablate"] == ["win_rate"]
    with pytest.raises(ValueError):
        ranker.spec({"ranker": {"kind": "logreg", "model": "m.json", "ablate": ["no_such_feature"]}})
    n = len(ranker.NAMES)
    params = {"fill": np.full(n, 0.5), "mean": np.zeros(n), "std": np.ones(n), "coef": np.arange(n, dtype=float),
              "intercept": 0.0}
    full = ranker.Model("logreg", ranker.ROOT, params)
    i = ranker.NAMES.index("win_rate")
    ablated = ranker.Model("logreg", ranker.ROOT, params, ablate=(i,))
    x = np.ones((2, n))
    missing = x.copy()
    missing[:, i] = np.nan
    assert np.allclose(ablated.raw(x), full.raw(missing))
    assert not np.allclose(ablated.raw(x), full.raw(x))
    assert np.allclose(ablated.contrib(x)[:, i], 0.5 * i)  # missing -> fill, the same as for any NaN


def _lr_body(features: list[str]) -> dict:
    k = len(features)
    return {"features": features, "fill": [0.0] * k, "mean": [0.0] * k, "std": [1.0] * k,
            "coef": [float(i + 1) for i in range(k)], "intercept": 0.5}


def test_older_model_with_fewer_features_runs_on_its_columns(tmp_path):
    """A model trained before a feature was added (R-198: 43 of 44) keeps working (#105)."""
    old = ranker.NAMES[:-1]
    f = tmp_path / "old.json"
    f.write_text(json.dumps(_lr_body(old)), encoding="utf-8")
    model = ranker.load({"kind": "logreg", "model": str(f)})
    assert model.cols == list(range(len(old)))
    x = np.arange(len(ranker.NAMES), dtype=float).reshape(1, -1) + 1
    want = float(x[0, :len(old)] @ np.arange(1, len(old) + 1) + 0.5)
    assert model.raw(x)[0] == pytest.approx(want)
    c = model.contrib(x)
    assert c.shape == (1, len(ranker.NAMES))
    assert c[0, ranker.NAMES.index(ranker.NAMES[-1])] == 0.0
    assert c[0, 0] == pytest.approx(1.0 * x[0, 0])


def test_model_columns_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_lr_body([*ranker.NAMES[:3], "no_such_feature"])), encoding="utf-8")
    with pytest.raises(ValueError, match="no_such_feature"):
        ranker.load({"kind": "logreg", "model": str(bad)})
    empty = tmp_path / "empty.json"
    body = _lr_body([])
    body.pop("features")
    empty.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="списка признаков"):
        ranker.load({"kind": "logreg", "model": str(empty)})


def test_default_ranker_model_loads_and_ranks(search_data):
    """The product default (R-198, 43 of the 44 features) loads and orders the result, no fallback note."""
    from src.match import fallback

    cfg = search_mod.load_config()
    sp = ranker.spec(cfg)
    assert sp is not None
    model = ranker.load(sp)
    assert model.cols is None or [ranker.NAMES[i] for i in model.cols] == model.booster.feature_name()
    res = search(Q, config=cfg)
    assert res
    assert any(ranker.MARKER in c.explanation for m in res for c in m.components)
    assert "ранжировщик недоступен" not in (fallback.search_note() or "")


def test_data_without_supplier_profile_is_counted_and_warned_once(monkeypatch, caplog):
    """Data built before the profile (#114; the release data-2026-10-01) leaves its 16 features NaN for every
    candidate: the ranker then loses quality silently (dev recall@10 0.503 against 0.572, #421) — count and warn."""
    monkeypatch.setattr(ranker, "without_profile", 0)
    full = np.ones((3, len(ranker.NAMES)))
    ranker._check_profile(full)
    bare = full.copy()
    bare[:, ranker.PROFILE_IDX] = np.nan
    one = bare.copy()
    one[1, ranker.PROFILE_IDX[-1]] = 0.0  # one candidate with one profile key: the data has the profile
    with caplog.at_level(logging.WARNING, logger=ranker.__name__):
        ranker._check_profile(bare)
        ranker._check_profile(bare)
        ranker._check_profile(one)
    assert ranker.without_profile == 2
    assert sum("нет профиля поставщика" in r.getMessage() for r in caplog.records) == 1
    assert {ranker.NAMES[i] for i in ranker.PROFILE_IDX} == {*ranker.PROFILE_LOG, *ranker.PROFILE_RAW}


def test_fixture_data_has_the_supplier_profile(search_data, monkeypatch):
    """The current ingest (the fixture is built by it) gives every ranked query a profile: nothing is counted."""
    monkeypatch.setattr(ranker, "without_profile", 0)
    assert search(Q, config=search_mod.load_config())
    assert ranker.without_profile == 0


# the 12 columns of suppliers*.parquet in the release data-2026-10-01 (ingest before the profile, #114)
RELEASE_COLUMNS = ["inn", "kpp", "inn_valid", "lots_participated", "lots_won", "win_rate", "customers", "customers_spb",
                   "first_seen", "last_seen", "median_start_price", "top_okpd2"]


def test_search_on_data_built_before_the_profile_is_counted(fixture_processed, tmp_path, monkeypatch, caplog):
    """The whole chain on the release's columns: store → stats without the profile → features → rerank (#421)."""
    import shutil

    import pyarrow.parquet as pq

    from src.match import index

    old = tmp_path / "processed"
    shutil.copytree(fixture_processed, old)
    for f in old.glob("suppliers*.parquet"):
        pq.write_table(pq.read_table(f, columns=RELEASE_COLUMNS), f)
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(old))
    monkeypatch.setenv("DATA_STORE", "parquet")
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "indexes"))
    monkeypatch.setattr(ranker, "without_profile", 0)
    index.clear_memory_cache()
    with caplog.at_level(logging.WARNING, logger=ranker.__name__):
        assert search(Q, config=search_mod.load_config())
    index.clear_memory_cache()
    assert ranker.without_profile == 1
    assert sum("нет профиля поставщика" in r.getMessage() for r in caplog.records) == 1
