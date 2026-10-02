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
from src.match.search import search, shadow_reliability
from src.match.store import ParquetStore

CUTOFF = date(2025, 7, 1)
PRIOR = Prior(win_rate=0.4, retention=0.3)
REF = date(2025, 7, 1)


def h(lots=10, wins=4, customers=5, won=3, rep=1, months=6, first=date(2024, 7, 1), em_lots=None,
      em_wins=None) -> History:
    """All lots in ЭМ unless said otherwise."""
    return History(lots=lots, wins=wins, customers=customers, won_customers=won, repeat_customers=rep,
                   active_months=months, first_seen=first, em_lots=lots if em_lots is None else em_lots,
                   em_wins=wins if em_wins is None else em_wins)


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
    # the win rate pools ЭМ only: АИС ГЗ rows are winners only (is_winner = 1 for all of them)
    p = pool_prior([h(lots=12, wins=7, em_lots=10, em_wins=5), h(lots=5, wins=5, em_lots=0, em_wins=0)])
    assert p.win_rate == pytest.approx(5 / 10)


def test_win_rate_counts_only_em_lots():
    """АИС ГЗ in the data has winners only (is_winner = 1 in all 279 031 rows): 100 % there is by construction."""
    only_aisgz = h(lots=2, wins=2, em_lots=0, em_wins=0)
    s = history_parts(only_aisgz, PRIOR, REF)
    assert "win_rate" not in s  # the part is skipped, the others are renormalized
    value, why = history_reliability(only_aisgz, PRIOR, REF)
    rest = {k: w for k, w in HISTORY_PARTS.items() if k != "win_rate"}
    assert value == pytest.approx(sum(w * s[k] for k, w in rest.items()) / sum(rest.values()))
    assert why.startswith("В АИС ГЗ в данных только победители: 2 контракта") and "Побед" not in why
    # mixed: the rate is the ЭМ one, the АИС ГЗ contracts are said separately
    mixed = h(lots=13, wins=6, em_lots=10, em_wins=3)
    value, why = history_reliability(mixed, PRIOR, REF)
    assert history_parts(mixed, PRIOR, REF)["win_rate"] == pytest.approx(smooth(3, 10, PRIOR.win_rate, 5))
    assert why.startswith("Побед в ЭМ 3 из 10 лотов") and "в АИС ГЗ 3 контракта (в данных только победители)" in why
    # АИС ГЗ wins never lift the rate: the same ЭМ record with or without 50 АИС ГЗ contracts
    assert history_parts(h(lots=60, wins=53, em_lots=10, em_wins=3), PRIOR, REF)["win_rate"] == pytest.approx(
        history_parts(mixed, PRIOR, REF)["win_rate"])


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
    assert "Побед в ЭМ 4 из 10 лотов" in why and "в среднем по ЭМ 40%" in why and "АИС ГЗ" not in why
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
                                        active_months=3, first_seen=date(2024, 1, 20), em_lots=3, em_wins=3)
    assert got["4701000038"] == History(lots=2, wins=1, customers=2, won_customers=1, repeat_customers=0,
                                        active_months=2, first_seen=date(2024, 11, 5), em_lots=2, em_wins=1)
    assert "0000000000" not in got
    full = store.supplier_history(["7801000012"], None)["7801000012"]
    assert (full.lots, full.wins, full.active_months) == (5, 3, 5)  # lots 6 and 7 after the cutoff
    # 7801000020: lot 1 lost in ЭМ, lots 2 and 5 — АИС ГЗ, winners only
    mixed = store.supplier_history(["7801000020"], CUTOFF)["7801000020"]
    assert (mixed.lots, mixed.wins, mixed.em_lots, mixed.em_wins, mixed.aisgz) == (3, 2, 1, 0, 2)


