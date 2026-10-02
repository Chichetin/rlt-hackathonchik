"""Shared fixtures: ingest output built from the synthetic CSV (tests/fixtures/ingest/README.md)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from src.ingest.build import build

FIXTURE_INGEST = Path(__file__).resolve().parents[1] / "fixtures" / "ingest"
CUTOFF = date(2025, 7, 1)


@pytest.fixture(autouse=True)
def _sources_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test talks to an open source by accident: discovery is cache-only and the cache is per test."""
    monkeypatch.setenv("DISCOVERY_OFFLINE", "1")
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources-default.sqlite"))
    monkeypatch.setenv("FNS_OPENDATA_PARQUET", str(tmp_path / "no-fns-opendata.parquet"))  # not the dev machine's
    # a test export uploaded in the UI (cache/testsets/active) must not answer /api/lot in tests (#207)
    monkeypatch.setenv("TESTSETS_DIR", str(tmp_path / "testsets-default"))
    # no real network: only httpx.MockTransport answers (a request to a source in a test is a bug)
    import httpx

    def no_network(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"network is closed in unit tests: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    # a captcha / refusal seen by one test must not block the next one, and a request of one test must not
    # make the next one wait (the pacers are module-level, src/sources/pacing.py)
    from src.sources import bo_nalog, eis_rnp, pb_nalog, rmsp

    pacers = [mod.PACER for mod in (bo_nalog, eis_rnp, pb_nalog, rmsp)]
    for pacer in pacers:
        pacer.reset()
    # enrich() merges РНП ЕИС (#111): a test that calls enrich() live (the worker's own call shape) twice must not
    # sit out the real 60 s Crawl-delay pause before `no_network` above even raises. Pacing tests set it back.
    monkeypatch.setattr(eis_rnp, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(eis_rnp, "_ANSWERS", {})  # parsed РНП answers are memoized per page: none from another test
    # web leads (#108): no test depends on the real configs/leads; tests/unit/test_web_leads.py sets its own
    from src.sources import web_leads

    monkeypatch.setattr(web_leads, "LEADS_DIR", tmp_path / "leads-default")
    # the registry export of industrial products (#74): not the dev machine's; tests/unit/test_gisp.py builds its own
    from src.sources import gisp

    monkeypatch.setattr(gisp, "DIR", tmp_path / "gisp-default")
    yield
    for pacer in pacers:
        pacer.reset()


@pytest.fixture(scope="session")
def fixture_processed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("processed-fixture")
    build(FIXTURE_INGEST, out, as_of=None, cutoff=CUTOFF)
    return out


@pytest.fixture
def search_data(fixture_processed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """search() reads the fixture; a fresh index dir and in-memory cache per test."""
    from src.match import index

    monkeypatch.setenv("DATA_PROCESSED_DIR", str(fixture_processed))
    monkeypatch.setenv("DATA_STORE", "parquet")  # tests on Postgres set DATA_STORE=postgres themselves
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "indexes"))
    index.clear_memory_cache()
    yield fixture_processed
    index.clear_memory_cache()


@pytest.fixture
def v2_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    """The product config without the learned ranker (#198): tests of the v2 formula itself — scores as the
    weighted sum of components, the fallback ladder depths. The ranker on top is tested in test_ranker.py."""
    from src.match import search as search_mod

    plain = search_mod.load_config

    def load_config(*args, **kwargs) -> dict:
        return {k: v for k, v in plain(*args, **kwargs).items() if k != "ranker"}

    monkeypatch.setattr(search_mod, "load_config", load_config)
