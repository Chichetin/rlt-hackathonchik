"""Learned ranker (#198): off = v2 unchanged, features deterministic, a model reorders and explains."""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

import numpy as np
import pytest

from src.contracts import Counterparty, CounterpartyMatch, Query, ScoreComponent
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
    # #576: the display formula (name match, pool experience, INN region) does not reach the model — its features
    # are those of the config without these options, and so are the shown values there
    legacy = {**cfg, "options": {**cfg["options"], "name_share": 0.0, "exp_scale": "fixed"},
              "signals": {**cfg["signals"], "inn_region": 0.0}}
    old: list = []
    plain = search(Q, config=legacy, features_out=old)
    assert np.array_equal(a, np.asarray([r for _, r in old]), equal_nan=True)
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


def _lots_model(tmp_path) -> dict:
    n = len(ranker.NAMES)
    coef = np.zeros(n)
    coef[ranker.NAMES.index("n_lots")] = 1.0  # rank by matched lots only
    model = tmp_path / "lr.json"
    mean, std = np.zeros(n), np.ones(n)
    mean[ranker.NAMES.index("n_lots")], std[ranker.NAMES.index("n_lots")] = 0.5, 2.0
    body = {"features": ranker.NAMES, "fill": [0.0] * n, "mean": mean.tolist(), "std": std.tolist(),
            "coef": coef.tolist(), "intercept": 0.0}
    model.write_text(json.dumps(body), encoding="utf-8")
    return {**search_mod.load_config(), "ranker": {"kind": "logreg", "model": str(model)}}


def test_model_reorders_and_explains(search_data, tmp_path):
    """model_weight 1.0: the score is the model's component alone — the order is the model's (as before #630)."""
    cfg = _lots_model(tmp_path)
    cfg["ranker"] = {**cfg["ranker"], "model_weight": 1.0}
    plain_cfg = {k: v for k, v in cfg.items() if k != "ranker"}
    got: list = []
    search(Q, config=plain_cfg, features_out=got)
    res = search(Q, config=cfg)
    lots = {inn: r[ranker.NAMES.index("n_lots")] for inn, r in got}
    assert [lots[m.counterparty.inn] for m in res] == sorted(lots.values(), reverse=True)[:len(res)]
    top = lots[res[0].counterparty.inn]
    assert all(m.score == round(sum(c.value * c.weight for c in m.components), 4) for m in res)
    assert all(c.weight == 0 for m in res for c in m.components if c.name != "model")
    assert res[0].score == 1.0  # the top of the model is 1.0 after min-max
    # why above the next one: n_lots is the only feature with a weight, so it is the reason whenever it differs
    ties = 0
    for a, b in zip(res, res[1:], strict=False):
        la, lb = round(math.expm1(lots[a.counterparty.inn])), round(math.expm1(lots[b.counterparty.inn]))
        va, vb = (next(c.value for c in m.components if c.name == "model") for m in (a, b))
        if la != lb and f"{va:.2f}" != f"{vb:.2f}":
            assert a.rank_reason == (f"выше следующего: оценка модели — {va:.2f} против {vb:.2f} "
                                     f"(лоты с похожими позициями — {la} против {lb})")
        elif la == lb:  # equal for the model and so for the score: the order is conditional, and the reason says so
            ties += 1
            assert a.score == b.score
            assert a.rank_reason == "релевантность и оценка модели равны — порядок условный (по ИНН)"
    assert ties > 0  # the fixture has suppliers with equal matched lots: the tie order is exercised
    assert res[-1].rank_reason is None
    fit = next(c for c in res[0].components if c.name == "product_fit")
    assert "почему на этом месте — модель логистическая регрессия, обученная на истории закупок" in fit.explanation
    n_top = round(math.expm1(top))
    assert f"за — лоты с похожими позициями: {n_top}" in fit.explanation  # the value, not only the name
    with pytest.raises(ValueError, match="ranker.kind"):
        search_mod.check_config({**cfg, "ranker": {"kind": "svm", "model": "x"}})


