"""T-14: selection of queries, the todo table (only Evidence facts), precision@10 of the block «Новые»."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query
from src.eval import label_new as L
from src.eval.split import EvalCase


def case(qid: str, okpd2: list[str]) -> EvalCase:
    return EvalCase(query=Query(query_id=qid, text="предмет " + qid, okpd2=okpd2), relevant_inns=[],
                    date=date(2025, 7, 10))


def test_select_is_deterministic_and_by_class():
    real = [case("lot-1", ["20.20.14.000"])]
    dev = [case("lot-9", ["32.50.13.110"]), case("lot-3", ["32.50.13.120"]), case("lot-5", ["20.20.14.100"]),
           case("lot-4", ["71.12.35.000"]), case("lot-6", ["71.12.19.000"]), case("lot-7", ["71.12.19.000"]),
           case("lot-8", ["32.50.13.110", "71.12.35.000"]),  # two classes: skipped
           case("lot-2", ["32"])]  # code too short: skipped
    a = L.select_cases(real, dev, 3)
    assert [c.query.query_id for c in a] == ["lot-1", "lot-4", "lot-3"]  # 71.12 (3) before 32.50 (2)
    assert a == L.select_cases(real, list(reversed(dev)), 3)


def match(inn: str | None, facts: dict[str, str]) -> CounterpartyMatch:
    ev = [Evidence(source="rmsp", url="https://ofd.nalog.ru/x", fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
                   fact=fact, field=fld, value=fact) for fld, fact in facts.items()]
    return CounterpartyMatch(counterparty=Counterparty(inn=inn, name="ООО Тест", evidence=ev), score=0.5,
                             components=[], origin="discovered")


def test_table_has_only_evidence_facts():
    q = Query(query_id="q1", text="картриджи", okpd2=["26.20.40.000"])
    rows = L.table_rows(q, [match("7801000012", {"okved": "Основной ОКВЭД 26.20 — Производство"}),
                            match(None, {})])
    assert rows[0]["okved"] == "Основной ОКВЭД 26.20 — Производство"
    assert rows[0]["region"] == "" and rows[0]["msp"] == ""  # no Evidence -> empty, never invented
    assert rows[0]["source_url"] == "https://ofd.nalog.ru/x" and rows[0]["label"] == ""
    assert rows[1]["inn"] == L.UNCONFIRMED and rows[1]["source_url"] == ""
    assert len(L.table_rows(q, [match("7801000012", {})] * 15)) == L.TOP_K


def lab(q: str, inn: str, label: str, by: str = "человек:a") -> dict[str, str]:
    return {"query_id": q, "inn": inn, "name": "", "label": label, "labeled_by": by}


def test_precision_and_priority():
    rows = [lab("a", "1", "1"), lab("a", "2", "0"), lab("a", "3", "1"), lab("a", "4", ""),  # empty: skipped
            lab("b", "5", "0"), lab("b", "6", "0", "llm:m"), lab("b", "7", "1", "llm:m"),
            lab("b", "5", "1", "llm:m")]  # human 0 wins over llm 1
    body = L.precision_body(rows)
    per = {p["query_id"]: p["precision@10"] for p in body["per_query"]}
    assert per == {"a": pytest.approx(2 / 3), "b": 0.0}  # LLM labels excluded: overlap < 20
    assert body["metrics"]["precision@10"]["value"] == pytest.approx(1 / 3)
    assert body["labeled_by"] == {"человек": 4}
    assert body["agreement_human_llm"] == {"overlap": 1, "share": 0.0, "llm_used": False}


def test_read_labels_semicolon_and_unconfirmed(tmp_path):
    p = tmp_path / "l.csv"
    p.write_text("query_id;inn;name;label;labeled_by\nq;ИНН не подтверждён;ООО «Х»;1;человек:a\n", encoding="utf-8-sig")
    body = L.precision_body(L.read_labels(p))
    assert body["metrics"]["precision@10"]["value"] == 1.0 and body["labeled"] == 1


def test_llm_without_agreement_does_not_change_value():
    human = [lab("a", "1", "1"), lab("a", "2", "0")]
    llm = [lab("a", "3", "0", "llm:m"), lab("a", "4", "0", "llm:m")]
    base = L.precision_body(human)["metrics"]["precision@10"]["value"]
    body = L.precision_body(human + llm)
    assert body["metrics"]["precision@10"]["value"] == base == 0.5  # overlap < 20: LLM labels are not used
    assert body["agreement_human_llm"]["llm_used"] is False and body["labeled"] == 2


def test_llm_used_after_agreement():
    rows = []
    for i in range(20):  # 20 common pairs, all equal
        rows += [lab("a", f"h{i}", "1"), lab("a", f"h{i}", "1", "llm:m")]
    rows.append(lab("a", "only", "0", "llm:m"))
    body = L.precision_body(rows, {"a": 21, "b": 0}, allow_llm=True)
    assert body["agreement_human_llm"]["llm_used"] is True and body["labeled"] == 21
    assert body["empty_blocks"] == ["b"] and body["n_selected"] == 2


def test_llm_labels_are_not_used_by_default_even_after_agreement():
    """D-133: the slide number comes from human labels only; --with-llm is the explicit opt-in."""
    rows = []
    for i in range(20):
        rows += [lab("a", f"h{i}", "1"), lab("a", f"h{i}", "1", "llm:m")]
    rows.append(lab("a", "only", "0", "llm:m"))
    body = L.precision_body(rows)
    assert body["agreement_human_llm"]["llm_used"] is False and body["labeled"] == 20


def test_empty_labeled_by_is_nobody():
    body = L.precision_body([lab("a", "1", "1", ""), lab("a", "2", "0")])
    assert body["labeled"] == 1 and body["labeled_by"] == {"человек": 1}
