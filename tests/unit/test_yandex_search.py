"""Yandex Search API client (#344): the cache pays once, offline reads the cache, the daily cap holds."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
import pytest

from src.sources import yandex_search as ys
from src.sources.cache import SourceCache

# A real answer of 02.10 (3 results); the INN of a natural person in <extended-text> is replaced by a
# synthetic checksum-valid one (152-ФЗ).
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "yandex_search" / "kanc_spb_3.xml"
QUERY = "поставщик канцтоваров Санкт-Петербург ИНН"


@pytest.fixture
def xml() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a fresh pacer: a block() of one test must not pause the next one
    monkeypatch.setattr(ys, "PACER", ys.HostPacer(0.0, ys.BLOCK_COOLDOWN_S))
    monkeypatch.setenv("YANDEX_SEARCH_CACHE", str(tmp_path / "default-ys.sqlite"))
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "test-key")
    monkeypatch.delenv("YANDEX_SEARCH_FOLDER_ID", raising=False)
    monkeypatch.delenv("YANDEX_SEARCH_OFFLINE", raising=False)
    monkeypatch.delenv("ENRICH_OFFLINE", raising=False)
    monkeypatch.setenv("YANDEX_SEARCH_DAILY_MAX", "100")
    monkeypatch.setenv("YANDEX_SEARCH_MIN_INTERVAL_S", "0")


class Api:
    """A fake endpoint: counts paid requests, remembers the last body."""

    def __init__(self, xml: str, status: int = 200, timeout: bool = False) -> None:
        self.xml, self.status, self.timeout, self.calls = xml, status, timeout, 0
        self.last: dict = {}

    def client(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            self.last = json.loads(request.content.decode("utf-8"))
            assert request.headers["Authorization"] == "Api-Key test-key"
            if self.timeout:  # the request left, the answer did not come
                raise httpx.ReadTimeout("timed out", request=request)
            if self.status != 200:
                return httpx.Response(self.status, text="refused")
            return httpx.Response(200, json={"rawData": base64.b64encode(self.xml.encode("utf-8")).decode()})

        return httpx.Client(transport=httpx.MockTransport(handle))


def test_parse_real_answer(xml: str) -> None:
    found, hits = ys.parse_xml(xml)
    assert found == 7422663
    assert [h.domain for h in hits] == ["www.rusprofile.ru", "checko.ru", "star-pro.ru"]
    first = hits[0]
    assert first.url == "https://www.rusprofile.ru/id/6812177"
    assert first.title.startswith('ООО "Канцпроф СПБ" Санкт-Петербург (ИНН 7813553580)')  # tags gone, entities decoded
    assert "7813553580" in first.inns
    assert "770000000082" not in first.inns  # extended-text (synthetic INN in the fixture) is not a snippet
    assert all(ys.inn_is_valid(i) for h in hits for i in h.inns)


def test_no_results_is_an_empty_answer() -> None:
    error = '<error code="15">Искомая комбинация слов нигде не встречается</error>'
    xml = f"<yandexsearch><response>{error}</response></yandexsearch>"
    assert ys.parse_xml(xml) == (0, [])


def test_other_xml_error_raises() -> None:
    with pytest.raises(ys.YandexSearchError, match="ошибка 32"):
        ys.parse_xml('<yandexsearch><response><error code="32">лимит</error></response></yandexsearch>')


def test_normalized_queries_share_one_cache_key() -> None:
    assert ys.search_url("  Канцтовары   СПб ") == ys.search_url("канцтовары спб")
    assert ys.search_url("ёлки") == ys.search_url("елки")
    assert ys.search_url("a", n=5) != ys.search_url("a", n=10)
    assert ys.search_url("a", region="2").endswith("&lr=2")
    with pytest.raises(ValueError):
        ys.normalize("   ")


def test_second_identical_search_is_free(tmp_path: Path, xml: str) -> None:
    cache, api = SourceCache(tmp_path / "s.sqlite"), Api(xml)
    first = ys.search(QUERY, 3, cache=cache, client=api.client())
    again = ys.search("  Поставщик  канцтоваров санкт-петербург инн", 3, cache=cache, client=api.client())
    assert api.calls == 1
    assert first is not None and again is not None
    assert not first.from_cache and again.from_cache
    assert again.hits == first.hits and again.found == 7422663
    assert api.last["query"] == {"searchType": "SEARCH_TYPE_RU", "queryText": QUERY.lower(), "page": "0"}
    assert api.last["groupSpec"]["groupsOnPage"] == "3"
    assert "folderId" not in api.last and "region" not in api.last
    assert ys.spent_today(cache) == 1
    # the cached text holds the INN literally: evidence.py can check a value extracted from it
    assert "7813553580" in (cache.page_text(first.search_url) or "")


def test_offline_reads_cache_only(tmp_path: Path, xml: str, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, api = SourceCache(tmp_path / "s.sqlite"), Api(xml)
    monkeypatch.setenv("YANDEX_SEARCH_OFFLINE", "1")
    assert ys.search(QUERY, 3, cache=cache, client=api.client()) is None
    assert api.calls == 0
    ys.search(QUERY, 3, cache=cache, client=api.client(), offline_only=False)
    answer = ys.search(QUERY, 3, cache=SourceCache(tmp_path / "s.sqlite", ttl_s=0), client=api.client())
    assert answer is not None and answer.from_cache and api.calls == 1  # stale is fine offline


def test_daily_cap_stops_paid_requests(tmp_path: Path, xml: str, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, api = SourceCache(tmp_path / "s.sqlite"), Api(xml)
    monkeypatch.setenv("YANDEX_SEARCH_DAILY_MAX", "1")
    ys.search("первый", 3, cache=cache, client=api.client())
    with pytest.raises(ys.BudgetExceeded):
        ys.search("второй", 3, cache=cache, client=api.client())
    assert api.calls == 1
    assert ys.search("первый", 3, cache=cache, client=api.client()) is not None  # the cache is still free


def test_cap_reached_returns_stale_answer(tmp_path: Path, xml: str, monkeypatch: pytest.MonkeyPatch) -> None:
    api = Api(xml)
    ys.search(QUERY, 3, cache=SourceCache(tmp_path / "s.sqlite"), client=api.client())
    monkeypatch.setenv("YANDEX_SEARCH_DAILY_MAX", "1")
    expired = SourceCache(tmp_path / "s.sqlite", ttl_s=0)
    answer = ys.search(QUERY, 3, cache=expired, client=api.client())
    assert answer is not None and answer.from_cache and api.calls == 1


def test_refusal_is_not_cached(tmp_path: Path, xml: str) -> None:
    cache = SourceCache(tmp_path / "s.sqlite")
    with pytest.raises(ys.YandexSearchError, match="HTTP 401"):
        ys.search(QUERY, 3, cache=cache, client=Api(xml, status=401).client())
    assert cache.get(ys.search_url(QUERY, 3), allow_stale=True) is None
    assert ys.spent_today(cache) == 1  # the request left: counted (conservatively), not cached


def test_no_key_sends_nothing(tmp_path: Path, xml: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY")
    api = Api(xml)
    with pytest.raises(ys.YandexSearchError, match="YANDEX_SEARCH_API_KEY"):
        ys.search(QUERY, 3, cache=SourceCache(tmp_path / "s.sqlite"), client=api.client())
    assert api.calls == 0


def test_dotenv_does_not_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text('# comment\nYANDEX_SEARCH_API_KEY=from-file\nYANDEX_SEARCH_REGION="2"\n', encoding="utf-8")
    monkeypatch.delenv("YANDEX_SEARCH_REGION", raising=False)
    ys._load_dotenv(env)
    assert ys.api_key() == "test-key"
    assert os.environ["YANDEX_SEARCH_REGION"] == "2"


RATE_ERROR = ("<yandexsearch><response><error code=\"55\">Превышено число запросов в секунду</error>"
              "</response></yandexsearch>")


def test_xml_error_is_not_cached_but_counted_and_pauses(tmp_path: Path) -> None:
    cache, api = SourceCache(tmp_path / "s.sqlite"), Api(RATE_ERROR)
    with pytest.raises(ys.YandexSearchError, match="ошибка 55"):
        ys.search(QUERY, 3, cache=cache, client=api.client())
    assert cache.get(ys.search_url(QUERY, 3), allow_stale=True) is None
    assert ys.spent_today(cache) == 1  # the request left: it may be billed
    with pytest.raises(ys.YandexSearchError, match="пауза"):
        ys.search("другой запрос", 3, cache=cache, client=api.client())
    assert api.calls == 1 and ys.spent_today(cache) == 1  # paused: nothing sent, nothing counted


def test_429_pauses_the_next_request(tmp_path: Path, xml: str) -> None:
    cache = SourceCache(tmp_path / "s.sqlite")
    with pytest.raises(ys.YandexSearchError, match="HTTP 429"):
        ys.search(QUERY, 3, cache=cache, client=Api(xml, status=429).client())
    api = Api(xml)
    with pytest.raises(ys.YandexSearchError, match="пауза"):
        ys.search(QUERY, 3, cache=cache, client=api.client())
    assert api.calls == 0


def test_timeout_after_sending_counts_and_pauses(tmp_path: Path, xml: str) -> None:
    cache, api = SourceCache(tmp_path / "s.sqlite"), Api(xml, timeout=True)
    with pytest.raises(ys.YandexSearchError, match="ReadTimeout"):
        ys.search(QUERY, 3, cache=cache, client=api.client())
    assert ys.spent_today(cache) == 1
    assert ys.PACER.blocked()


def test_every_payment_is_counted(tmp_path: Path, xml: str) -> None:
    api = Api(xml)
    expired = SourceCache(tmp_path / "s.sqlite", ttl_s=0)  # every call misses: every call pays
    ys.search(QUERY, 3, cache=expired, client=api.client())
    ys.search(QUERY, 3, cache=expired, client=api.client())
    assert api.calls == 2 and ys.spent_today(expired) == 2


def test_default_cache_is_shared_by_worktrees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main = tmp_path / "repo"
    assert ys.main_checkout(main / ".claude" / "worktrees" / "344-x") == main
    assert ys.main_checkout(main) == main
    monkeypatch.delenv("YANDEX_SEARCH_CACHE")
    assert ys.cache_path() == ys.main_checkout() / "cache" / "yandex_search.sqlite"