def test_model_is_the_seventh_component_and_the_order_is_the_score(search_data, tmp_path):
    """#630: the model's raw output, min-max within the answer, is the component «model» with weight W; the six
    formula weights are × (1 − W); score = Σ weight · value and the list never goes up by it."""
    cfg = _lots_model(tmp_path)
    plain_cfg = {k: v for k, v in cfg.items() if k != "ranker"}
    sp = ranker.spec(cfg)
    assert sp is not None and sp["model_weight"] == ranker.MODEL_WEIGHT == 0.5  # no key — the default
    res = search(Q, config=cfg)
    plain = {m.counterparty.inn: m for m in search(Q, config=plain_cfg, k=search_mod.CANDIDATES)}
    assert len(res) >= 3
    for m in res:
        comps = {c.name: c for c in m.components}
        assert len(m.components) == 7 and comps["model"].weight == 0.5
        assert 0.0 <= comps["model"].value <= 1.0
        assert re.fullmatch(r"оценка модели подбора \(44 признака истории закупок\): место \d+ из \d+",
                            comps["model"].explanation)
        assert m.score == round(sum(c.value * c.weight for c in m.components), 4)
        base = {c.name: c.weight for c in plain[m.counterparty.inn].components}
        assert all(c.weight == round(base[c.name] * 0.5, 4) for c in m.components if c.name != "model")
        assert m.score == pytest.approx(0.5 * plain[m.counterparty.inn].score + 0.5 * comps["model"].value, abs=1e-3)
    assert [m.score for m in res] == sorted((m.score for m in res), reverse=True)
    assert max(next(c.value for c in m.components if c.name == "model") for m in res) == 1.0
    assert any(c.explanation.endswith(f"место 1 из {len(plain)}") for m in res for c in m.components
               if c.name == "model")
    assert all(m.rank_reason for m in res[:-1]) and res[-1].rank_reason is None
    # W = 0: the model's component carries nothing, the order is the formula's score
    zero = search(Q, config={**cfg, "ranker": {**cfg["ranker"], "model_weight": 0.0}})
    assert [m.score for m in zero] == sorted((plain[m.counterparty.inn].score for m in zero), reverse=True)
    for bad in (1.5, -0.1, "half", True):
        with pytest.raises(ValueError, match="model_weight"):
            search_mod.check_config({**cfg, "ranker": {**cfg["ranker"], "model_weight": bad}})


def test_without_the_ranker_six_components_as_before(search_data, tmp_path):
    """No customer (customer_only) or no ranker: no «model» component, the score is the formula of the six."""
    cfg = _lots_model(tmp_path)
    cfg["ranker"] = {**cfg["ranker"], "customer_only": True}
    free = Q.model_copy(update={"customer_inn": None})
    for res in (search(free, config=cfg), search(Q, config={k: v for k, v in cfg.items() if k != "ranker"})):
        assert res
        assert all(len(m.components) == 6 and "model" not in {c.name for c in m.components} for m in res)
        assert all(m.score == round(sum(c.value * c.weight for c in m.components), 4) for m in res)


def test_rerank_normalizes_within_the_answer(search_data, tmp_path):
    """Equal raw outputs → every «model» is 1.0; the score order has the raw output as its tie-break."""
    model = ranker.load({"kind": "logreg", "model": str(_lots_model(tmp_path)["ranker"]["model"])})
    res = search(Q, config={k: v for k, v in _lots_model(tmp_path).items() if k != "ranker"})[:3]
    rows = [[0.0] * len(ranker.NAMES)] * len(res)
    out = ranker.rerank(model, res, rows, 3, 0.5)
    assert all(next(c for c in m.components if c.name == "model").value == 1.0 for m in out)
    assert [m.score for m in out] == sorted((m.score for m in out), reverse=True)


def _hand_match(name: str, fit: float) -> CounterpartyMatch:
    """Six components as search() gives them: product_fit with weight 1.0, the rest with weight 0."""
    comps = [ScoreComponent(name=n, value=fit if n == "product_fit" else 0.0, weight=1.0 if n == "product_fit" else 0.0,
                            explanation="x") for n in ("product_fit", "experience", "reliability", "price", "activity",
                                                       "geography")]
    return CounterpartyMatch(counterparty=Counterparty(inn=None, name=name), score=fit, components=comps,
                             origin="history")