def test_prior_pools_every_supplier_before_as_of(fixture_processed):
    """Store.reliability_prior = pool_prior over every supplier before as_of: one prior for every query, page and
    export — a supplier's reliability does not depend on where it is shown."""
    store = ParquetStore(fixture_processed)
    inns = ["470100000088", "4701000038", "780100000090", "7801000012", "7801000013", "7801000020"]  # all of them
    everyone = store.supplier_history(inns, CUTOFF)
    p = store.reliability_prior(CUTOFF)
    assert p == pool_prior(everyone.values())
    assert p.win_rate == pytest.approx(4 / 9)  # ЭМ before the cutoff: 9 lots × supplier, 4 won; АИС ГЗ not counted
    assert store.reliability_prior(None) != p  # lots after the cutoff count without as_of


# --- the switch in search() ------------------------------------------------------------------------


@pytest.fixture
def mode(monkeypatch):
    def apply(value: str):
        cfg = search_mod.load_config()
        weights = {**cfg["weights"], "reliability": 0.2} if value == "on" else cfg["weights"]
        monkeypatch.setattr(search_mod, "load_config",
                            lambda: {**cfg, "weights": weights, "reliability": {"mode": value},
                                     "options": {**cfg["options"], "price": "none"}})  # price weight not spread (#585)
    return apply


def test_default_mode_is_shadow_and_invalid_is_an_error():
    assert reliability_mode(search_mod.load_config()) == "on"  # #585
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
    assert (rec["cutoff"], rec["outcome_until"], rec["outcome"]) == ("2025-07-01", "2025-10-01", "ЭМ")
    assert rec["suppliers_before"] == 5 and rec["with_min_em_lots_after"] == 2
    inns, after = outcomes(get_store(), CUTOFF, date(2025, 10, 1))
    assert len(inns) == 5 and "7801000012" in inns
    a = after["7801000012"]
    assert (a.lots, a.wins, a.em_lots, a.em_wins) == (1, 0, 1, 0)  # lot 6 lost in ЭМ; lot 7 is holdout
    a = outcomes(get_store(), CUTOFF, date(2025, 12, 31))[1]["7801000012"]
    assert (a.lots, a.em_lots) == (2, 2)  # lot 7 is in the data


def _ranking(res):
    return [(m.counterparty.inn, m.score) for m in res]


