"""reliability (#72): history signals before as_of, registry part for suppliers without history, the switch.

Fixture (tests/fixtures/ingest), supplier 7801000012: wins lots 9 (2024-01-20, customer 7801000051),
1 (2024-03-01, 7801000051), 3 (2024-11-05, 7701000040); loses 6 (2025-08-01) and 7 (2025-10-15),
both after the cutoff 2025-07-01. Supplier 4701000038: loses 3 (7701000040), wins 4 (2025-02-14, 7801000051).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import yaml

from src.contracts import Counterparty, Evidence, Query
from src.match import search as search_mod
from src.match.reliability import (
    HISTORY_PARTS,
    NO_HISTORY,
    SHADOW_NOTE,
    History,
    Prior,
    history_parts,
    history_reliability,
    ogrn_year,
    plural,
    pool_prior,
    registry_reliability,
    reliability_mode,
    smooth,
    spearman,
)
from src.match.search import search
from src.match.store import ParquetStore

CUTOFF = date(2025, 7, 1)
PRIOR = Prior(win_rate=0.4, retention=0.3)
REF = date(2025, 7, 1)


def h(lots=10, wins=4, customers=5, won=3, rep=1, months=6, first=date(2024, 7, 1)) -> History:
    return History(lots=lots, wins=wins, customers=customers, won_customers=won, repeat_customers=rep,
                   active_months=months, first_seen=first)


def rel(m):
    return next(c for c in m.components if c.name == "reliability")


# --- signals --------------------------------------------------------------------------------------


def test_smoothing_pulls_small_samples_to_the_prior():
    assert smooth(1, 1, 0.4, 5) == pytest.approx((1 + 2) / 6)  # 1 of 1 is not 1.0
    assert smooth(0, 0, 0.4, 5) == pytest.approx(0.4)
    assert smooth(100, 100, 0.4, 5) > smooth(10, 10, 0.4, 5) > smooth(1, 1, 0.4, 5) > 0.4
    assert smooth(0, 10, 0.4, 5) < 0.4


def test_pool_prior_is_the_pooled_rate_of_the_candidates():
    p = pool_prior([h(lots=10, wins=5, won=4, rep=1), h(lots=30, wins=5, won=2, rep=2)])
    assert p.win_rate == pytest.approx(10 / 40) and p.retention == pytest.approx(3 / 6)
    assert pool_prior([]) == Prior(0.0, 0.0)


def test_each_signal_moves_the_value_up():
    base = history_reliability(h(), PRIOR, REF)[0]
    assert history_reliability(h(wins=8), PRIOR, REF)[0] > base  # wins more
    assert history_reliability(h(rep=3), PRIOR, REF)[0] > base  # customers came back
    assert history_reliability(h(customers=15), PRIOR, REF)[0] > base  # broader
    assert history_reliability(h(months=12), PRIOR, REF)[0] > base  # more regular
    assert history_reliability(h(first=date(2025, 4, 1), months=3), PRIOR, REF)[0] < history_reliability(
        h(first=date(2024, 1, 1), months=3 * 18 // 3), PRIOR, REF)[0]  # longer in the data


def test_parts_are_in_unit_interval_and_saturate():
    big = history_parts(h(lots=1000, wins=1000, customers=500, won=500, rep=500, months=19,
                          first=date(2024, 1, 1)), PRIOR, REF)
    assert set(big) == set(HISTORY_PARTS) and all(0.0 <= v <= 1.0 for v in big.values())
    assert big["breadth"] == 1.0 and big["tenure"] == 1.0
    assert history_parts(h(customers=20), PRIOR, REF)["breadth"] == 1.0
    assert history_parts(h(customers=19), PRIOR, REF)["breadth"] < 1.0
    # tenure: a year in the data saturates the length half; regularity = active / calendar months
    t = history_parts(h(first=date(2024, 8, 1), months=6), PRIOR, REF)["tenure"]
    assert t == pytest.approx(0.5 * 1.0 + 0.5 * 6 / 12)
    t = history_parts(h(first=date(2025, 1, 1), months=7), PRIOR, REF)["tenure"]
    assert t == pytest.approx(0.5 * 7 / 12 + 0.5 * 1.0)
    assert sum(HISTORY_PARTS.values()) == pytest.approx(1.0)


def test_value_is_the_weighted_sum_and_explained():
    x = h()
    value, why = history_reliability(x, PRIOR, REF)
    s = history_parts(x, PRIOR, REF)
    assert value == pytest.approx(sum(HISTORY_PARTS[k] * s[k] for k in HISTORY_PARTS))
    assert "Побед 4 из 10 лотов" in why and "у похожих поставщиков 40%" in why
    assert "повторные победы у 1 из 3 заказчиков" in why and "всего заказчиков 5" in why
    _, why = history_reliability(h(lots=21, won=1, rep=0, wins=1), PRIOR, REF)
    assert "из 21 лота" in why and "у 0 из 1 заказчика" in why and "(ов)" not in why
    assert "в данных с 07.2024, активен 6 из 13 мес." in why


def test_no_history_is_no_data_not_a_value():
    assert history_reliability(None, PRIOR, REF) == (0.0, NO_HISTORY)
    assert history_reliability(h(lots=0, wins=0), PRIOR, REF) == (0.0, NO_HISTORY)


# --- registry part (suppliers without history) ----------------------------------------------------


def _ev(field: str, value: str) -> Evidence:
    return Evidence(source="ФНС", url="https://pb.nalog.ru/x", fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
                    fact=f"{field} {value}", field=field, value=value)


def test_registry_unknown_is_not_checked_not_zero():
    value, why = registry_reliability(Counterparty(inn="7801000012", name="ИНН 7801000012"), REF)
    assert value is None and why.startswith("Не проверено")


def test_registry_uses_only_known_facts_renormalized():
    ok = Counterparty(inn="7801000012", name="x", status="Действующее", in_rnp=False,
                      evidence=[_ev("ogrn", "1157847000001")])  # record of 2015 → 10 years
    value, why = registry_reliability(ok, REF)
    assert value == pytest.approx(1.0) and "2015" in why and "в РНП не найден" in why
    only_status = Counterparty(inn="7801000012", name="x", status="действующая")
    value, why = registry_reliability(only_status, REF)
    assert value == pytest.approx(1.0) and "не проверено: РНП, возраст" in why
    young = Counterparty(inn="7801000012", name="x", status="Действующее",
                         evidence=[_ev("ogrn", "1247800000001")])  # 2024 → 1 year
    value, _ = registry_reliability(young, REF)
    assert value == pytest.approx((0.4 * 1.0 + 0.3 * 1 / 5) / 0.7)
    bad = Counterparty(inn="7801000012", name="x", status="Ликвидируется", in_rnp=True)
    assert registry_reliability(bad, REF)[0] == pytest.approx(0.0)


def test_ogrn_year():
    assert ogrn_year("1157847000001", REF) == 2015
    assert ogrn_year("304780000000001", REF) == 2004  # ОГРНИП, 15 digits
    assert ogrn_year("1997847000001", REF) is None  # 2099 is in the future
    assert ogrn_year("1017847000001", REF) is None  # before 2002 there was no ОГРН
    assert ogrn_year("115784700000", REF) is None and ogrn_year("11578470000AB", REF) is None


# --- store: aggregates strictly before as_of -------------------------------------------------------


def test_supplier_history_counts_only_before_as_of(fixture_processed):
    store = ParquetStore(fixture_processed)
    got = store.supplier_history(["7801000012", "4701000038", "0000000000"], CUTOFF)
    assert got["7801000012"] == History(lots=3, wins=3, customers=2, won_customers=2, repeat_customers=1,
                                        active_months=3, first_seen=date(2024, 1, 20))
    assert got["4701000038"] == History(lots=2, wins=1, customers=2, won_customers=1, repeat_customers=0,
                                        active_months=2, first_seen=date(2024, 11, 5))
    assert "0000000000" not in got
    full = store.supplier_history(["7801000012"], None)["7801000012"]
    assert (full.lots, full.wins, full.active_months) == (5, 3, 5)  # lots 6 and 7 after the cutoff


# --- the switch in search() ------------------------------------------------------------------------


@pytest.fixture
def mode(monkeypatch):
    def apply(value: str):
        cfg = search_mod.load_config()
        weights = {**cfg["weights"], "reliability": 0.2} if value == "on" else cfg["weights"]
        monkeypatch.setattr(search_mod, "load_config",
                            lambda: {**cfg, "weights": weights, "reliability": {"mode": value}})
    return apply


def test_default_mode_is_shadow_and_invalid_is_an_error():
    assert reliability_mode(search_mod.load_config()) == "shadow"
    assert reliability_mode({}) == "off"
    # YAML 1.1 reads bare on / off as booleans
    assert reliability_mode(yaml.safe_load("reliability: {mode: on}")) == "on"
    assert reliability_mode(yaml.safe_load("reliability: {mode: off}")) == "off"
    with pytest.raises(ValueError):
        reliability_mode({"reliability": {"mode": "maybe"}})
    with pytest.raises(ValueError, match="reliability.mode"):  # a bad config fails when loaded, not mid-search
        search_mod.check_config({**search_mod.load_config(), "reliability": {"mode": "maybe"}})


def test_sanity_check_reads_the_store_up_to_the_holdout(search_data):
    """`python -m src.match.reliability` goes through the Store (#293): suppliers before the cutoff, outcomes in
    [cutoff, holdout_from) — 7801000012 has lot 6 (2025-08-01) there; lot 7 (2025-10-15) is holdout, never read."""
    from src.match.reliability import check, outcomes
    from src.match.store import get_store

    rec = check(min_after=1, quantiles=1)
    assert (rec["cutoff"], rec["outcome_until"]) == ("2025-07-01", "2025-10-01")
    assert rec["suppliers_before"] == 5 and rec["with_min_lots_after"] == 2
    inns, after = outcomes(get_store(), CUTOFF, date(2025, 10, 1))
    assert len(inns) == 5 and "7801000012" in inns
    assert after["7801000012"] == (1, 0)  # lot 6 lost; lot 7 is holdout
    assert outcomes(get_store(), CUTOFF, date(2025, 12, 31))[1]["7801000012"] == (2, 0)  # it is in the data


def _ranking(res):
    return [(m.counterparty.inn, m.score) for m in res]


def test_shadow_shows_value_but_keeps_score_and_order(search_data, mode):
    """The product default: the learned ranker sets the order (D-103), shadow fills the shown top-k after it."""
    assert search_mod.load_config().get("ranker")
    q = Query(query_id="q", text="бумага офисная", as_of=CUTOFF)
    mode("off")
    off = search(q)
    assert all(rel(m).value == 0.0 and rel(m).explanation.startswith("Нет данных") for m in off)
    assert all(rel(m).explanation == search_mod.UNAVAILABLE["reliability"] for m in off)  # off = D-029, no history
    mode("shadow")
    shadow = search(q)
    assert _ranking(shadow) == _ranking(off)
    assert all(rel(m).weight == 0.0 and rel(m).explanation.endswith(SHADOW_NOTE) for m in shadow)
    top = next(m for m in shadow if m.counterparty.inn == "7801000012")
    assert rel(top).value > 0 and "Побед 3 из 3" in rel(top).explanation  # lots 6, 7 are after as_of


def test_on_adds_the_weighted_component_to_the_score(search_data, v2_formula, mode):
    """The formula itself (no ranker: under D-103 the shown score is capped by the one above)."""
    q = Query(query_id="q", text="бумага офисная", as_of=CUTOFF)
    mode("off")
    off = {m.counterparty.inn: m for m in search(q, k=200)}
    mode("on")
    on = search(q, k=200)
    assert on
    for m in on:
        r = rel(m)
        assert r.weight == pytest.approx(0.2) and not r.explanation.endswith(SHADOW_NOTE)
        assert m.score == pytest.approx(off[m.counterparty.inn].score + 0.2 * r.value, abs=2e-4)


def test_spearman_handles_ties():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 1, 2, 2], [1, 1, 2, 2]) == pytest.approx(1.0)


def test_months_count_to_the_day_before_as_of(search_data):
    m = next(m for m in search(Query(query_id="q", text="бумага офисная", as_of=CUTOFF))
             if m.counterparty.inn == "7801000012")
    assert "в данных с 01.2024, активен 3 из 18 мес." in rel(m).explanation  # 01.2024 … 06.2025


def test_no_wins_yet_is_said_in_words():
    _, why = history_reliability(h(wins=0, won=0, rep=0), PRIOR, REF)
    assert "побед у заказчиков ещё не было" in why and "из 0" not in why


@pytest.mark.parametrize("exp", ["H-15b", "H-15c", "H-15d-tie", "H-15e", "H-15f", "H-15g"])
def test_shadow_keeps_score_and_order_under_experiment_options(search_data, exp):
    """main's scoring options (#22: experience_by, fit_bm25_share, tie_break, signals) — shadow changes nothing."""
    from src.eval.run import load_method_config

    base = load_method_config(exp)
    q = Query(query_id="q", text="бумага офисная", as_of=CUTOFF, customer_inn="7801000051")
    off = search(q, k=200, config={**base, "reliability": {"mode": "off"}})
    shadow = search(q, k=200, config={**base, "reliability": {"mode": "shadow"}})
    assert off and _ranking(shadow) == _ranking(off)
    assert any(rel(m).value > 0 for m in shadow)  # the value is computed, only not weighted


def test_rel_on_experiment_is_reproducible_from_the_repository():
    """rel-on-s500 (research/EXPERIMENTS.md) is an entry of configs/experiments.yaml, not a tree copy."""
    from src.eval.run import load_method_config

    for exp in ("rel-on-s500", "rel-on-f"):
        cfg = search_mod.check_config(load_method_config(exp))
        assert reliability_mode(cfg) == "on" and cfg["weights"]["reliability"] == pytest.approx(0.2), exp
    v1, f = load_method_config("rel-on-s500"), load_method_config("rel-on-f")
    assert v1["options"]["experience_by"] == "class" and f["options"] == load_method_config("H-15f")["options"]
    for exp in ("rel-off-s500", "rel-off-f"):
        assert reliability_mode(load_method_config(exp)) == "shadow"
    assert reliability_mode(search_mod.load_config()) == "shadow"  # the product stays in shadow


def test_plural_forms():
    forms = ("лот", "лота", "лотов")
    assert [plural(n, *forms) for n in (0, 1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 101, 111)] == [
        "лотов", "лот", "лота", "лота", "лотов", "лотов", "лотов", "лотов", "лот", "лота", "лотов", "лот", "лотов"]


def test_shadow_reads_history_only_for_the_shown_top_k(search_data, mode, monkeypatch):
    """#127 review: in shadow the history SQL covers the k shown suppliers, not all 200 candidates."""
    from src.match.store import ParquetStore

    calls: list[int] = []
    real = ParquetStore.supplier_history
    monkeypatch.setattr(ParquetStore, "supplier_history", lambda self, inns, as_of: calls.append(len(inns))
                        or real(self, inns, as_of))
    q = Query(query_id="q", text="бумага офисная", as_of=CUTOFF)
    mode("shadow")
    res = search(q, k=2)
    assert calls == [2] and len(res) == 2 and all(rel(m).value > 0 for m in res)
    calls.clear()
    mode("on")
    assert len(search(q, k=2)) == 2 and calls and calls[0] > 2  # on: every candidate is scored with it
    calls.clear()
    mode("off")
    search(q, k=2)
    assert calls == []


def test_shadow_covers_suppliers_with_an_invalid_inn(search_data, mode):
    mode("shadow")
    res = search(Query(query_id="q", text="бумага офисная", as_of=CUTOFF), k=200)
    bad = [m for m in res if m.counterparty.inn is None]
    assert bad and all(rel(m).value > 0 and rel(m).explanation.startswith("Побед") for m in bad)
