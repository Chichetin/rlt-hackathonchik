"""Second basis of the role (#509): behaviour in the organizers' data — many OKPD2 classes, no main one.
Never rewrites `kind`; a `data:` Evidence that survives strip_unverified; goods only."""

from datetime import UTC, datetime

import pytest

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query
from src.enrich.evidence import strip_unverified
from src.match import pipeline
from src.sources import role

AT = datetime(2026, 9, 12, tzinfo=UTC)
INN = "7811756312"


def _cp(classes=34, share=0.12, kind=None, evidence=True, **extra) -> Counterparty:
    ev = [Evidence(source="data:participants", url="data:participants#L1", fetched_at=AT, fact="ИНН",
                   field="inn", value=INN)] if evidence else []
    stats = {} if classes is None else {"okpd2_classes": classes, "top_class_share": share}
    return Counterparty(inn=INN, name="ИНН " + INN, kind=kind, stats=stats, evidence=ev, **extra)


def test_fact_text_and_source():
    ev = role.behavior_fact(_cp())
    assert ev is not None
    assert ev.fact == ("По истории закупок — торговый посредник: широкий ассортимент, 34 класса ОКПД2, "
                       "на главный приходится 12%")
    assert ev.url == f"data:suppliers#{INN}" and ev.source == "data:suppliers" and ev.fetched_at == AT

def _fact(cp) -> str:
    ev = role.behavior_fact(cp)
    assert ev is not None
    return ev.fact


@pytest.mark.parametrize("n,word", [(15, "классов"), (21, "класс"), (22, "класса"), (111, "классов"), (112, "классов")])
def test_plural(n, word):
    assert f" {n} {word} ОКПД2" in _fact(_cp(classes=n))


@pytest.mark.parametrize("classes,share", [(14, 0.1), (15, 0.30), (40, 0.5), (None, 0)])
def test_thresholds_not_met(classes, share):
    assert role.behavior_fact(_cp(classes=classes, share=share)) is None


def test_boundary_inclusive_classes_exclusive_share():
    assert role.behavior_fact(_cp(classes=15, share=0.2999)) is not None


def test_no_data_evidence_no_fact():
    assert role.behavior_fact(_cp(evidence=False)) is None


def test_manufacturer_names_disagreement():
    assert "По ОКВЭД — производитель, по истории закупок — похож на посредника" in \
        _fact(_cp(kind="manufacturer"))
    gisp = Evidence(source="ГИСП", url="https://x/1", fetched_at=AT, fact="f", field="product", value="v")
    cp = _cp(kind="manufacturer")
    cp = cp.model_copy(update={"evidence": [*cp.evidence, gisp]})
    assert _fact(cp).startswith("По реестру ГИСП — производитель")


def test_with_behavior_keeps_kind_once_and_goods_only():
    cp = _cp(kind="dealer")
    out = role.with_behavior(cp, "goods")
    assert out.kind == "dealer" and sum(e.field == role.BEHAVIOR_FIELD for e in out.evidence) == 1
    assert role.with_behavior(out, "goods") is out
    assert role.with_behavior(cp, "services") is cp and role.with_behavior(cp, "works") is cp


def test_survives_strip_unverified():
    out = strip_unverified(role.with_behavior(_cp(), "goods"), lambda url: None)
    assert any(e.field == role.BEHAVIOR_FIELD for e in out.evidence)


def test_pipeline_goods_vs_services_and_origin():
    hist = CounterpartyMatch(counterparty=_cp(), score=1.0, components=[], origin="history")
    new = CounterpartyMatch(counterparty=_cp(), score=1.0, components=[], origin="discovered")
    goods = pipeline._for_purchase([hist, new], Query(query_id="q", text="бумага", okpd2=["17.12.14"]))
    assert any(e.field == role.BEHAVIOR_FIELD for e in goods[0].counterparty.evidence)
    assert goods[1] is new
    svc = pipeline._for_purchase([hist], Query(query_id="q", text="уборка", okpd2=["81.21.10"]))
    assert not any(e.field == role.BEHAVIOR_FIELD for e in svc[0].counterparty.evidence)
