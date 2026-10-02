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
    # #278: the score stays the formula of the components — the relevance level, not the model's raw score
    plain = {m.counterparty.inn: m.score for m in search(Q, config=plain_cfg, k=search_mod.CANDIDATES)}
    assert all(m.score == plain[m.counterparty.inn] for m in res)
    assert all(m.score == round(sum(c.value * c.weight for c in m.components), 4) for m in res)
    # why above the next one: n_lots is the only feature with a weight, so it is the reason whenever it differs
    ties = 0
    for a, b in zip(res, res[1:], strict=False):
        la, lb = round(math.expm1(lots[a.counterparty.inn])), round(math.expm1(lots[b.counterparty.inn]))
        if la != lb:
            assert a.rank_reason == f"выше следующего: лоты с похожими позициями — {la} против {lb}"
        else:  # equal for the model: the formula decides (#278), and the reason says so truthfully
            ties += 1
            assert a.score >= b.score
            if a.score > b.score:
                assert a.rank_reason == (f"модель оценила одинаково со следующим; выше по релевантности — "
                                         f"{a.score:.4f} против {b.score:.4f}")
            else:
                assert a.rank_reason == "модель и релевантность одинаковы со следующим — порядок условный (по ИНН)"
    assert ties > 0  # the fixture has suppliers with equal matched lots: the tie order is exercised
    assert res[-1].rank_reason is None
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
    assert all(m.rank_reason is None for m in ranker.reasons(search(Q, config=plain_cfg)))  # v2: no reasons


def _logreg_on_lots(tmp_path) -> dict:
    n = len(ranker.NAMES)
    coef = np.zeros(n)
    coef[ranker.NAMES.index("n_lots")] = 1.0
    model = tmp_path / "lr.json"
    model.write_text(json.dumps({"features": ranker.NAMES, "fill": [0.0] * n, "mean": [0.0] * n, "std": [1.0] * n,
                                 "coef": coef.tolist(), "intercept": 0.0}), encoding="utf-8")
    return {**search_mod.load_config(), "ranker": {"kind": "logreg", "model": str(model)}}


def test_reasons_follow_the_visible_neighbour(search_data, tmp_path):
    """After the filters the next supplier on screen is another one: the reason compares with it (#278),
    and a later search without the model leaves no stale reasons."""
    cfg = _logreg_on_lots(tmp_path)
    res = search(Q, config=cfg)
    assert len(res) >= 3
    shown = ranker.reasons([res[0], *res[2:]])  # the filters removed the second one
    lots = {m.counterparty.inn: int(re.findall(r"(\d+) лот\(ов\)", next(
        c for c in m.components if c.name == "experience").explanation)[0]) for m in res}
    a, c = lots[res[0].counterparty.inn], lots[res[2].counterparty.inn]
    assert a != c  # precondition: otherwise the reason would not name the lots
    assert shown[0].rank_reason == f"выше следующего: лоты с похожими позициями — {a} против {c}"
    plain_cfg = {k: v for k, v in cfg.items() if k != "ranker"}
    assert all(m.rank_reason is None for m in ranker.reasons(search(Q, config=plain_cfg)))


def _ranked(raw: float, score: float, row: dict[str, float], contrib: dict[str, float]) -> ranker.Ranked:
    r, c = np.full(len(ranker.NAMES), math.nan), np.zeros(len(ranker.NAMES))
    for k, v in row.items():
        r[ranker.NAMES.index(k)] = v
    for k, v in contrib.items():
        c[ranker.NAMES.index(k)] = v
    return ranker.Ranked(raw, score, r, c)


def test_reason_names_a_feature_the_customer_can_check():
    wins = {"n_wins": math.log1p(771), "fit": 1.0}
    # fit is equal (1.00 and 1.00): its contribution differs only by interaction — skipped for n_wins
    a = _ranked(1.0, 0.9996, wins, {"fit": 0.9, "n_wins": 0.5})
    b = _ranked(0.2, 0.9996, {"n_wins": math.log1p(515), "fit": 1.0}, {"fit": 0.3, "n_wins": 0.1})
    assert ranker.reason(a, b) == "выше следующего: победы в лотах с похожими позициями — 771 против 515"
    # a missing value is not a reason either
    a2 = _ranked(1.0, 0.9, {"cust_lots": math.nan, "act": 0.99}, {"cust_lots": 0.8, "act": 0.2})
    b2 = _ranked(0.5, 0.9, {"cust_lots": math.log1p(3), "act": 0.5}, {"cust_lots": 0.0, "act": 0.1})
    assert ranker.reason(a2, b2) == "выше следующего: компонента «активность» — 0.99 против 0.50"
    same = _ranked(0.4, 0.9996, wins, {})
    assert ranker.reason(same, _ranked(0.4, 0.9993, wins, {})) == (
        "модель оценила одинаково со следующим; выше по релевантности — 0.9996 против 0.9993")
    assert ranker.reason(same, _ranked(0.4, 0.9996, wins, {})).endswith("порядок условный (по ИНН)")
    assert "по совокупности признаков" in ranker.reason(_ranked(1.0, 0.9, wins, {"fit": 0.5}),
                                                         _ranked(0.5, 0.9, wins, {"fit": 0.1}))
    # raw equal within SAME, but the formula of the upper is lower: no false «выше по релевантности»
    assert ranker.reason(_ranked(0.4 + 1e-12, 0.9993, wins, {}), _ranked(0.4, 0.9996, wins, {})) == (
        "модель оценила почти одинаково со следующим — порядок условный")


def test_reason_never_reads_as_a_minus_or_as_equal_values():
    """Review of #309: «стаж 313 дн. против 722 дн.» under «Выше следующего» reads as «less tenure is a plus»,
    «1.00 против 1.00» explains nothing, a feature without a reading (share of one customer) is no reason."""
    wins = {"n_wins": math.log1p(555), "days_first": math.log1p(313), "act": 0.9996, "top_customer_share": 0.9}
    nxt = {"n_wins": math.log1p(97), "days_first": math.log1p(722), "act": 0.9993, "top_customer_share": 0.2}
    contrib = {"days_first": 0.9, "act": 0.7, "top_customer_share": 0.6, "n_wins": 0.3}
    a, b = _ranked(1.0, 1.0, wins, contrib), _ranked(0.1, 1.0, nxt, {})
    assert ranker.reason(a, b) == "выше следующего: победы в лотах с похожими позициями — 555 против 97"
    # less is better for the days since the last participation
    a = _ranked(1.0, 1.0, {"days_last": math.log1p(3)}, {"days_last": 0.4})
    b = _ranked(0.1, 1.0, {"days_last": math.log1p(240)}, {})
    assert ranker.reason(a, b) == "выше следующего: давность последнего участия — 3 дн. против 240 дн."
    # nothing the customer can read in a's favour
    only_worse = _ranked(1.0, 1.0, {"days_first": math.log1p(313)}, {"days_first": 0.9})
    longer = _ranked(0.1, 1.0, {"days_first": math.log1p(722)}, {})
    assert "по совокупности признаков" in ranker.reason(only_worse, longer)
    assert set(ranker.BETTER) <= set(ranker.NAMES)


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
