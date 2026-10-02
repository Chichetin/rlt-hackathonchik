from src.contracts import Counterparty, CounterpartyMatch, Kind
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


def _full(inn="7707083893", *, name="ООО Ромашка", kind: Kind | None = "manufacturer", ev=("okved", "https://pb.nalog.ru/x")):
    from datetime import UTC, datetime

    from src.contracts import Evidence

    evidence = [Evidence(source="s", url=ev[1], fetched_at=datetime(2026, 10, 2, tzinfo=UTC), fact="f", field=ev[0],
                         value="v")]
    return CounterpartyMatch(counterparty=Counterparty(inn=inn, name=name, kind=kind, status="Действующее",
                                                       evidence=evidence), score=1.0, components=[], origin="history")


def test_count_named_role_basis_dated():
    rows = [_full(),                                                    # all three
            _full(name="ИНН 7707083893"),                               # no name
            _full(kind=None),                                           # okved evidence but no role
            _full(kind="unknown"),                                      # unknown is not a role
            _full(ev=("inn", "data:contracts#1")),                      # organizers' data only: no basis, no date
            _m("7702070139", None)]
    c = count(rows)
    assert (c["named"], c["role_basis"], c["dated"]) == (4, 2, 4)