def test_rerank_by_hand_score_order_tie_break_places_and_reasons():
    """raw = the first feature (a fixed logreg): A — best formula, worst model; B — best model; X — the score of A,
    higher model (the tie-break); C — the rest. W = 0.5: B 0.75, X 0.50, A 0.50, C 0.25."""
    n = len(ranker.NAMES)
    coef = np.zeros(n)
    coef[0] = 1.0
    model = ranker.Model("logreg", Path("hand.json"), {"fill": np.zeros(n), "mean": np.zeros(n), "std": np.ones(n),
                                                       "coef": coef, "intercept": 0.0})
    given = {"A": (1.0, 0.0), "B": (0.5, 10.0), "X": (0.5, 5.0), "C": (0.0, 5.0)}  # name: (formula, raw)
    matches = [_hand_match(k, f) for k, (f, _) in given.items()]
    rows = [[r] + [0.0] * (n - 1) for _, r in given.values()]
    out = ranker.rerank(model, matches, rows, 10, 0.5)
    assert [m.counterparty.name for m in out] == ["B", "X", "A", "C"]
    assert [m.score for m in out] == [0.75, 0.5, 0.5, 0.25]
    model_of = {m.counterparty.name: next(c for c in m.components if c.name == "model") for m in out}
    assert {k: c.value for k, c in model_of.items()} == {"A": 0.0, "B": 1.0, "X": 0.5, "C": 0.5}
    assert model_of["B"].explanation.endswith("место 1 из 4")
    assert model_of["X"].explanation.endswith("место 2 из 4") and model_of["C"].explanation.endswith("место 2 из 4")
    assert model_of["A"].explanation.endswith("место 4 из 4")
    assert all(c.weight == 0.5 for m in out for c in m.components if c.name == "product_fit")
    # the model's own reason is its feature («fit» here): the raw values, as the card shows them
    assert out[0].rank_reason == ("выше следующего: оценка модели — 1.00 против 0.50 "
                                  "(компонента «соответствие предмету» — 10.00 против 5.00)")
    assert out[1].rank_reason == "релевантность равна; выше оценка модели — 0.5000 против 0.0000"
    assert out[2].rank_reason == "выше следующего: компонента «соответствие предмету» — 1.00 против 0.00"
    assert out[3].rank_reason is None
    assert [m.counterparty.name for m in ranker.rerank(model, matches, rows, 2, 0.5)] == ["B", "X"]  # top-k


def test_next_reason_names_the_leading_component():
    def r(raw: float, parts: dict[str, float], values: dict[str, float]) -> ranker.Ranked:
        return ranker.Ranked(raw, round(sum(parts.values()), 4), np.full(len(ranker.NAMES), math.nan),
                             np.zeros(len(ranker.NAMES)), parts, values)

    a = r(0.1, {"experience": 0.4, "model": 0.1}, {"experience": 0.8, "model": 0.2})
    b = r(0.9, {"experience": 0.1, "model": 0.3}, {"experience": 0.2, "model": 0.6})
    # the formula put a above b though the model rated b higher: the reason is the formula's component
    assert ranker.next_reason(a, b) == "выше следующего: компонента «опыт» — 0.80 против 0.20"
    c = r(1.0, {"experience": 0.1, "model": 0.45}, {"experience": 0.2, "model": 0.9})
    assert ranker.next_reason(c, b) == "выше следующего: оценка модели — 0.90 против 0.60"
    same = r(0.5, {"experience": 0.3}, {"experience": 0.6, "model": 0.5})
    lower = r(0.4, {"experience": 0.3}, {"experience": 0.6, "model": 0.4})
    assert ranker.next_reason(same, lower) == "релевантность равна; выше оценка модели — 0.5000 против 0.4000"
    assert ranker.next_reason(same, same).endswith("порядок условный (по ИНН)")
    near = r(0.5, {"experience": 0.301}, {"experience": 0.6})
    assert ranker.next_reason(near, same) == "по совокупности компонент — 0.3010 против 0.3000"
    # a component where the upper one is worse is never its reason, however visible
    upper = r(0.5, {"experience": 0.301, "activity": 0.0}, {"experience": 0.6, "activity": 0.0})
    worse = r(0.5, {"experience": 0.3, "activity": 0.0}, {"experience": 0.6, "activity": 0.5})
    assert ranker.next_reason(upper, worse) == "по совокупности компонент — 0.3010 против 0.3000"


