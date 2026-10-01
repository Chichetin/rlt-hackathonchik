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
def _sources_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test talks to an open source by accident: discovery is cache-only and the cache is per test."""
    monkeypatch.setenv("DISCOVERY_OFFLINE", "1")
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources-default.sqlite"))
    # no real network: only httpx.MockTransport answers (a request to a source in a test is a bug)
    import httpx

    def no_network(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"network is closed in unit tests: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    # a captcha / refusal seen by one test must not block the next one (module-level cooldowns)
    from src.sources import bo_nalog, pb_nalog, rmsp

    for mod in (bo_nalog, pb_nalog, rmsp):
        monkeypatch.setattr(mod, "_blocked_until", 0.0)
    # eis_rnp's module-level pacing lock persists across tests in one process (#111, D-063): without
    # this, a test that calls enrich() live (no offline/rnp_offline — the worker's own call shape)
    # shortly after another one can hit a real 60 s Crawl-delay `time.sleep` before `no_network`
    # above even raises. Tests of the pacing itself override this back explicitly.
    from src.sources import eis_rnp

    monkeypatch.setattr(eis_rnp, "MIN_INTERVAL_S", 0.0)


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
