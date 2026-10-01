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


# --- #242: «Может работать у заказчика» (near_customer) --------------------------------------------------------------

def _r(name: str, *, region: str | None = None, kpp: str | None = None, inn: str | None = None,
       entity: str | None = None, spb: int | None = None, origin: str = "history", kind=None,
       score: float = 0.5) -> CounterpartyMatch:
    stats: dict[str, float | int | str] = {}
    if entity:
        stats["entity_type"] = entity
    if spb is not None:
        stats["customers_spb"] = spb
    cp = Counterparty(inn=inn, name=name, region=region, kpp=kpp, stats=stats, kind=kind)
    return CounterpartyMatch(counterparty=cp, score=score, origin=origin,  # type: ignore[arg-type]
                             components=[ScoreComponent(name="product_fit", value=0.5, weight=1, explanation="x")])


NEAR_ROWS = [
    _r("spb-kpp", kpp="780101001", inn=UL),
    _r("lo-kpp", kpp="470301001"),
    _r("msk-kpp", kpp="770101001", spb=0),
    _r("msk-worked-spb", kpp="771001001", spb=3),  # Moscow, but has bid for St Petersburg customers
    _r("ip-spb-by-inn", inn=IP),  # ИП without a KPP: the INN region
    _r("ip-lo-zero-kpp", inn="470100000088", kpp="000000000"),  # a 000000000 KPP is a stub: the INN region
    _r("ul-zero-kpp", inn="7801000020", kpp="000000000"),  # ЮЛ: the INN is not a region source — unknown
    _r("registry-lo", region="Ленинградская область", kpp="780101001"),  # registry first
    _r("registry-77", region="регион 77", kpp="780101001"),  # src/enrich/enrich.py::_region_by_code
    _r("registry-moscow", region="Москва", kpp="780101001"),  # a known region without a code: not ours, not KPP
    _r("nothing"),
    _r("new-spb", region="Санкт-Петербург", origin="discovered"),
    _r("new-lo", region="Ленинградская область", origin="discovered"),
]
SPB_NEAR = ["spb-kpp", "lo-kpp", "ip-spb-by-inn", "ip-lo-zero-kpp", "registry-lo", "new-spb", "new-lo"]
SPB_WORKED = ["spb-kpp", "msk-worked-spb", "ip-spb-by-inn", "new-spb"]
LO_WORKED = ["lo-kpp", "ip-lo-zero-kpp", "registry-lo", "new-lo"]
LO_INN = "4703173460"


def _near(rows=NEAR_ROWS, okpd2=(), customer_inn=None, region="Санкт-Петербург", **f) -> list[str]:
    q = Query(query_id="q", text="x", okpd2=list(okpd2), customer_inn=customer_inn, region=region, filters=f)
    return [m.counterparty.name for m in apply_filters(rows, q)]


def test_supplier_region_registry_then_kpp_then_ip_inn():
    got = {m.counterparty.name: F.supplier_region(m.counterparty) for m in NEAR_ROWS}
    assert got == {"spb-kpp": "78", "lo-kpp": "47", "msk-kpp": "77", "msk-worked-spb": "77", "ip-spb-by-inn": "78",
                   "ip-lo-zero-kpp": "47", "ul-zero-kpp": None, "registry-lo": "47", "registry-77": "77",
                   "registry-moscow": "Москва", "nothing": None, "new-spb": "78", "new-lo": "47"}


def test_kpp_99_is_not_a_region():
    """KPP 99…: the inspectorate of the largest taxpayers — the region is unknown, not «another region»."""
    big = Counterparty(inn="7707049388", name="telecom", kpp="997750001")
    assert F.supplier_region(big) is None
    assert F.supplier_region(big.model_copy(update={"region": "Санкт-Петербург"})) == "78"  # the registry still wins


def test_near_is_the_customer_region_or_the_neighbour():
    assert _near(near_customer="near") == SPB_NEAR
    assert _near(near_customer="near", customer_inn=LO_INN) == SPB_NEAR  # 78 and 47 are neighbours both ways


def test_worked_is_the_same_region_or_spb_customers_in_history():
    assert _near(near_customer="worked") == SPB_WORKED
    # customer of the Leningrad region: only the same region (customers_spb says nothing about 47)
    assert _near(near_customer="worked", customer_inn=LO_INN) == LO_WORKED


def test_customer_region_is_the_inn_then_the_query_region():
    """D-083: the customer's INN 78 / 47 wins over Query.region; another prefix (77) leaves the query region."""
    assert _near(near_customer="worked", customer_inn=LO_INN, region="Санкт-Петербург") == LO_WORKED
    assert _near(near_customer="worked", region="Ленинградская область") == LO_WORKED
    assert _near(near_customer="worked", customer_inn="7707049388", region="Санкт-Петербург") == SPB_WORKED


def test_unknown_region_never_passes_and_is_not_guessed():
    for choice in ("worked", "near"):
        got = _near(near_customer=choice)
        assert "nothing" not in got and "ul-zero-kpp" not in got and "registry-moscow" not in got


def test_any_and_empty_are_no_filter():
    every = [m.counterparty.name for m in NEAR_ROWS]
    assert _near(near_customer="any") == _near(near_customer="") == _near() == every


@pytest.mark.parametrize("okpd2, applied", [
    (["43.39.11.000"], "near"), (["81.21.10.000"], "near"),  # works, services: on site — near by default
    (["17.12.14.110"], "any"), ([], "any"),  # goods travel; no OKPD2 — no type, no default filter
    (["62.01.29.000"], "any"), (["58.11.11.000"], "any"),  # a remote service, printed editions (goods, #188)
])
def test_auto_follows_the_purchase_type(okpd2, applied):
    q = Query(query_id="q", text="x", okpd2=okpd2, filters={"near_customer": "auto"})
    assert F.near_of(q).choice == applied
    assert _near(okpd2=okpd2, near_customer="auto") == _near(near_customer=applied)


