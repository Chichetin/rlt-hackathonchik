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
