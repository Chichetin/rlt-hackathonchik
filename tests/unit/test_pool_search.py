"""«Новые» of /api/search from the offline pool (#518): the from_pool() path of `make predict`, offline."""

from __future__ import annotations

import zipfile
from pathlib import Path

import duckdb
import pytest

from src.contracts import Query
from src.enrich import discover as D
from src.enrich.evidence import check_counterparty
from src.match import pipeline
from src.sources.cache import SourceCache

FIX = Path(__file__).resolve().parents[1] / "fixtures"
POOL_INN = "0078000013"  # a ЮЛ of СПб with the main OKVED 17.12 in tests/fixtures/msp_dump/sample.xml
WEIGHTS = {"product_fit": 0.5, "geography": 0.2}
PAPER = Query(query_id="q", text="бумага", okpd2=["17.12.14.110"])


@pytest.fixture(scope="module")
def pool_dir(tmp_path_factory) -> Path:
    from src.db import pool
    from src.sources import msp_dump

    tmp = tmp_path_factory.mktemp("pool518")
    zip_path = tmp / "dump.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("a.xml", (FIX / "msp_dump" / "sample.xml").read_text(encoding="utf-8"))
    msp_dump.build(zip_path, tmp / "parts", workers=1)
    pool.build(tmp / "parts", tmp / "pool", fetched_at=1_789_000_000.0)
    return tmp / "pool"


@pytest.fixture
def with_pool(monkeypatch, pool_dir) -> Path:
    monkeypatch.setattr(D, "pool_source", lambda: (None, pool_dir))
    monkeypatch.setattr(D, "_POOL_MEMO", {})
    monkeypatch.setattr(D, "_POOL_ROWS", {})
    return pool_dir


def _suppliers(root: Path, inns: list[str]) -> Path:
    out = root / "processed"
    out.mkdir()
    con = duckdb.connect()
    con.execute("CREATE TABLE s AS SELECT unnest(?) AS inn", [inns])
    con.execute(f"COPY s TO '{(out / 'suppliers.parquet').as_posix()}' (FORMAT parquet)")
    con.close()
    return out


def _run(processed: Path, cache: SourceCache, **kw) -> D.Discovery:
    return D.discover(PAPER, cache=cache, weights=WEIGHTS, processed=processed, offline=True, **kw)


def test_pool_company_is_new_with_evidence(with_pool, tmp_path):
    cache = SourceCache(tmp_path / "c.sqlite")
    res = _run(_suppliers(tmp_path, ["7801000012"]), cache, pool=True)
    new = [m for m in res.matches if m.counterparty.inn == POOL_INN]
    assert len(new) == 1 and new[0].origin == "discovered"
    cp = new[0].counterparty
    assert cp.evidence and check_counterparty(cp, cache.page_text) == []  # every fact backed by a cached page
    assert res.relation[POOL_INN] == "class" and D.STEP[res.relation[POOL_INN]] == 0.6
    assert any(r.startswith("пул: кандидатов 1, новых к остальным каналам 1") for r in res.requests)
    assert D.POOL_ABSENT not in res.notes


def test_organizers_supplier_before_as_of_is_not_new(with_pool, tmp_path):
    res = _run(_suppliers(tmp_path, [POOL_INN]), SourceCache(tmp_path / "c.sqlite"), pool=True)
    assert POOL_INN not in {m.counterparty.inn for m in res.matches + res.candidates}
    assert any(r.startswith("пул: кандидатов 0") for r in res.requests)


def test_no_pool_keeps_the_block_and_notes_once(tmp_path):
    processed = _suppliers(tmp_path, ["7801000012"])
    off = _run(processed, SourceCache(tmp_path / "a.sqlite"))
    on = _run(processed, SourceCache(tmp_path / "b.sqlite"), pool=True)  # conftest: no pool on this machine
    assert [m.counterparty.inn for m in on.matches] == [m.counterparty.inn for m in off.matches]
    assert on.notes.count(D.POOL_ABSENT) == 1 and D.POOL_ABSENT not in off.notes


def test_pool_is_off_unless_asked(with_pool, tmp_path):
    """make predict adds the pool itself (_with_pool): discover() without pool=True does not."""
    res = _run(_suppliers(tmp_path, ["7801000012"]), SourceCache(tmp_path / "c.sqlite"))
    assert POOL_INN not in {m.counterparty.inn for m in res.matches}
    assert not any(r.startswith("пул:") for r in res.requests)


