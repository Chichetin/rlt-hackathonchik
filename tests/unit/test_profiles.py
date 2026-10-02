"""Weight profiles by procurement type (#104): goods / services by the query's OKPD2, off by default."""

from __future__ import annotations

from datetime import date

import pytest

from src.contracts import Query
from src.match import search as search_mod
from src.match.profiles import GOODS, SERVICES, procurement_type, profile_overrides, profile_weights
from src.match.search import search

pytestmark = pytest.mark.usefixtures("v2_formula")  # the v2 formula; the ranker on top — test_ranker.py

CUTOFF = date(2025, 7, 1)
BASE = {"product_fit": 0.35, "experience": 0.2, "reliability": 0.2, "activity": 0.1, "geography": 0.1, "price": 0.05}
ON = {"weights_by_type": {"services": {"geography": 0.0}}}


@pytest.mark.parametrize(("codes", "want"), [
    (["01.13.11.000"], GOODS),          # section A, first class
    (["17.12.14.110"], GOODS),
    (["32.99.12.110"], GOODS),          # last goods class of section C
    (["33.12.1"], SERVICES),            # 33 — repair and installation services inside section C
    (["33"], SERVICES),
    (["35.11.10"], GOODS),              # section D: electricity, gas, steam — products
    (["36.00.20.130"], SERVICES),       # section E: water supply services
    (["43.21.10.140"], SERVICES),       # construction works
    (["58.29.50.000"], SERVICES),       # section J: software licences
    (["99.00"], SERVICES),
    ([" 61.10.11.110 "], SERVICES),     # whitespace is not a code
    (["17.12", "32.99.12"], GOODS),     # all positions are goods
    (["43.21", "33.12"], SERVICES),
    (["17.12", "33.12"], None),         # mixed lot -> default profile
    (["35.11", "36.00"], None),         # electricity is goods, water supply services: mixed
    ([], None),
    ([""], None),
    (["34.10"], None),                  # class 34 does not exist in OKPD2
    (["00.10"], None),
    (["abc"], None),
    (["7"], None),
    (["1712"], None),                   # no dot after the class: not an OKPD2 code
    (["17.12", "1712"], None),
])
def test_procurement_type_by_okpd2_class(codes, want):
    assert procurement_type(codes) == want


def test_profile_overrides_validate():
    assert profile_overrides({}) == {}
    assert profile_overrides({"weights_by_type": None}) == {}
    assert profile_overrides(ON) == {"services": {"geography": 0.0}}
    with pytest.raises(ValueError, match="works"):
        profile_overrides({"weights_by_type": {"works": {"geography": 0.0}}})
    with pytest.raises(ValueError, match="distance"):
        profile_overrides({"weights_by_type": {"services": {"distance": 0.0}}})
    with pytest.raises(ValueError):
        profile_overrides({"weights_by_type": {"services": {"geography": -0.1}}})
    with pytest.raises(ValueError):
        profile_overrides({"weights_by_type": {"services": {"geography": 1.5}}})
    with pytest.raises(ValueError):
        profile_overrides({"weights_by_type": {"services": 0.0}})


def test_off_is_identity():
    for codes in (["43.21"], ["17.12"], [], ["17.12", "33.12"]):
        assert profile_weights(BASE, {}, codes, predicted=False) == (BASE, "")
        assert profile_weights(BASE, {"weights_by_type": {}}, codes, predicted=False) == (BASE, "")


def test_services_profile_drops_geography():
    w, note = profile_weights(BASE, ON, ["43.21.10.140"], predicted=False)
    assert w == {**BASE, "geography": 0.0}
    assert BASE["geography"] == 0.1  # the base is not mutated
    assert note == "профиль весов «услуга» (ОКПД2 запроса, класс 43): география не учитывается"


def test_goods_profile_keeps_base_weights_and_says_so():
    w, note = profile_weights(BASE, ON, ["17.12.14.110"], predicted=True)
    assert w == BASE
    assert note == ("профиль весов «товар» (ОКПД2 определён по похожим позициям, класс 17): "
                    "география учитывается, вес 0.1")


def test_goods_profile_override_lists_changed_weights():
    cfg = {"weights_by_type": {"goods": {"geography": 0.05, "experience": 0.3}}}
    w, note = profile_weights(BASE, cfg, ["26.20"], predicted=False)
    assert w == {**BASE, "geography": 0.05, "experience": 0.3}
    assert note.endswith("география учитывается, вес 0.05; вес опыта 0.2 → 0.3")


def test_unknown_or_mixed_type_uses_default_and_says_why():
    w, note = profile_weights(BASE, ON, [], predicted=False)
    assert w == BASE and note == "профиль весов по умолчанию: ОКПД2 запроса не определён — тип закупки неизвестен"
    w, note = profile_weights(BASE, ON, ["17.12", "33.12"], predicted=False)
    assert w == BASE and note == "профиль весов по умолчанию: в запросе и товары, и работы/услуги (ОКПД2 17, 33)"
    w, note = profile_weights(BASE, ON, ["34.10"], predicted=False)
    assert w == BASE and note == "профиль весов по умолчанию: класс ОКПД2 34.10 не распознан — тип закупки неизвестен"


def test_default_config_has_no_profiles():
    assert profile_overrides(search_mod.load_config()) == {}


def test_check_config_rejects_bad_profile():
    cfg = {**search_mod.load_config(), "weights_by_type": {"works": {}}}
    with pytest.raises(ValueError):
        search_mod.check_config(cfg)


def _geo(m):
    return next(c for c in m.components if c.name == "geography")