def test_shadow_shows_value_but_keeps_score_and_order(search_data, mode):
    """The product default: the learned ranker sets the order (D-103); search() leaves shadow alone, the page the
    user sees gets it (shadow_reliability) — score and order as with off."""
    assert search_mod.load_config().get("ranker")
    q = Query(query_id="q", text="бумага офисная", as_of=CUTOFF)
    mode("off")
    off = search(q)
    assert all(rel(m).value == 0.0 and rel(m).explanation.startswith("Нет данных") for m in off)
    assert all(rel(m).explanation == search_mod.UNAVAILABLE["reliability"] for m in off)  # off = D-029, no history
    assert shadow_reliability(off, CUTOFF) == off  # off: nothing to fill
    mode("shadow")
    found = search(q)
    assert _ranking(found) == _ranking(off) and [rel(m) for m in found] == [rel(m) for m in off]
    shadow = shadow_reliability(found, CUTOFF)
    assert _ranking(shadow) == _ranking(off)
    assert all(rel(m).weight == 0.0 and rel(m).explanation.endswith(SHADOW_NOTE) for m in shadow)
    top = next(m for m in shadow if m.counterparty.inn == "7801000012")
    assert rel(top).value > 0 and "Побед в ЭМ 3 из 3" in rel(top).explanation  # lots 6, 7 are after as_of


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
    m = next(m for m in shadow_reliability(search(Query(query_id="q", text="бумага офисная", as_of=CUTOFF)), CUTOFF)
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
    cfg = {**base, "reliability": {"mode": "shadow"}}
    shadow = shadow_reliability(search(q, k=200, config=cfg), CUTOFF, cfg)
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
    assert reliability_mode(search_mod.load_config()) == "on"  # #585: in the score


def test_plural_forms():
    forms = ("лот", "лота", "лотов")
    assert [plural(n, *forms) for n in (0, 1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 101, 111)] == [
        "лотов", "лот", "лота", "лота", "лотов", "лотов", "лотов", "лотов", "лот", "лота", "лотов", "лот", "лотов"]


def test_shadow_reads_history_only_for_the_shown_top_k(search_data, mode, monkeypatch):
    """#127 review: in shadow search() reads no history at all (the API asks for ≤ 200 candidates); the history
    SQL covers only the rows shown — and the prior is one query per data snapshot and as_of."""
    from src.match.store import ParquetStore

    calls: list[int] = []
    priors: list[object] = []
    real, real_prior = ParquetStore.supplier_history, ParquetStore.reliability_prior
    monkeypatch.setattr(ParquetStore, "supplier_history", lambda self, inns, as_of: calls.append(len(inns))
                        or real(self, inns, as_of))
    monkeypatch.setattr(ParquetStore, "reliability_prior", lambda self, as_of: priors.append(as_of)
                        or real_prior(self, as_of))
    monkeypatch.setattr(search_mod, "_priors", {})
    q = Query(query_id="q", text="бумага офисная", as_of=CUTOFF)
    mode("shadow")
    found = search(q, k=200)
    assert calls == [] and len(found) > 2
    page = shadow_reliability(found[:2], CUTOFF)
    assert calls == [2] and all(rel(m).value > 0 for m in page)
    assert shadow_reliability(found[2:4], CUTOFF) and calls == [2, 2] and priors == [CUTOFF]  # prior cached
    calls.clear()
    mode("on")
    assert len(search(q, k=2)) == 2 and calls and calls[0] > 2  # on: every candidate is scored with it
    calls.clear()
    mode("off")
    search(q, k=2)
    assert calls == []


def test_shadow_covers_suppliers_with_an_invalid_inn(search_data, mode):
    mode("shadow")
    res = shadow_reliability(search(Query(query_id="q", text="бумага офисная", as_of=CUTOFF), k=200), CUTOFF)
    bad = [m for m in res if m.counterparty.inn is None]
    assert bad and all(rel(m).value > 0 and rel(m).explanation.startswith("Побед") for m in bad)


def test_shadow_never_breaks_the_page(search_data, mode, monkeypatch):
    """Reference only: the store failing leaves the rows as search() gave them («Нет данных»), no error."""
    from src.match.store import ParquetStore

    mode("shadow")
    found = search(Query(query_id="q", text="бумага офисная", as_of=CUTOFF), k=3)

    def down(self, inns, as_of):
        raise OSError("disk")

    monkeypatch.setattr(ParquetStore, "supplier_history", down)
    assert shadow_reliability(found, CUTOFF) == found


def test_small_sample_note_only_where_it_changes_the_percent():
    """Review #127: «с поправкой на малую выборку» only when the smoothing moves the shown percent."""
    _, few = history_reliability(h(lots=3, wins=3), PRIOR, REF)
    assert "Побед в ЭМ 3 из 3 лотов — " in few and "с поправкой на малую выборку" in few
    _, many = history_reliability(h(lots=500, wins=400, em_lots=500, em_wins=400), PRIOR, REF)
    assert "Побед в ЭМ 400 из 500 лотов — 80% (в среднем по ЭМ 40%)" in many and "поправк" not in many


def test_start_up_warms_the_prior_and_never_fails(monkeypatch):
    """The prior is one SQL over every supplier (1–1.5 s on Postgres): the API warms it at start-up, in the
    background; a store that is not there is no start-up error."""
    from src.api import app as app_mod

    seen: list[object] = []
    monkeypatch.setattr(search_mod, "reliability_prior", lambda store, as_of: seen.append(as_of))
    monkeypatch.setattr(app_mod, "get_store", lambda: "store")
    app_mod._warm_reliability()
    assert seen == [None]

    def down():
        raise OSError("no data")

    monkeypatch.setattr(app_mod, "get_store", down)
    app_mod._warm_reliability()  # logged, not raised
