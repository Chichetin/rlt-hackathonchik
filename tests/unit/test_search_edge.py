"""Edge cases of search() and the fallback ladder (#113) on the synthetic ingest fixture.

Fixture (tests/fixtures/ingest): code 17.12.14.110 (paper) in lots 1, 3, 6, 7; 7801000012 took part in all four.
Postgres is off here (DATABASE_URL is not postgres): the vector step is skipped and says so.
"""

from __future__ import annotations

import threading
from urllib.parse import unquote

import pytest

from src.contracts import Query
from src.match import fallback
from src.match import index as index_mod
from src.match.search import search


@pytest.fixture(autouse=True)
def _no_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")


def fit(m):
    return next(c for c in m.components if c.name == "product_fit")


@pytest.mark.parametrize(("raw", "valid", "bad"), [
    (["17.12.14.110", "17.12.", " 1712 ", "171214110", "17,12"], ["17.12.14.110", "17.12"], []),
    (["abc", "1", "17.1.14"], [], ["abc", "1", "17.1.14"]),
])
def test_okpd2_codes_are_normalized(raw, valid, bad):
    assert fallback.okpd2_codes(raw) == (valid, bad)


def test_ladder_rungs_start_at_the_codes_own_level():
    assert fallback._okpd2_rungs(["17.12.14.110"]) == [(4, ["17.12.14.110"]), (3, ["17.12.14"]), (2, ["17.12"]),
                                                       (1, ["17"])]
    assert fallback._okpd2_rungs(["17.12", "33.12.1"]) == [(4, ["33.12.1"]), (2, ["17.12", "33.12"]), (1, ["17", "33"])]


@pytest.mark.parametrize(("text", "okpd2", "what", "depth"), [
    ("абракадабра", ["17.12.14.110"], "кода ОКПД2 17.12.14.110", 1.0),  # no word in the index
    ("и в на", ["1712"], "группы ОКПД2 17.12", 0.5),  # no words at all; the code normalized
    ("абракадабра", ["17.12.99.000"], "группы ОКПД2 17.12", 0.5),  # code absent from the data: raised
    ("абракадабра", ["17.99.99.999"], "класса ОКПД2 17", 0.25),  # up to the class
])
def test_ladder_finds_suppliers_by_okpd2_level(search_data, text, okpd2, what, depth):
    res = search(Query(query_id="q", text=text, okpd2=okpd2))
    assert res and res[0].counterparty.inn == "7801000012"  # most lots with the code
    for m in res:
        assert fit(m).explanation.startswith(f"По названию не найдено; поставщик {what} по частоте участия")
        assert fit(m).value <= 0.4 * 1.0 + 1e-9  # BM25 part is 0
    assert fit(res[0]).value == pytest.approx(0.4 * depth)
    assert what in (fallback.search_note() or "")


def test_vector_step_when_vectors_answer(search_data, monkeypatch):
    """Postgres stand-in: the vector step returns the paper documents; BM25 part is 0, class predicted."""
    monkeypatch.setattr(fallback, "_vector_hits", lambda idx, text: (idx.retrieve(["бумаг"], 50), ""))
    res = search(Query(query_id="q", text="bumaga"))
    assert res and all(fit(m).explanation.startswith("По названию не найдено; найден по смыслу") for m in res)
    assert fit(res[0]).value == pytest.approx(0.4)  # 0.6 · 0 + 0.4 · code depth 1.0 (predicted 17.12.14.110)
    assert "по смыслу" in (fallback.search_note() or "")


@pytest.mark.parametrize(("body", "status", "note"), [
    ({"text": "   "}, 422, None),
    ({"text": "и в на"}, 200, "в запросе нет слов для поиска; ОКПД2 не задан"),
    ({"text": "экскаватор"}, 200, "по названию ничего не найдено; поиск по смыслу недоступен; ОКПД2 не задан"),
    ({"text": "экскаватор", "okpd2": ["abc"]}, 200, "ОКПД2 не распознан: abc"),
    ({"text": "экскаватор", "okpd2": ["99.99.99.999"]}, 200, "по ОКПД2 99.99.99.999 нет поставщиков"),
    ({"text": "бумага", "as_of": "2020-01-01"}, 200, "нет истории до 01.01.2020"),
    ({"text": "бумага", "filters": {"region": "Москва"}}, 200, "фильтры скрыли 6"),
])
def test_empty_answer_says_why(search_data, tmp_path, monkeypatch, body, status, note):
    from fastapi.testclient import TestClient

    from src.api import app as app_mod

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'q.sqlite').as_posix()}")
    monkeypatch.setattr(app_mod, "_engine", None)
    r = TestClient(app_mod.app).post("/api/search", json={"query_id": "q", **body})
    assert r.status_code == status
    if status == 422:
        assert r.json()["detail"] == "Пустой запрос"
        return
    assert r.json()["history"] == []
    assert note in unquote(r.headers["X-Search-Note"])


def test_long_query_keeps_the_rarest_words(search_data):
    idx = index_mod.get_index(search_data, None)
    tokens = ["бумаг", "картр", "тонер", "ремон", "бумаг", "xyzzy"]
    assert fallback.cap_tokens(idx, tokens, cap=5) == tokens  # ≤ cap unique words: untouched
    assert fallback.cap_tokens(idx, tokens, cap=2) == ["тонер", "ремон"]  # 1 and 2 documents


def test_parallel_first_build_builds_the_index_once(search_data, monkeypatch):
    calls = []
    build = index_mod._build
    monkeypatch.setattr(index_mod, "_build", lambda *a: (calls.append(1), build(*a))[1])
    got: list = []
    threads = [threading.Thread(target=lambda: got.append(index_mod.get_index(search_data, None))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and len({id(i) for i in got}) == 1
