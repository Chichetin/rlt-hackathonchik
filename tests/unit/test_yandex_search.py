"""Yandex Search API client (#194, #186): XML → documents, off by default, the ceilings per lot and per run,
the hard request limit, cache does not spend, deferred mode, refusals.

`tests/fixtures/yandex_search/vaccines_spb.xml` is the real XML answer of the probe of 01.10 («поставщик
вакцин оптом Санкт-Петербург», region 2); `operation_*.json` are built from the API specification, not
recorded (tests/fixtures/yandex_search/README.md). No test talks to the network: conftest closes httpx, every
answer comes from httpx.MockTransport, and the key is the literal `test-key`.
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
    monkeypatch.setenv("YANDEX_MAX_PER_LOT", "50")
    monkeypatch.setenv("YANDEX_MAX_PER_RUN", "100")
    monkeypatch.setenv("YANDEX_SEARCH_MODE", "sync")  # the deferred tests ask for their mode themselves
    monkeypatch.setenv("YANDEX_BUDGET_DB", str(tmp_path / "budget.sqlite"))
    monkeypatch.setattr(Y, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(Y, "_blocked_until", 0.0)
    monkeypatch.setattr(Y, "_run_caps", None)
    monkeypatch.setattr(Y, "_dotenv", lambda name: None)  # a developer's .env never decides a test


def _forbidden() -> httpx.Client:
    """A transport that fails the test on ANY request: «nothing was sent» is checked, not assumed."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a request was sent while Yandex must be silent: {request.method} {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


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
    assert sent["query"]["page"] == "0" and "page" not in sent  # SearchQuery.page (search_query.proto)
    assert str(calls[0].url) == Y.ENDPOINT
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


# --- the hard limit of 2500 ---------------------------------------------------------------------------------

def test_request_2501_raises_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("YANDEX_SEARCH_MAX_REQUESTS", raising=False)
    budget = Y.Budget()
    assert budget.limit == 2500
    budget.record(2499, "прежние запросы")
    cache = SourceCache(tmp_path / "c.sqlite")
    calls: list[httpx.Request] = []
    assert Y.search("запрос 2500", cache=cache, client=_client(calls), budget=budget) is not None
    assert len(calls) == 1 and budget.used() == 2500
    with pytest.raises(Y.BudgetExceeded):
        Y.search("запрос 2501", cache=cache, client=_forbidden(), budget=budget)
    assert budget.used() == 2500
    assert Y.search("запрос 2500", cache=cache, client=_forbidden(), budget=budget) is not None  # cache is free


def test_limit_is_read_from_settings(monkeypatch):
    monkeypatch.setenv("YANDEX_SEARCH_MAX_REQUESTS", "300")
    assert Y.Budget().limit == 300


# --- off by default: the key and both ceilings ---------------------------------------------------------------

@pytest.mark.parametrize(("env", "reason"), [
    ({"YANDEX_SEARCH_API_KEY": ""}, "нет ключа"),
    ({"YANDEX_SEARCH_API_KEY": "your_yandex_search_api_key_here"}, "нет ключа"),
    ({"YANDEX_MAX_PER_LOT": ""}, "нет потолка (YANDEX_MAX_PER_LOT)"),
    ({"YANDEX_MAX_PER_RUN": ""}, "нет потолка (YANDEX_MAX_PER_RUN)"),
    ({"YANDEX_MAX_PER_LOT": "", "YANDEX_MAX_PER_RUN": ""}, "нет потолка (YANDEX_MAX_PER_LOT, YANDEX_MAX_PER_RUN)"),
    ({"YANDEX_MAX_PER_LOT": "0"}, "нет потолка"),
    ({"YANDEX_MAX_PER_RUN": "-5"}, "нет потолка"),
    ({"YANDEX_MAX_PER_RUN": "много"}, "нет потолка"),
    ({"YANDEX_MAX_PER_LOT": "3.5"}, "нет потолка"),
])
@pytest.mark.parametrize("mode", ["sync", "deferred"])
def test_off_without_key_or_ceiling_sends_nothing(tmp_path, monkeypatch, env, reason, mode):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    g = Y.gate()
    assert not g.enabled and g.reason.startswith("выключен") and reason in g.reason
    budget = Y.Budget(limit=10)
    cache = SourceCache(tmp_path / "c.sqlite")
    with pytest.raises(Y.Disabled, match="выключен"):
        Y.search(QUERY, cache=cache, client=_forbidden(), budget=budget, mode=mode, caps=Y.Caps(5, 5))
    assert budget.used() == 0
    cache.put(Y.operation_url(Y.search_url(QUERY)), json.dumps({"id": "op1"}))
    with pytest.raises(Y.Disabled):  # a status request is a request to Yandex too
        Y.collect(QUERY, cache=cache, client=_forbidden())


