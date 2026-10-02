"""Opt-in incumbent signal (#22): off by default, counts only the query's customer before as_of."""

from __future__ import annotations

from datetime import date

import pytest

from src.contracts import Query
from src.match import search as search_mod
from src.match.search import search
from src.match.signals import incumbent_value, mix_incumbent, signal_shares

pytestmark = pytest.mark.usefixtures("v2_formula")  # the v2 formula; the ranker on top — test_ranker.py

CUTOFF = date(2025, 7, 1)
CUST = "7701000040"  # customer of paper lot 3 (2024-11-05) and lot 7 (2025-10-15, after cutoff)


def _exp(res, inn):
    m = next(m for m in res if m.counterparty.inn == inn)
    return next(c for c in m.components if c.name == "experience")


@pytest.fixture
def with_signal(monkeypatch):
    def apply(share):
        cfg = search_mod.load_config()
        monkeypatch.setattr(search_mod, "load_config", lambda: {**cfg, "signals": {"incumbent": share}})
    return apply


def test_default_config_has_no_active_signal():
    assert signal_shares(search_mod.load_config()) == {"incumbent": 0.0, "inn_region": 0.0}


def test_signal_shares_validate():
    with pytest.raises(ValueError):
        signal_shares({"signals": {"incumbent": 1.5}})
    with pytest.raises(ValueError):
        signal_shares({"signals": {"incumbent": -0.1}})
    with pytest.raises(ValueError):
        signal_shares({"signals": {"bogus": 0.1}})
    assert signal_shares({"signals": {"incumbent": 1}}) == {"incumbent": 1.0, "inn_region": 0.0}


def test_mix_zero_share_is_identity_and_saturates():
    assert mix_incumbent(0.3, "x", 4, 0.0) == (0.3, "x")
    assert incumbent_value(0) == 0.0 and incumbent_value(5) == 1.0 and incumbent_value(50) == 1.0
    assert 0 < incumbent_value(1) < incumbent_value(2) < 1


def test_off_by_default_matches_v1(search_data):
    q = Query(query_id="q", text="бумага офисная", customer_inn=CUST, as_of=CUTOFF)
    res = search(q)
    assert all("с этим заказчиком" not in _exp(res, m.counterparty.inn).explanation for m in res)


def test_incumbent_counts_only_this_customer_before_as_of(search_data, with_signal):
    with_signal(1.0)
    q = Query(query_id="q", text="бумага офисная", customer_inn=CUST, as_of=CUTOFF)
    res = search(q)
    inns = {m.counterparty.inn for m in res}
    # lot 3 of CUST before cutoff
    assert _exp(res, "4701000038").value == pytest.approx(round(incumbent_value(1), 4))
    assert "с этим заказчиком: 1 лот" in _exp(res, "4701000038").explanation
    # 7801000020: lots only of another customer
    if "7801000020" in inns:
        assert _exp(res, "7801000020").value == 0.0
    # lot 7 of CUST is after cutoff: 470100000088 gets nothing from it
    if "470100000088" in inns:
        assert "с этим заказчиком: 0 лот" in _exp(res, "470100000088").explanation
    for m in res:
        assert m.score == pytest.approx(sum(c.value * c.weight for c in m.components), abs=1e-3)


def test_incumbent_without_customer_is_noop(search_data, with_signal):
    q = Query(query_id="q", text="бумага офисная", as_of=CUTOFF)
    base = [(m.counterparty.inn, m.score) for m in search(q)]
    with_signal(1.0)
    assert [(m.counterparty.inn, m.score) for m in search(q)] == base
