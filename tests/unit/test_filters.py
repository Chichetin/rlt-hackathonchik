"""User filters (#232): role chips (kinds), ЮЛ/ИП (entity), minimum relevance (min_score), facets for the chips."""

import pytest

from src.contracts import Counterparty, CounterpartyMatch, Query, ScoreComponent
from src.match import filters as F
from src.match.filters import apply_filters, apply_legal, facets, parse_filters

UL, IP = "7801000012", "780100000090"  # checksum-valid INNs of the ingest fixture: 10 digits — ЮЛ, 12 — ИП


def _m(name: str, kind=None, inn: str | None = None, entity: str | None = None, score: float = 0.5,
       status: str | None = None, in_rnp: bool | None = None, origin: str = "history") -> CounterpartyMatch:
    stats: dict[str, float | int | str] = {"entity_type": entity} if entity else {}
    cp = Counterparty(inn=inn, name=name, kind=kind, stats=stats, status=status, in_rnp=in_rnp)
    return CounterpartyMatch(counterparty=cp, score=score, origin=origin,  # type: ignore[arg-type]
                             components=[ScoreComponent(name="product_fit", value=0.5, weight=1, explanation="x")])


def _names(matches, **f) -> list[str]:
    return [m.counterparty.name for m in apply_filters(matches, Query(query_id="q", text="x", filters=f))]


ROLES = [_m("maker", "manufacturer"), _m("dist", "distributor"), _m("dealer", "dealer"),
         _m("rule-unknown", "unknown"), _m("not-yet", None)]


def test_kinds_single():
    assert _names(ROLES, kinds="distributor") == ["dist"]
    assert _names(ROLES, kinds="dealer") == ["dealer"]


def test_kinds_several_and_spaces():
    assert _names(ROLES, kinds="manufacturer,distributor") == ["maker", "dist"]
    assert _names(ROLES, kinds=" dealer , manufacturer ") == ["maker", "dealer"]


def test_kinds_unknown_is_rule_unknown_and_not_determined_yet():
    """«Роль не определена»: the rule said unknown (D-057) or there is no role yet (None)."""
    assert _names(ROLES, kinds="unknown") == ["rule-unknown", "not-yet"]
    assert _names(ROLES, kinds="unknown,distributor") == ["dist", "rule-unknown", "not-yet"]


def test_kinds_empty_is_no_filter():
    every = [m.counterparty.name for m in ROLES]
    assert _names(ROLES, kinds="") == every and _names(ROLES) == every and _names(ROLES, kinds=" , ") == every


def test_only_manufacturers_is_a_synonym_of_kinds_manufacturer():
    assert _names(ROLES, only_manufacturers=True) == _names(ROLES, kinds="manufacturer") == ["maker"]
    assert _names(ROLES, only_manufacturers=False) == [m.counterparty.name for m in ROLES]
    # both given: filters are ANDed, as every other pair of filters
    assert _names(ROLES, only_manufacturers=True, kinds="manufacturer,dealer") == ["maker"]
    assert _names(ROLES, only_manufacturers=True, kinds="distributor") == []


ENTITIES = [_m("ul-stats", entity="ЮЛ"), _m("ip-stats", entity="ИП"), _m("ul-inn", inn=UL), _m("ip-inn", inn=IP),
            _m("nothing"), _m("stats-win", inn=UL, entity="ИП")]


def test_entity_by_stats_then_inn_length():
    assert _names(ENTITIES, entity="ul") == ["ul-stats", "ul-inn"]
    assert _names(ENTITIES, entity="ip") == ["ip-stats", "ip-inn", "stats-win"]  # organizers' entity_type first


def test_entity_unknown_does_not_pass_and_is_not_guessed():
    assert "nothing" not in _names(ENTITIES, entity="ul") + _names(ENTITIES, entity="ip")
    assert F.entity_of(ENTITIES[4].counterparty) is None