def test_off_is_the_default_state(monkeypatch):
    for name in ("YANDEX_SEARCH_API_KEY", "YANDEX_MAX_PER_LOT", "YANDEX_MAX_PER_RUN"):
        monkeypatch.delenv(name)
    assert not Y.gate().enabled and "нет ключа" in Y.gate().reason
    with pytest.raises(Y.Disabled):
        Y.fetch_raw(QUERY, client=_forbidden(), budget=Y.Budget(limit=10))
    assert (Y.run_caps().per_lot, Y.run_caps().per_run) == (0, 0)


def test_on_with_key_and_both_ceilings():
    g = Y.gate()
    assert g.enabled and (g.per_lot, g.per_run) == (50, 100) and g.reason.startswith("включён")


def test_empty_environment_value_overrides_dotenv(monkeypatch):
    monkeypatch.setattr(Y, "_dotenv", lambda name: "from-dotenv")
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY")
    assert Y.setting("YANDEX_SEARCH_API_KEY") == "from-dotenv"
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "")  # `YANDEX_SEARCH_API_KEY= <cmd>` switches Yandex off
    assert Y.setting("YANDEX_SEARCH_API_KEY") is None and not Y.gate().enabled


# --- ceilings per lot and per run ------------------------------------------------------------------------------

def test_ceiling_per_lot(tmp_path):
    calls: list[httpx.Request] = []
    budget, caps = Y.Budget(limit=100), Y.Caps(per_lot=2, per_run=10)
    cache = SourceCache(tmp_path / "c.sqlite")
    for q in ("а оптом", "а производитель"):
        Y.search(q, cache=cache, client=_client(calls), budget=budget, caps=caps, lot="L1")
    assert caps.left("L1") == 0 and caps.left("L2") == 2
    with pytest.raises(Y.CapExceeded, match="на лот"):
        Y.search("а дилер", cache=cache, client=_forbidden(), budget=budget, caps=caps, lot="L1")
    assert len(calls) == 2 and budget.used() == 2  # the refused request spent neither money nor the hard limit
    Y.search("б оптом", cache=cache, client=_client(calls), budget=budget, caps=caps, lot="L2")  # another lot
    assert len(calls) == 3 and caps.used_run == 3


def test_ceiling_per_run(tmp_path):
    calls: list[httpx.Request] = []
    budget, caps = Y.Budget(limit=100), Y.Caps(per_lot=2, per_run=3)
    cache = SourceCache(tmp_path / "c.sqlite")
    for lot, q in (("L1", "а"), ("L1", "б"), ("L2", "в")):
        Y.search(q, cache=cache, client=_client(calls), budget=budget, caps=caps, lot=lot)
    assert caps.left("L2") == 0 and caps.left("L3") == 0
    with pytest.raises(Y.CapExceeded, match="на прогон"):
        Y.search("г", cache=cache, client=_forbidden(), budget=budget, caps=caps, lot="L3")
    assert len(calls) == 3 and budget.used() == 3