def test_search_off_matches_v1(search_data):
    q = Query(query_id="q", text="ремонт кондиционеров", as_of=CUTOFF)
    cfg = search_mod.load_config()
    base = search(q, config=cfg)
    off = search(q, config={**cfg, "weights_by_type": {}})
    assert [m.model_dump() for m in off] == [m.model_dump() for m in base]
    assert all("профиль весов" not in c.explanation for m in base for c in m.components)


def _geo_weighted() -> dict:
    """The product config with geography weight 0.1 (D-077 has 0): the profile has something to change."""
    cfg = search_mod.load_config()
    return {**cfg, "weights": {**cfg["weights"], "geography": 0.1}}


def test_search_services_profile_zeroes_geography(search_data):
    q = Query(query_id="q", text="ремонт кондиционеров", as_of=CUTOFF)  # positions vote 33.12 -> services
    cfg = _geo_weighted()
    base = {m.counterparty.inn: m for m in search(q, config=cfg)}
    res = search(q, config={**cfg, **ON})
    assert res and set(base) == {m.counterparty.inn for m in res}
    for m in res:
        geo = _geo(m)
        assert geo.weight == 0.0
        assert geo.explanation.endswith("; профиль весов «услуга» (ОКПД2 определён по похожим позициям, класс 33): "
                                        "география не учитывается")
        assert geo.explanation.startswith(_geo(base[m.counterparty.inn]).explanation)
        assert m.score == pytest.approx(sum(c.value * c.weight for c in m.components), abs=1e-3)
        assert m.score == pytest.approx(base[m.counterparty.inn].score - 0.1 * geo.value, abs=1e-3)
    # the supplier from the neighbour region (KPP 47) loses its geography points
    assert _geo(base["4701000038"]).value == pytest.approx(0.8)


def test_search_goods_profile_keeps_scores(search_data):
    q = Query(query_id="q", text="бумага офисная", okpd2=["17.12.14.110"], as_of=CUTOFF)
    cfg = _geo_weighted()
    base = search(q, config=cfg)
    res = search(q, config={**cfg, **ON})
    assert [(m.counterparty.inn, m.score) for m in res] == [(m.counterparty.inn, m.score) for m in base]
    assert all(_geo(m).explanation.endswith("профиль весов «товар» (ОКПД2 запроса, класс 17): "
                                            "география учитывается, вес 0.1") for m in res)


def test_eval_slices_by_procurement_type(monkeypatch, tmp_path):
    from src.eval import run as R
    from src.eval.split import EvalCase

    assert R.slice_names({"channel": "ЭМ", "type": "services"}) == ["channel=ЭМ", "type=services"]
    assert R.slice_names({"type": "unknown"}) == ["type=unknown"]
    assert R.slice_names({"channel": "ЭМ"}) == ["channel=ЭМ"]  # old results without the type
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "c.sqlite"))
    monkeypatch.setattr(search_mod, "search", lambda q, k=20, weights=None, config=None: [])
    cases = [EvalCase(query=Query(query_id=f"c{i}", text="x", okpd2=codes), relevant_inns=["7801000012"],
                      date=date(2025, 7, 10))
             for i, codes in enumerate((["43.21"], ["17.12"], ["17.12", "33.12"], []))]
    body = R.evaluate(cases, search_mod.load_config())
    assert [q["type"] for q in body["per_query"]] == ["services", "goods", "unknown", "unknown"]
    assert {"type=goods", "type=services", "type=unknown"} <= set(body["slices"])
    assert body["slices"]["type=unknown"]["n_queries"] == 2


def test_compare_slices_by_type_against_older_result_and_shows_both_values(tmp_path, capsys):
    import json

    from src.eval import compare as C

    old = {"exp_id": "OLD", "part": "dev", "per_query": [  # a result from before #104: no "type"
        {"query_id": q, "recall@10": v, "mrr": v, "ndcg@10": v, "channel": "ЭМ"} for q, v in
        (("g1", 1.0), ("g2", 0.0), ("s1", 0.5))]}
    new = {"exp_id": "NEW", "part": "dev", "per_query": [
        {"query_id": q, "recall@10": v, "mrr": v, "ndcg@10": v, "channel": "ЭМ", "type": t} for q, v, t in
        (("s1", 1.0, "services"), ("g1", 1.0, "goods"), ("g2", 0.5, "goods"))]}
    for t in (C.paired(new, old), C.paired(old, new)):  # the type of either run defines the slice
        assert t["type=goods"]["n_queries"] == 2 and t["type=services"]["n_queries"] == 1
    t = C.paired(new, old)
    r = t["type=services"]["metrics"]["recall@10"]
    assert (r["a"], r["b"], r["delta"]) == (1.0, 0.5, 0.5)
    g = t["type=goods"]["metrics"]["recall@10"]
    assert (g["a"], g["b"], g["delta"]) == (0.75, 0.5, 0.25)
    for rec in (old, new):
        (tmp_path / f"{rec['exp_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    assert C.main(["NEW", "OLD"], results=tmp_path) == 0
    out = capsys.readouterr().out
    assert "type=services" in out and "0.500 → 1.000" in out


def test_profile_experiment_reproduces_its_recorded_config():
    """make eval EXP=H-104b gives the configuration stored in eval/results/H-104b.json (slide numbers)."""
    import json

    from src.eval import run as R
    from src.match.signals import signal_shares

    rec = json.loads((R.RESULTS / "H-104b.json").read_text(encoding="utf-8"))["config"]
    cfg = R.load_method_config("H-104b")
    assert cfg["weights"] == rec["weights"] and profile_overrides(cfg) == profile_overrides(rec)
    assert search_mod.scoring_options(cfg) == search_mod.scoring_options(rec)
    assert signal_shares(cfg) == signal_shares(rec)
    assert profile_overrides(cfg) == {"goods": {"geography": 0.05}}
