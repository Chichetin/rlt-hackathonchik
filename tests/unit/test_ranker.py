"""Learned ranker (#198): off = v2 unchanged, features deterministic, a model reorders and explains."""

from __future__ import annotations

import json
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
    cfg = search_mod.load_config()
    assert "ranker" not in cfg
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
    got: list = []
    search(Q, config={k: v for k, v in cfg.items() if k != "ranker"}, features_out=got)
    res = search(Q, config=cfg)
    lots = {inn: r[ranker.NAMES.index("n_lots")] for inn, r in got}
    assert [lots[m.counterparty.inn] for m in res] == sorted(lots.values(), reverse=True)[:len(res)]
    top = lots[res[0].counterparty.inn]
    assert res[0].score == round(1.0 / (1.0 + np.exp(-(top - 0.5) / 2.0)), 4)  # sigmoid of the standardized score
    fit = next(c for c in res[0].components if c.name == "product_fit")
    assert "ранжировщик (логистическая регрессия): главный вклад — лоты с похожими позициями" in fit.explanation
    with pytest.raises(ValueError, match="ranker.kind"):
        search_mod.check_config({**cfg, "ranker": {"kind": "svm", "model": "x"}})