def test_cached_answer_spends_no_ceiling(tmp_path):
    calls: list[httpx.Request] = []
    budget, caps = Y.Budget(limit=100), Y.Caps(per_lot=1, per_run=1)
    cache = SourceCache(tmp_path / "c.sqlite")
    Y.search(QUERY, cache=cache, client=_client(calls), budget=budget, caps=caps, lot="L1")
    for _ in range(3):  # the ceilings are exhausted, the cache still answers
        assert Y.search(QUERY, cache=cache, client=_forbidden(), budget=budget, caps=caps, lot="L1") is not None
    assert (caps.used_run, caps.used_lot, budget.used()) == (1, {"L1": 1}, 1)


def test_process_wide_ceiling_when_no_caps_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("YANDEX_MAX_PER_LOT", "1")
    calls: list[httpx.Request] = []
    cache, budget = SourceCache(tmp_path / "c.sqlite"), Y.Budget(limit=100)
    Y.search("а", cache=cache, client=_client(calls), budget=budget)
    with pytest.raises(Y.CapExceeded):
        Y.search("б", cache=cache, client=_forbidden(), budget=budget)
    assert Y.run_caps().used_run == 1


# --- deferred mode (fixtures built from the specification — [ПРОВЕРИТЬ] not checked live) ----------------------

def _op(name: str) -> dict:
    return json.loads((FIX / f"operation_{name}.json").read_text(encoding="utf-8"))