def test_entity_empty_is_no_filter():
    assert _names(ENTITIES, entity="") == [m.counterparty.name for m in ENTITIES]


SCORES = [_m("a", score=0.8), _m("b", score=0.5), _m("c", score=0.4999), _m("d", score=0.0)]


def test_min_score_drops_below_and_keeps_equal():
    assert _names(SCORES, min_score=0.5) == ["a", "b"]
    assert _names(SCORES, min_score=0.0) == ["a", "b", "c", "d"]
    assert _names(SCORES, min_score=0.81) == []
    assert _names(SCORES, min_score="0.5") == ["a", "b"]  # Query.filters allows str: a numeric string works
    assert _names(SCORES, min_score="") == ["a", "b", "c", "d"]


def test_filters_combine_with_and():
    rows = [_m("maker-ul", "manufacturer", inn=UL, score=0.9), _m("maker-ip", "manufacturer", inn=IP, score=0.9),
            _m("maker-ul-weak", "manufacturer", inn=UL, score=0.1), _m("dist-ul", "distributor", inn=UL, score=0.9)]
    assert _names(rows, kinds="manufacturer", entity="ul", min_score=0.5) == ["maker-ul"]


@pytest.mark.parametrize("f, key", [
    ({"kinds": "manufacturer,producer"}, "kinds"), ({"kinds": True}, "kinds"),
    ({"entity": "fl"}, "entity"), ({"entity": True}, "entity"),
    ({"min_score": "high"}, "min_score"), ({"min_score": True}, "min_score"),
    ({"min_score": float("nan")}, "min_score"), ({"min_score": "inf"}, "min_score"),
])
def test_bad_values_are_an_error_not_a_silent_no_filter(f, key):
    """A typo must not turn the filter off (D-030: a filter keeps only confirmed matches)."""
    with pytest.raises(ValueError, match=key):
        parse_filters(f)


def test_unknown_keys_are_ignored():
    assert parse_filters({"something_new": "x"}) == parse_filters({})


def test_legal_status_drops_dead_and_rnp_before_user_filters():
    rows = [_m("ok"), _m("liq", status="В процессе ликвидации"), _m("rnp", in_rnp=True), _m("unchecked")]
    assert [m.counterparty.name for m in apply_legal(rows)] == ["ok", "unchecked"]
    assert _names(rows) == ["ok", "unchecked"]


@pytest.mark.parametrize("status", [
    "Юридическое лицо прекратило деятельность", "Индивидуальный предприниматель прекратил деятельность",
    "Недействующее юридическое лицо", "Исключено из ЕГРЮЛ", "деятельность прекращена (ГИР БО: TERMINATED)",
    "банкротство (ГИР БО: BANKRUPT)",
])
def test_legal_status_drops_every_stopped_entity_wording(status):
    rows = [_m("dead", status=status), _m("ok", status="Действующая организация")]
    assert [m.counterparty.name for m in apply_legal(rows)] == ["ok"]


def test_facets_count_every_role_and_entity_value():
    rows = [*ROLES, *ENTITIES]
    got = facets(rows)
    assert got == {"kind": {"manufacturer": 1, "distributor": 1, "dealer": 1, "unknown": 2 + len(ENTITIES)},
                   "entity": {"ul": 2, "ip": 3, "unknown": len(ROLES) + 1}}
    assert facets([]) == {"kind": {"manufacturer": 0, "distributor": 0, "dealer": 0, "unknown": 0},
                          "entity": {"ul": 0, "ip": 0, "unknown": 0}}


def test_facet_equals_the_rows_a_single_chip_keeps():
    rows = [*ROLES, *ENTITIES]
    got = facets(rows)
    for kind, n in got["kind"].items():
        assert len(apply_filters(rows, Query(query_id="q", text="x", filters={"kinds": kind}))) == n
    for entity in ("ul", "ip"):
        assert len(apply_filters(rows, Query(query_id="q", text="x", filters={"entity": entity}))) == \
            got["entity"][entity]