def test_missing_model_falls_back_to_v2(search_data, tmp_path):
    """No model file (or no lightgbm) never breaks the demo: the v2 order and a note saying so."""
    from src.match import fallback

    plain_cfg = {k: v for k, v in search_mod.load_config().items() if k != "ranker"}
    cfg = {**plain_cfg, "ranker": {"kind": "lgbm", "model": str(tmp_path / "absent.txt")}}
    assert search(Q, config=cfg) == search(Q, config=plain_cfg)
    search(Q, config=cfg)
    assert "ранжировщик недоступен" in (fallback.search_note() or "")
    assert all(m.rank_reason is None for m in ranker.reasons(search(Q, config=plain_cfg)))  # v2: no reasons


def test_no_customer_keeps_the_v2_order(search_data, tmp_path):
    """#490: the ranker learned only queries with a customer; free text without one goes in the v2 order."""
    old = _logreg_on_lots(tmp_path)
    cfg = {**old, "ranker": {**old["ranker"], "customer_only": True}}
    plain_cfg = {k: v for k, v in cfg.items() if k != "ranker"}
    assert search(Q, config=cfg) != search(Q, config=plain_cfg)  # with a customer the model reorders
    free = Q.model_copy(update={"customer_inn": None})
    assert search(free, config=cfg) == search(free, config=plain_cfg)
    assert search(free, config=old) != search(free, config=plain_cfg)  # without the key: as before #490
    got: list = []
    search(free, config=cfg, features_out=got)
    assert got  # training still gets the features
    assert search_mod.load_config()["ranker"].get("customer_only") is True  # the product default


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
    cfg["ranker"]["model_weight"] = 1.0  # the order is the model's: its feature is the reason (#630)
    res = search(Q, config=cfg)
    assert len(res) >= 3
    shown = ranker.reasons([res[0], *res[2:]])  # the filters removed the second one
    lots = {m.counterparty.inn: int(re.findall(r"(\d+) лот\(ов\)", next(
        c for c in m.components if c.name == "experience").explanation)[0]) for m in res}
    a, c = lots[res[0].counterparty.inn], lots[res[2].counterparty.inn]
    assert a != c  # precondition: otherwise the reason would not name the lots
    assert shown[0].rank_reason is not None
    assert shown[0].rank_reason.startswith("выше следующего: оценка модели — 1.00 против ")
    assert shown[0].rank_reason.endswith(f" (лоты с похожими позициями — {a} против {c})")
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
    a2 = _ranked(1.0, 0.9, {"cust_lots": math.nan, "act": 0.99}, {"cust_lots": 0.8, "act": 0.4})
    b2 = _ranked(0.5, 0.9, {"cust_lots": math.log1p(3), "act": 0.5}, {"cust_lots": 0.0, "act": 0.1})
    assert ranker.reason(a2, b2) == "выше следующего: компонента «активность» — 0.99 против 0.50"
    same = _ranked(0.4, 0.9996, wins, {})
    assert ranker.reason(same, _ranked(0.4, 0.9993, wins, {})) == (
        "оценка модели равна; выше релевантность — 0.9996 против 0.9993")
    assert ranker.reason(same, _ranked(0.4, 0.9996, wins, {})).endswith("порядок условный (по ИНН)")
    assert "по совокупности признаков" in ranker.reason(_ranked(1.0, 0.9, wins, {"fit": 0.5}),
                                                         _ranked(0.5, 0.9, wins, {"fit": 0.1}))
    # raw equal within SAME, but the formula of the upper is lower: no false «выше по релевантности»
    assert ranker.reason(_ranked(0.4 + 1e-12, 0.9993, wins, {}), _ranked(0.4, 0.9996, wins, {})) == (
        "оценка модели почти равна — порядок условный")


