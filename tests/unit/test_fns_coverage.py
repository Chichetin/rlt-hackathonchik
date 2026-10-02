from src.contracts import Counterparty, CounterpartyMatch
from src.eval.fns_coverage import count, share


def _m(inn: str, status: str | None) -> CounterpartyMatch:
    return CounterpartyMatch(counterparty=Counterparty(inn=inn, name=f"ИНН {inn}", status=status), score=1.0,
                             components=[], origin="history")


def test_count_splits_checked_active_unchecked():
    c = count([_m("7707083893", "Действующая"), _m("7736050003", "Ликвидирована"), _m("7702070139", None)])
    assert (c["n"], c["checked"], c["active"], c["unchecked"]) == (3, 2, 1, 1)
    assert c["unchecked_inn"] == ["7702070139"]


def test_share_of_empty_block_is_none():
    assert share(0, 0) is None
    assert share(1, 4) == 0.25
