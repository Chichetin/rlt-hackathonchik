"""The one place that reads the environment (#290, arch plan #284).

`get_settings()` is a view of `os.environ`: every attribute reads its own variable when it is asked for, as
each module's own `os.environ.get` did before. So there is no cache to go stale (a test or a `.env` reload sets
a variable after the import and the next access sees it), a hot path such as `tokenize()` pays for one read, and
a garbage value of one variable cannot break the others. Names and defaults live here and in .env.example
(tests/unit/test_settings.py keeps the two in step); `make settings-check` (scripts/check_settings.py) fails on
a new `os.environ` read anywhere else in src/ — files not migrated yet are on scripts/settings_allowlist.txt,
which only shrinks.

An empty value (`NAME=` in .env or in a docker env_file) means «not set», as .env.example shows.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, overload

ROOT = Path(__file__).resolve().parents[1]

ENRICH_QUEUE_MAX_DEFAULT = 2000
COUNTERPARTY_LIVE_MAX_DEFAULT = 2
QUEUE_STALE_RUNNING_DEFAULT_S = 600


def _resolved(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def _directory(default: str) -> Callable[[str | None], Path]:
    """A directory: relative values (and the default) are relative to the repository root."""
    return lambda v: _resolved(Path(v or default))


def _limit(default: int) -> Callable[[str | None], int]:
    """At least 1; garbage falls back to the default (a limit must not stop the service from starting)."""

    def parse(v: str | None) -> int:
        try:
            return max(1, int(v or default))
        except ValueError:
            return default

    return parse


class _Var[T]:
    """One setting: the variable it reads and how to turn its text (None — not set or empty) into a value."""

    def __init__(self, env_name: str, parse: Callable[[str | None], T]) -> None:
        self.env_name = env_name
        self.parse = parse

    @overload
    def __get__(self, obj: None, owner: type) -> _Var[T]: ...

    @overload
    def __get__(self, obj: Settings, owner: type) -> T: ...

    def __get__(self, obj: Settings | None, owner: type) -> _Var[T] | T:
        return self if obj is None else self.parse(obj._environ.get(self.env_name) or None)


class Settings:
    """What the environment says, read at access time (see the module docstring)."""

    # API (src/api)
    demo_fixture = _Var("DEMO_FIXTURE", lambda v: v == "1")  # 1 — synthetic fixture, not organizers' data (D-050)
    enrich_offline = _Var("ENRICH_OFFLINE", lambda v: v == "1")  # 1 — enrichment from the cache only
    api_key = _Var("API_KEY", lambda v: (v or "").strip())  # "" — the guarded routes are open (D-100)
    enrich_queue_max = _Var("ENRICH_QUEUE_MAX", _limit(ENRICH_QUEUE_MAX_DEFAULT))  # pending enrich tasks, ≥ 1
    counterparty_live_max = _Var("COUNTERPARTY_LIVE_MAX", _limit(COUNTERPARTY_LIVE_MAX_DEFAULT))  # live calls, ≥ 1
    ui_dist = _Var("UI_DIST", lambda v: Path(v or ROOT / "src" / "ui" / "dist"))  # as given: relative to the cwd
    ratings_salt = _Var("RATINGS_SALT", lambda v: v or "rlt-ratings")
    testsets_dir = _Var("TESTSETS_DIR", _directory("cache/testsets"))  # test exports uploaded in the UI
    test_data_dir = _Var("TEST_DATA_DIR", lambda v: _resolved(Path(v)) if v else None)  # test export for /api/lot
    # organizers' data and the search over it (src/match)
    data_processed_dir = _Var("DATA_PROCESSED_DIR", _directory("data/processed"))  # ingest output (parquet)
    data_raw_dir = _Var("DATA_RAW_DIR", _directory("data/raw"))  # the three CSV as issued
    data_store = _Var("DATA_STORE", lambda v: (v or "").strip().lower() or "auto")  # validated by get_store()
    data_schema = _Var("DATA_SCHEMA", lambda v: v or "data")  # Postgres schema of the loaded data
    database_url = _Var("DATABASE_URL", lambda v: v or "")  # "" — not set
    index_dir = _Var("INDEX_DIR", _directory("indexes"))  # BM25 indexes
    bm25_tokenizer = _Var("BM25_TOKENIZER", lambda v: v)  # overrides `tokenizer:` of configs/search.yaml
    # block «Новые» (src/match/new_fit.py, #77): off | shadow | on, overrides `mode:` of configs/discover.yaml
    discovery_new_fit = _Var("DISCOVERY_NEW_FIT", lambda v: (v or "").strip() or None)
    # block «Новые» for a query WITHOUT OKPD2, by the class search() predicts from the text (P-02); 0 — off
    discovery_predicted_class = _Var("DISCOVERY_PREDICTED_CLASS", lambda v: v != "0")
    # EIS contract history (src/sources/eis_contracts.py, #244): cache ages, s; cards opened per check
    eis_contracts_search_max_age_s = _Var("EIS_CONTRACTS_SEARCH_MAX_AGE_S", lambda v: float(v or 7 * 24 * 3600))
    eis_contracts_card_max_age_s = _Var("EIS_CONTRACTS_CARD_MAX_AGE_S", lambda v: float(v or 30 * 24 * 3600))
    eis_contracts_max_cards = _Var("EIS_CONTRACTS_MAX_CARDS", lambda v: int(v or 3))
    # worker (src/worker): unlike the two limits above this one always raised on garbage, a typo is not a default
    queue_stale_running_s = _Var("QUEUE_STALE_RUNNING_S", lambda v: int(v or QUEUE_STALE_RUNNING_DEFAULT_S))

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def as_dict(self) -> dict[str, Any]:
        """Every setting with its current value (for tests and diagnostics)."""
        return {name: getattr(self, name) for name, v in vars(Settings).items() if isinstance(v, _Var)}


def env_names() -> tuple[str, ...]:
    """Every variable `Settings` reads, in declaration order."""
    return tuple(v.env_name for v in vars(Settings).values() if isinstance(v, _Var))


def get_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """`Settings` over `environ` (default: the live `os.environ`)."""
    return Settings(os.environ if environ is None else environ)
