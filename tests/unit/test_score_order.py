"""#624: the shown order is the shown score, descending; the model's order only breaks ties."""
from src.api.app import by_score
from src.contracts import Counterparty, CounterpartyMatch


def _m(inn: str, score: float, origin: str = "history", reason: str | None = None) -> CounterpartyMatch:
    return CounterpartyMatch(counterparty=Counterparty(inn=None, name=inn), score=score, components=[],
                             origin=origin, rank_reason=reason)  # type: ignore[arg-type]


def test_lower_score_never_stands_above_a_higher_one():
    model = [_m("a", 0.75, reason="выше следующего: опыт"), _m("b", 0.83), _m("c", 0.83), _m("n1", 0.4, "discovered"),
             _m("n2", 0.9, "discovered")]
    out = by_score(model)
    assert [m.counterparty.name for m in out] == ["b", "c", "a", "n2", "n1"]  # equal 0.83: the model's order kept
    hist = [m.score for m in out if m.origin == "history"]
    assert hist == sorted(hist, reverse=True)


def test_reason_stays_only_for_the_same_neighbour_below():
    kept = by_score([_m("a", 0.9, reason="выше следующего: опыт"), _m("b", 0.8)])
    assert kept[0].rank_reason == "выше следующего: опыт"
    moved = by_score([_m("a", 0.7, reason="выше следующего: опыт"), _m("b", 0.8)])
    assert [m.counterparty.name for m in moved] == ["b", "a"] and moved[1].rank_reason is None
