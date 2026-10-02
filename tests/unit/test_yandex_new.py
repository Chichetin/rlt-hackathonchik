import json

from src.contracts import Counterparty, CounterpartyMatch, ScoreComponent
from src.eval.yandex_new import from_yandex, thin_cases


def _case(qid: str, codes: list[str]) -> str:
    return json.dumps({"query": {"query_id": qid, "text": "x", "okpd2": codes}, "relevant_inns": [], "grades": {}})


def test_thin_cases_one_per_group_then_next_round(tmp_path):
    thin = tmp_path / "thin.json"
    thin.write_text(json.dumps({"thin": [{"okpd2": "21.20"}, {"okpd2": "56.29"}]}), encoding="utf-8")
    cases = tmp_path / "dev.jsonl"
    cases.write_text("\n".join([_case("a1", ["21.20.10.110"]), _case("a2", ["21.20.23.110"]),
                                _case("b1", ["56.29.20.000"]), _case("c1", ["17.12.14.110"])]), encoding="utf-8")
    first = thin_cases(2, cases, thin)
    assert {c["query"]["okpd2"][0][:5] for c in first} == {"21.20", "56.29"}  # one per group before a second round
    assert len(thin_cases(10, cases, thin)) == 3  # the non-thin case never enters
    assert [c["query"]["query_id"] for c in thin_cases(3, cases, thin)] == \
           [c["query"]["query_id"] for c in thin_cases(3, cases, thin)]  # seeded: same order every run


def _match(explanation: str) -> CounterpartyMatch:
    return CounterpartyMatch(
        counterparty=Counterparty(inn="7707083893", name="ПАО Тест", evidence=[]),
        score=0.5, origin="discovered",
        components=[ScoreComponent(name="product_fit", value=0.8, weight=0.762, explanation=explanation)])


def test_from_yandex_only_by_the_mark_of_yandex_matches():
    assert from_yandex(_match("Найдена в Яндекс Поиске по запросу «шприцы производитель», ИНН указан на сайте a.ru"))
    assert not from_yandex(_match("Найдена веб-поиском по товару; вид деятельности — по реестру ФНС"))
    assert not from_yandex(_match("основной ОКВЭД 21.20 — производство"))
