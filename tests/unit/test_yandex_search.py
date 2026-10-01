"""Yandex Search API client (#194): XML → documents, the hard request limit, cache does not spend, refusals.

`tests/fixtures/yandex_search/vaccines_spb.xml` is the real XML answer of the probe of 01.10 («поставщик
вакцин оптом Санкт-Петербург», region 2). No test talks to the network (conftest closes httpx).
"""

import base64
import json
from pathlib import Path

import httpx
import pytest

from src.sources import yandex_search as Y
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "yandex_search"
XML = (FIX / "vaccines_spb.xml").read_text(encoding="utf-8")
QUERY = "поставщик вакцин оптом Санкт-Петербург"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "test-key")
    monkeypatch.setenv("YANDEX_BUDGET_DB", str(tmp_path / "budget.sqlite"))
    monkeypatch.setattr(Y, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(Y, "_blocked_until", 0.0)


def _client(calls: list[httpx.Request], status: int = 200, xml: str = XML) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"code": 16, "message": "denied"})
        return httpx.Response(200, json={"rawData": base64.b64encode(xml.encode("utf-8")).decode()})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_xml_real_answer():
    found, docs = Y.parse_xml(XML)
    assert found == "5209546"
    assert len(docs) == 10
    assert docs[0].url == "https://privivka.spb.ru/wholesale/"
    assert docs[0].domain == "privivka.spb.ru"
    assert all(d.url.startswith("http") and d.domain for d in docs)
    assert "<" not in docs[0].title  # <hlword> is flattened into text


def test_flatten_parse_roundtrip():
    body = Y.flatten(QUERY, 0, "2", XML)
    ans = Y.parse(body)
    assert ans.meta["query.text"] == QUERY and ans.meta["answer.found"] == "5209546"
    assert [d.url for d in ans.docs] == [d.url for d in Y.parse_xml(XML)[1]]
    assert all("\n" not in line for line in body.splitlines())


def test_search_url_is_human_checkable():
    url = Y.search_url(QUERY, 1, "2")
    assert url.startswith("https://yandex.ru/search/?text=") and url.endswith("&lr=2&p=1")
    with pytest.raises(ValueError):
        Y.search_url("  ")


def test_budget_hard_limit(tmp_path):
    b = Y.Budget(tmp_path / "b.sqlite", limit=3)
    b.record(1, "проба")
    assert [b.take("q"), b.take("q")] == [2, 3]
    with pytest.raises(Y.BudgetExceeded):
        b.take("q")
    assert b.used() == 3 and b.left() == 0


def test_request_is_counted_and_cache_is_free(tmp_path):
    calls: list[httpx.Request] = []
    budget = Y.Budget(limit=10)
    cache = SourceCache(tmp_path / "c.sqlite")
    client = _client(calls)
    first = Y.search(QUERY, cache=cache, client=client, budget=budget)
    second = Y.search(QUERY, cache=cache, client=client, budget=budget)
    assert first is not None and second is not None and first.body == second.body
    assert len(calls) == 1 and budget.used() == 1
    sent = json.loads(calls[0].content)
    assert sent["query"]["queryText"] == QUERY and sent["region"] == "2"
    assert calls[0].headers["Authorization"] == "Api-Key test-key"
    assert cache.get(Y.search_url(QUERY) + "#raw") is not None  # XML kept for audit


def test_exhausted_budget_sends_nothing(tmp_path):
    calls: list[httpx.Request] = []
    budget = Y.Budget(limit=1)
    budget.record(1, "уже потрачено")
    with pytest.raises(Y.BudgetExceeded):
        Y.search(QUERY, cache=SourceCache(tmp_path / "c.sqlite"), client=_client(calls), budget=budget)
    assert calls == []


def test_offline_reads_cache_only(tmp_path):
    cache = SourceCache(tmp_path / "c.sqlite")
    assert Y.search(QUERY, cache=cache, offline=True) is None
    cache.put(Y.search_url(QUERY), Y.flatten(QUERY, 0, "2", XML))
    assert Y.search(QUERY, cache=cache, offline=True) is not None


def test_refusal_blocks_and_is_counted(tmp_path):
    calls: list[httpx.Request] = []
    budget = Y.Budget(limit=10)
    with pytest.raises(Y.Blocked):
        Y.search(QUERY, cache=SourceCache(tmp_path / "c.sqlite"), client=_client(calls, 401), budget=budget)
    assert budget.used() == 1  # a failed paid request is never under-counted
    with pytest.raises(Y.Blocked):  # paused: no second request
        Y.search("другой запрос", cache=SourceCache(tmp_path / "c.sqlite"), client=_client(calls), budget=budget)
    assert len(calls) == 1


def test_no_key_no_request(tmp_path, monkeypatch):
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "your_search_api_key_here")
    calls: list[httpx.Request] = []
    budget = Y.Budget(limit=10)
    with pytest.raises(RuntimeError, match="YANDEX_SEARCH_API_KEY"):
        Y.fetch_raw(QUERY, client=_client(calls), budget=budget)
    assert calls == [] and budget.used() == 0


def test_main_root_of_worktree(tmp_path):
    main = tmp_path / "repo"
    wt = main / ".claude" / "worktrees" / "1-x"
    (main / ".git" / "worktrees" / "1-x").mkdir(parents=True)
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {(main / '.git' / 'worktrees' / '1-x').as_posix()}\n", encoding="utf-8")
    assert Y.main_root(wt) == main.resolve()
    assert Y.main_root(main) == main