def test_reason_never_reads_as_a_minus_or_as_equal_values():
    """Review of #309: «стаж 313 дн. против 722 дн.» under «Выше следующего» reads as «less tenure is a plus»,
    «1.00 против 1.00» explains nothing, a feature without a reading (share of one customer) is no reason."""
    wins = {"n_wins": math.log1p(555), "days_first": math.log1p(313), "act": 0.9996, "top_customer_share": 0.9}
    nxt = {"n_wins": math.log1p(97), "days_first": math.log1p(722), "act": 0.9993, "top_customer_share": 0.2}
    contrib = {"days_first": 0.9, "act": 0.7, "top_customer_share": 0.6, "n_wins": 0.6}  # n_wins: 0.6 ≥ 0.2 · 2.8
    a, b = _ranked(1.0, 1.0, wins, contrib), _ranked(0.1, 1.0, nxt, {})
    assert ranker.reason(a, b) == "выше следующего: победы в лотах с похожими позициями — 555 против 97"
    # less is better for the days since the last participation
    a = _ranked(1.0, 1.0, {"days_last": math.log1p(3)}, {"days_last": 0.4})
    b = _ranked(0.1, 1.0, {"days_last": math.log1p(240)}, {})
    assert ranker.reason(a, b) == "выше следующего: давность последнего участия — 3 дн. против 240 дн."
    # the same for the last purchase with similar positions (#105): fewer days is the plus, more days is never one
    a = _ranked(1.0, 1.0, {"days_similar": math.log1p(10)}, {"days_similar": 0.4})
    b = _ranked(0.1, 1.0, {"days_similar": math.log1p(400)}, {})
    assert ranker.reason(a, b) == (
        "выше следующего: давность последней закупки с похожими позициями — 10 дн. против 400 дн.")
    assert "по совокупности признаков" in ranker.reason(
        _ranked(1.0, 1.0, {"days_similar": math.log1p(400)}, {"days_similar": 0.4}),
        _ranked(0.1, 1.0, {"days_similar": math.log1p(10)}, {}))
    # nothing the customer can read in a's favour
    only_worse = _ranked(1.0, 1.0, {"days_first": math.log1p(313)}, {"days_first": 0.9})
    longer = _ranked(0.1, 1.0, {"days_first": math.log1p(722)}, {})
    assert "по совокупности признаков" in ranker.reason(only_worse, longer)
    assert set(ranker.BETTER) <= set(ranker.NAMES) and "geo" not in ranker.BETTER  # geo: weight 0 (D-077)


def test_reason_is_a_main_one_and_in_a_favour():
    """Second review of #309: a feature better by the reading but counted by the model against `a` is no reason;
    a reason carries at least MIN_SHARE of the lift; less is better for the price gap."""
    against = _ranked(1.0, 1.0, {"n_wins": math.log1p(555), "act": 0.9},
                      {"n_wins": -0.2, "act": 0.5})  # more wins, but the model held them against a
    assert ranker.reason(against, _ranked(0.1, 1.0, {"n_wins": math.log1p(97), "act": 0.5}, {})) == (
        "выше следующего: компонента «активность» — 0.90 против 0.50")
    small = _ranked(1.0, 1.0, {"win_rate": 0.77, "act": 0.9}, {"days_first": 0.9, "win_rate": 0.1})
    other = _ranked(0.1, 1.0, {"win_rate": 0.42, "act": 0.9}, {})
    # 0.1 of 1.0: below MIN_SHARE — named as one of many, not as the reason
    assert ranker.reason(small, other) == "по совокупности признаков модели, в том числе: доля побед — 77% против 42%"
    tiny = _ranked(1.0, 1.0, {"win_rate": 0.77}, {"days_first": 0.97, "win_rate": 0.03})
    assert ranker.reason(tiny, other) == "по совокупности признаков модели"  # 3 % < MIN_MENTION: not named at all
    near, far = math.log(1.2), math.log(3.0)
    assert ranker.reason(_ranked(1.0, 1.0, {"price_gap": near}, {"price_gap": 0.5}),
                         _ranked(0.1, 1.0, {"price_gap": far}, {})) == (
        "выше следующего: разница НМЦК и типичной цены его лотов — в 1.2 раза против в 3.0 раза")
    assert ranker.reason(_ranked(1.0, 1.0, {"price_gap": far}, {"price_gap": 0.5}),
                         _ranked(0.1, 1.0, {"price_gap": near}, {})) == "по совокупности признаков модели"  # no plus


def test_value_text_reads_like_a_card():
    assert ranker.value_text("cust_lots", math.log1p(7)) == "лоты у этого заказчика: 7"
    assert ranker.value_text("days_last", math.log1p(240)) == "давность последнего участия: 240 дн."
    # #105: days since the last purchase with similar positions — days, not a bare log number
    assert ranker.value_text("days_similar", math.log1p(30)) == (
        "давность последней закупки с похожими позициями: 30 дн.")
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