def test_broken_pool_never_breaks_the_block(monkeypatch, tmp_path):
    def boom() -> tuple[str | None, Path | None]:
        raise OSError("disk")

    monkeypatch.setattr(D, "pool_source", boom)
    res = _run(_suppliers(tmp_path, ["7801000012"]), SourceCache(tmp_path / "c.sqlite"), pool=True)
    assert "пул: ошибка OSError — канал пропущен" in res.requests


def test_pool_rows_are_read_once_per_process(with_pool, monkeypatch, tmp_path):
    from src.db import pool as pool_mod

    calls: list[str] = []
    real = pool_mod.candidates
    monkeypatch.setattr(pool_mod, "candidates", lambda *a, **k: calls.append("c") or real(*a, **k))
    processed = _suppliers(tmp_path, ["7801000012"])
    cache = SourceCache(tmp_path / "c.sqlite")
    first = D.from_pool(PAPER, PAPER.okpd2, predicted=False, weights=WEIGHTS, processed=processed, cache=cache,
                        pool_dir=with_pool)
    again = D.from_pool(PAPER, PAPER.okpd2, predicted=False, weights=WEIGHTS, processed=processed, cache=cache,
                        pool_dir=with_pool)
    assert calls == ["c", "c"]  # the query's region and the neighbour, then the memo
    assert [m.counterparty.inn for m in again[0]] == [m.counterparty.inn for m in first[0]] == [POOL_INN]


def test_same_query_is_answered_from_memory(with_pool, monkeypatch, tmp_path):
    processed = _suppliers(tmp_path, ["7801000012"])
    first = _run(processed, SourceCache(tmp_path / "c.sqlite"), pool=True)
    monkeypatch.setattr(D, "from_pool", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read again")))
    again = _run(processed, SourceCache(tmp_path / "c.sqlite"), pool=True)
    assert [m.counterparty.inn for m in again.matches] == [m.counterparty.inn for m in first.matches]
    assert any("из памяти процесса" in r for r in again.requests)
    (tmp_path / "x").mkdir()
    other = _run(_suppliers(tmp_path / "x", [POOL_INN]), SourceCache(tmp_path / "c.sqlite"), pool=True)
    # other organizers' data: not answered from the memo
    assert any(r.startswith("пул: ошибка AssertionError") for r in other.requests)


def test_api_search_pipeline_adds_pool_and_keeps_history(search_data, with_pool, monkeypatch):
    """pipeline.pick() — the /api/search path: the pool company is in «Новые», «С опытом» is unchanged."""
    q = PAPER.model_copy(update={"query_id": "p518"})
    got = pipeline.pick(q)
    monkeypatch.setattr(D, "pool_source", lambda: (None, None))
    base = pipeline.pick(q)
    assert POOL_INN in {m.counterparty.inn for m in got.discovered}
    assert POOL_INN not in {m.counterparty.inn for m in base.discovered}
    assert [(m.counterparty.inn, m.score) for m in got.history] == [(m.counterparty.inn, m.score) for m in base.history]
    assert base.discovery is not None and D.POOL_ABSENT in base.discovery.notes


def test_company_of_another_channel_keeps_that_row(with_pool, tmp_path):
    from src.match.store import store_for

    processed = _suppliers(tmp_path, ["7801000012"])
    res = D.Discovery()
    added, relation = D.pool_matches(PAPER, PAPER.okpd2, {POOL_INN}, res, WEIGHTS,
                                     SourceCache(tmp_path / "c.sqlite"), store_for(processed))
    assert added == [] and relation == {}
    assert any(r.startswith("пул: кандидатов 1, новых к остальным каналам 0") for r in res.requests)


def test_additional_okved_keeps_its_step_and_takes_a_producer_slot(with_pool, monkeypatch, tmp_path):
    """An additional OKVED of the class: STEP «extra» (0.5) in Discovery.relation, a producer's slot in the block."""
    real = D.from_pool

    def as_extra(*a, **k):
        found, relation, checked = real(*a, **k)
        return found, {inn: "extra" for inn in relation}, checked

    monkeypatch.setattr(D, "from_pool", as_extra)
    res = _run(_suppliers(tmp_path, ["7801000012"]), SourceCache(tmp_path / "c.sqlite"), pool=True)
    assert res.relation[POOL_INN] == "extra" and D.STEP["extra"] == 0.5
    assert POOL_INN in {m.counterparty.inn for m in res.matches}