def _deferred_client(calls: list[httpx.Request], statuses: list[dict], submit: dict | None = None) -> httpx.Client:
    """POST searchAsync → the pending operation; every GET operations/<id> → the next of `statuses`."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            assert str(request.url) == Y.ASYNC_ENDPOINT
            return httpx.Response(200, json=submit or _op("pending"))
        assert str(request.url) == Y.OPERATION_ENDPOINT + _op("pending")["id"]
        return httpx.Response(200, json=statuses.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_deferred_submit_then_collect_on_the_next_call(tmp_path):
    calls: list[httpx.Request] = []
    budget, caps = Y.Budget(limit=10), Y.Caps(5, 5)
    cache = SourceCache(tmp_path / "c.sqlite")
    client = _deferred_client(calls, [_op("pending"), _op("done")])
    kw = {"cache": cache, "client": client, "budget": budget, "caps": caps, "lot": "L1", "mode": "deferred"}
    assert Y.search(QUERY, **kw) is None  # submitted, not waited for
    assert [c.method for c in calls] == ["POST"] and budget.used() == 1 and caps.used_run == 1
    assert calls[0].headers["Authorization"] == "Api-Key test-key"
    assert json.loads(calls[0].content)["query"]["queryText"] == QUERY
    assert Y.search(QUERY, **kw) is None  # still running: one status request, nothing paid
    page = Y.search(QUERY, **kw)  # ready
    assert [c.method for c in calls] == ["POST", "GET", "GET"]
    assert budget.used() == 1 and caps.used_run == 1  # one paid request for the whole exchange
    assert page is not None
    docs = Y.parse(page.body).docs
    assert [d.domain for d in docs] == ["brass.example.ru", "spb.2gis.ru"]
    assert cache.get(Y.search_url(QUERY) + "#raw") is not None
    assert Y.search(QUERY, **{**kw, "client": _forbidden()}) is not None  # now cached: free


def test_deferred_waits_and_polls(tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(Y.time, "sleep", slept.append)
    calls: list[httpx.Request] = []
    client = _deferred_client(calls, [_op("pending"), _op("done")])
    page = Y.search(QUERY, cache=SourceCache(tmp_path / "c.sqlite"), client=client, budget=Y.Budget(limit=10),
                    caps=Y.Caps(5, 5), mode="deferred", wait_s=60, poll_s=7)
    assert page is not None and [c.method for c in calls] == ["POST", "GET", "GET"] and slept == [7, 7]


def test_deferred_gives_up_after_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(Y.time, "sleep", lambda s: None)
    calls: list[httpx.Request] = []
    client = _deferred_client(calls, [])
    budget = Y.Budget(limit=10)
    page = Y.search(QUERY, cache=SourceCache(tmp_path / "c.sqlite"), client=client, budget=budget,
                    caps=Y.Caps(5, 5), mode="deferred", wait_s=1, poll_s=5)  # no time for a single poll
    assert page is None and [c.method for c in calls] == ["POST"] and budget.used() == 1


@pytest.mark.parametrize("wait_s", [0, 60])
def test_deferred_answer_ready_at_once(tmp_path, monkeypatch, wait_s):
    slept: list[float] = []
    monkeypatch.setattr(Y.time, "sleep", slept.append)
    calls: list[httpx.Request] = []
    client = _deferred_client(calls, [], submit=_op("done"))  # a status request would fail: no statuses left
    page = Y.search(QUERY, cache=SourceCache(tmp_path / "c.sqlite"), client=client, budget=Y.Budget(limit=10),
                    caps=Y.Caps(5, 5), mode="deferred", wait_s=wait_s, poll_s=5)
    assert page is not None and len(calls) == 1 and slept == []


def test_deferred_error_is_not_an_answer_and_next_call_submits_again(tmp_path):
    calls: list[httpx.Request] = []
    budget, caps = Y.Budget(limit=10), Y.Caps(5, 5)
    cache = SourceCache(tmp_path / "c.sqlite")
    client = _deferred_client(calls, [_op("error")])
    kw = {"cache": cache, "client": client, "budget": budget, "caps": caps, "mode": "deferred"}
    assert Y.search(QUERY, **kw) is None
    with pytest.raises(Y.OperationFailed, match="Internal error"):
        Y.search(QUERY, **kw)
    assert cache.get(Y.search_url(QUERY)) is None  # no answer was invented
    assert Y.search(QUERY, **kw) is None  # a new paid request, counted
    assert [c.method for c in calls] == ["POST", "GET", "POST"] and budget.used() == 2 and caps.used_run == 2


def test_deferred_gone_operation_is_forgotten(tmp_path):
    cache = SourceCache(tmp_path / "c.sqlite")
    cache.put(Y.operation_url(Y.search_url(QUERY)), json.dumps({"id": "old"}))
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"code": 5})))
    assert Y.collect(QUERY, cache=cache, client=client) is None
    assert Y.collect(QUERY, cache=cache, client=_forbidden()) is None  # nothing pending any more


def test_deferred_respects_ceilings_and_budget(tmp_path):
    budget, caps = Y.Budget(limit=10), Y.Caps(per_lot=0, per_run=5)
    with pytest.raises(Y.CapExceeded):
        Y.search(QUERY, cache=SourceCache(tmp_path / "c.sqlite"), client=_forbidden(), budget=budget, caps=caps,
                 mode="deferred")
    budget.record(10, "всё потрачено")
    with pytest.raises(Y.BudgetExceeded):
        Y.search(QUERY, cache=SourceCache(tmp_path / "c2.sqlite"), client=_forbidden(), budget=budget,
                 caps=Y.Caps(5, 5), mode="deferred")


def test_deferred_is_the_default_mode_and_cheaper(monkeypatch):
    monkeypatch.delenv("YANDEX_SEARCH_MODE")
    assert Y.search_mode() == "deferred"
    monkeypatch.setenv("YANDEX_SEARCH_MODE", "SYNC")
    assert Y.search_mode() == "sync"
    assert Y.cost_rub(1000, "deferred") == 30.5 and Y.cost_rub(1000, "sync") == 488.0
    assert Y.cost_rub(3, "deferred") == 0.09


def test_cli_budget_makes_no_request(capsys, monkeypatch):
    monkeypatch.delenv("YANDEX_MAX_PER_RUN")
    assert Y.main(["budget"]) == 0
    out = capsys.readouterr().out
    assert "выключен: нет потолка (YANDEX_MAX_PER_RUN)" in out and "израсходовано 0 из" in out
    assert "test-key" not in out
