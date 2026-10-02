"""НОСТРОЙ (#368): SRO membership by INN through the cache, Evidence checked against the cached page; no network."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest

from src.contracts import Counterparty
from src.enrich import enrich as enrich_mod
from src.enrich.evidence import check_evidence
from src.sources import nostroy
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "nostroy"
MEMBER, EXCLUDED, NOBODY = "7839024647", "7802084569", "7707083893"


def _answer(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(nostroy, "MIN_INTERVAL_S", 0.0)
    nostroy.PACER.reset()
    yield
    nostroy.PACER.reset()


@pytest.fixture
def cache(tmp_path) -> SourceCache:
    return SourceCache(tmp_path / "c.sqlite")


def _client(answers: Mapping[str, object], seen: list[dict] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        got = answers[body["filters"]["inn"]]
        return got if isinstance(got, httpx.Response) else httpx.Response(200, json=got)
    return httpx.Client(transport=httpx.MockTransport(handler))


def _checked(res: nostroy.SroResult, cache: SourceCache) -> None:
    for e in res.evidence:
        assert check_evidence(e, cache.page_text) == [], e


def test_member_of_an_sro_with_the_basis_on_the_cached_page(cache):
    seen: list[dict] = []
    res = nostroy.check(MEMBER, cache, _client({MEMBER: _answer("member")}, seen), offline=False)
    assert res.member is True and len(res.evidence) == 1
    fact = res.evidence[0].fact
    assert "Член СРО «Ассоциация «Петровское объединение строителей»» (СРО-С-303-22122020)" in fact
    assert "в реестре с 16.07.2021" in fact and res.evidence[0].field == "sro"
    _checked(res, cache)
    assert seen == [{"filters": {"inn": MEMBER}, "page": 1, "pageCount": "20", "sortBy": {}}]
    page = cache.page_text(nostroy.page_url(MEMBER)) or ""
    assert "record.0.member_status: Является членом" in page  # JSON escaping removed: values occur literally
    raw = cache.page_text(nostroy.raw_url(MEMBER)) or ""
    assert "director" not in page and "director" not in raw  # a natural person's name is not kept
    # a second check reads the cache: no request
    assert nostroy.check(MEMBER, cache, _client({}), offline=False).member is True


def test_excluded_and_absent_are_no_member_with_a_dated_fact(cache):
    client = _client({EXCLUDED: _answer("excluded"), NOBODY: _answer("empty")})
    gone = nostroy.check(EXCLUDED, cache, client, offline=False)
    assert gone.member is False and "статус «Исключен»" in gone.evidence[0].fact
    nobody = nostroy.check(NOBODY, cache, client, offline=False)
    assert nobody.member is False and nobody.evidence[0].fact.startswith("Не найдена в реестре членов СРО НОСТРОЙ на ")
    _checked(gone, cache)
    _checked(nobody, cache)


def test_unclear_answers_are_not_guessed(cache):
    member = _answer("member")["data"]["data"][0]
    excluded = _answer("excluded")["data"]["data"][0]
    deactivated = {**member, "sro": {**member["sro"], "deactivate_message": "Сведения о СРО исключены"}}
    other = member  # asked for NOBODY, the record is of MEMBER
    answers = {
        # a page that does not hold every record: an exclusion here says nothing about the next page
        EXCLUDED: {"success": True, "data": {"data": [excluded], "count": 25}},
        # «member» of an SRO that is itself excluded is not a membership
        MEMBER: {"success": True, "data": {"data": [deactivated], "count": 1}},
        # the registry answered with another INN only
        NOBODY: {"success": True, "data": {"data": [other], "count": 1}},
    }
    for inn in answers:
        assert nostroy.check(inn, cache, _client(answers), offline=False) == nostroy.SroResult(None, [])


def test_current_membership_wins_over_an_old_exclusion(cache):
    member = _answer("member")["data"]["data"][0]
    old = {**_answer("excluded")["data"]["data"][0], "inn": MEMBER}
    both = {"success": True, "data": {"data": [old, member], "count": 2}}
    res = nostroy.check(MEMBER, cache, _client({MEMBER: both}), offline=False)
    assert res.member is True and [e.value for e in res.evidence] == ["СРО-С-303-22122020"]


def test_offline_reads_the_cache_only_and_refusals_are_never_cached(cache):
    with pytest.raises(KeyError):
        nostroy.check(MEMBER, cache, _client({}), offline=True)
    with pytest.raises(nostroy.Blocked):
        nostroy.check(MEMBER, cache, _client({MEMBER: httpx.Response(429)}), offline=False)
    assert cache.get(nostroy.page_url(MEMBER), allow_stale=True) is None
    with pytest.raises(nostroy.Blocked, match="пауза"):  # the host is left alone after a refusal
        nostroy.check(MEMBER, cache, _client({MEMBER: _answer("member")}), offline=False)
    nostroy.PACER.reset()
    with pytest.raises(nostroy.Blocked, match="не ответ реестра"):
        nostroy.check(MEMBER, cache, _client({MEMBER: httpx.Response(200, text="<html>captcha</html>")}),
                      offline=False)
    with pytest.raises(ValueError):
        nostroy.check("7839024640", cache, _client({}), offline=False)  # a bad checksum is never asked


def test_card_shows_the_cached_membership_only(cache):
    nostroy.check(MEMBER, cache, _client({MEMBER: _answer("member")}), offline=False)
    cards = [Counterparty(inn=MEMBER, name="АНО «ВАЛААМ»"), Counterparty(inn=EXCLUDED, name="АО «Эталон ЛенСпецСМУ»")]
    shown, unasked = enrich_mod.add_open_data(cards, cache)
    assert [e.value for e in shown.evidence if e.field == "sro"] == ["СРО-С-303-22122020"]
    assert not [e for e in unasked.evidence if e.field == "sro"]  # never asked: no fact, no request
    fake = shown.evidence[-1].model_copy(update={"value": "СРО-С-999-01012020"})
    assert check_evidence(fake, cache.page_text)  # an invented SRO number fails the Evidence check