def test_auto_without_a_coded_customer_region_is_any():
    q = Query(query_id="q", text="x", okpd2=["43.39.11.000"], region="Москва", filters={"near_customer": "auto"})
    assert F.near_of(q).choice == "any" and F.near_of(q).code is None


@pytest.mark.parametrize("choice", ["worked", "near"])
def test_explicit_near_without_a_coded_customer_region_is_an_error(choice):
    """Nothing to compare with: an error, not an empty list that looks like «nobody nearby»."""
    with pytest.raises(ValueError, match="near_customer"):
        _near(region="Москва", near_customer=choice)


@pytest.mark.parametrize("value", ["close", "yes", True])
def test_bad_near_customer_is_an_error(value):
    with pytest.raises(ValueError, match="near_customer"):
        parse_filters({"near_customer": value})


def test_near_combines_with_other_filters():
    rows = [*NEAR_ROWS, _r("ip-msk", inn="770100000079")]
    assert _near(rows, near_customer="near", entity="ip") == ["ip-spb-by-inn", "ip-lo-zero-kpp"]


def test_near_facets_equal_the_rows_each_choice_keeps():
    for inn in (None, LO_INN):
        q = Query(query_id="q", text="x", customer_inn=inn)
        got = F.near_facets(NEAR_ROWS, F.near_of(q))
        assert got == {c: len(_near(customer_inn=inn, near_customer=c)) for c in ("worked", "near", "any")}
    assert F.near_facets(NEAR_ROWS, F.near_of(Query(query_id="q", text="x"))) == {"worked": 4, "near": 7, "any": len(NEAR_ROWS)}


# --- review of #240, item 3: each chip group counted after the filters of the OTHER groups -------------------------

CHIP_ROWS = [
    _r("maker-ul-spb", kind="manufacturer", inn=UL, kpp="780101001", spb=1, score=0.9),
    _r("maker-ip-spb", kind="manufacturer", inn=IP, spb=1, score=0.8),
    _r("dist-ul-msk", kind="distributor", inn="7707049388", kpp="770701001", spb=2, score=0.7),
    _r("dist-ip-lo", kind="distributor", inn="470100000088", score=0.6),
    _r("dealer-ul-lo", kind="dealer", inn="4701000038", kpp="470101001", score=0.5),
    _r("unknown-ip-msk", kind="unknown", inn="770100000079", score=0.4),
    _r("none-no-inn", region="Санкт-Петербург", score=0.3),
    _r("maker-ul-msk", kind="manufacturer", inn="7804428656", kpp="770401001", spb=0, score=0.95),
]


def _chips(f: dict, customer_inn: str | None = None) -> dict[str, dict[str, int]]:
    return F.chip_facets(CHIP_ROWS, Query(query_id="q", text="x", customer_inn=customer_inn, filters=f))


def test_chip_facets_count_each_group_after_the_other_groups():
    f = {"entity": "ul", "near_customer": "near", "kinds": "distributor"}  # nothing passes all three
    assert _near(CHIP_ROWS, **f) == []
    assert _chips(f) == {
        "kind": {"manufacturer": 1, "distributor": 0, "dealer": 1, "unknown": 0},  # ЮЛ near the customer
        "entity": {"ul": 0, "ip": 1, "unknown": 0},  # distributors near the customer
        "near": {"worked": 1, "near": 0, "any": 1},  # distributors-ЮЛ: one from Moscow that bid for St Petersburg
    }


@pytest.mark.parametrize("f", [
    {}, {"entity": "ul", "near_customer": "near", "kinds": "distributor"},
    {"min_score": 0.5, "near_customer": "worked"}, {"kinds": "manufacturer,distributor", "entity": "ip"},
    {"only_msp": True}, {"near_customer": "auto"},
])
@pytest.mark.parametrize("customer_inn", [None, "4703173460"])
def test_a_chip_count_is_what_its_choice_gives_with_the_other_filters_on(f, customer_inn):
    got = _chips(f, customer_inn)
    others = {k: v for k, v in f.items() if k != "kinds"}
    for kind in F.KINDS:
        assert got["kind"][kind] == len(_near(CHIP_ROWS, customer_inn=customer_inn, **{**others, "kinds": kind}))
    for entity in ("ul", "ip"):
        assert got["entity"][entity] == len(_near(CHIP_ROWS, customer_inn=customer_inn, **{**f, "entity": entity}))
    no_entity = {k: v for k, v in f.items() if k != "entity"}
    assert sum(got["entity"].values()) == len(_near(CHIP_ROWS, customer_inn=customer_inn, **no_entity))
    for choice in ("worked", "near"):
        assert got["near"][choice] == len(_near(CHIP_ROWS, customer_inn=customer_inn,
                                                **{**f, "near_customer": choice}))


def test_a_group_choice_does_not_change_its_own_counts():
    assert _chips({"kinds": "dealer"})["kind"] == _chips({"kinds": "manufacturer,unknown"})["kind"] == \
        _chips({})["kind"]
    assert _chips({"entity": "ip"})["entity"] == _chips({"entity": "ul"})["entity"] == _chips({})["entity"]
    assert _chips({"near_customer": "worked"})["near"] == _chips({"near_customer": "near"})["near"] == \
        _chips({})["near"]
