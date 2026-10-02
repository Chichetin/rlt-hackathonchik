"""src/settings.py: the one place that reads the environment (#290)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src import settings
from src.settings import ROOT, get_settings

ENV_EXAMPLE = ROOT / ".env.example"


def test_defaults_with_an_empty_environment() -> None:
    s = get_settings({})
    assert s.demo_fixture is False and s.enrich_offline is False
    assert s.api_key == ""
    assert s.enrich_queue_max == 2000 and s.counterparty_live_max == 2
    assert s.ui_dist == ROOT / "src" / "ui" / "dist"
    assert s.ratings_salt == "rlt-ratings"
    assert s.testsets_dir == ROOT / "cache" / "testsets"
    assert s.test_data_dir is None
    assert s.data_processed_dir == ROOT / "data" / "processed"
    assert s.data_raw_dir == ROOT / "data" / "raw"
    assert s.data_store == "auto" and s.data_schema == "data" and s.database_url == ""
    assert s.index_dir == ROOT / "indexes"
    assert s.bm25_tokenizer is None
    assert s.queue_stale_running_s == 600


def test_blank_values_are_unset() -> None:
    """`NAME=` in .env / docker env_file sets an empty string: it means «not set», as .env.example shows."""
    blank = {name: "" for name in settings.env_names()}
    assert get_settings(blank).as_dict() == get_settings({}).as_dict()


def test_flags_are_exactly_one() -> None:
    assert get_settings({"DEMO_FIXTURE": "1", "ENRICH_OFFLINE": "1"}).demo_fixture is True
    assert get_settings({"DEMO_FIXTURE": "1", "ENRICH_OFFLINE": "1"}).enrich_offline is True
    for other in ("0", "true", "yes", " 1"):
        s = get_settings({"DEMO_FIXTURE": other, "ENRICH_OFFLINE": other})
        assert (s.demo_fixture, s.enrich_offline) == (False, False), other


def test_api_key_is_stripped() -> None:
    assert get_settings({"API_KEY": "  secret \n"}).api_key == "secret"
    assert get_settings({"API_KEY": "   "}).api_key == ""


def test_data_store_is_normalized_but_not_validated_here() -> None:
    assert get_settings({"DATA_STORE": " Postgres "}).data_store == "postgres"
    assert get_settings({"DATA_STORE": "nonsense"}).data_store == "nonsense"  # get_store() raises on it


def test_database_url_and_schema_are_taken_as_given() -> None:
    s = get_settings({"DATABASE_URL": "postgresql+psycopg://u:p@h/db", "DATA_SCHEMA": "data_v2"})
    assert (s.database_url, s.data_schema) == ("postgresql+psycopg://u:p@h/db", "data_v2")


def test_relative_directories_resolve_against_the_repo_root(tmp_path: Path) -> None:
    s = get_settings({"DATA_PROCESSED_DIR": "x/proc", "DATA_RAW_DIR": "x/raw", "INDEX_DIR": "x/idx",
                      "TESTSETS_DIR": "x/ts", "TEST_DATA_DIR": "x/td"})
    assert s.data_processed_dir == ROOT / "x" / "proc" and s.data_raw_dir == ROOT / "x" / "raw"
    assert s.index_dir == ROOT / "x" / "idx" and s.testsets_dir == ROOT / "x" / "ts"
    assert s.test_data_dir == ROOT / "x" / "td"
    absolute = get_settings({"DATA_PROCESSED_DIR": str(tmp_path), "TEST_DATA_DIR": str(tmp_path)})
    assert absolute.data_processed_dir == tmp_path and absolute.test_data_dir == tmp_path


def test_ui_dist_is_taken_as_given() -> None:
    """UI_DIST was never resolved against the root (a relative value meant the working directory): kept."""
    assert get_settings({"UI_DIST": "dist-x"}).ui_dist == Path("dist-x")


def test_tokenizer_and_salt_overrides() -> None:
    s = get_settings({"BM25_TOKENIZER": "snowball", "RATINGS_SALT": "pepper"})
    assert s.bm25_tokenizer == "snowball" and s.ratings_salt == "pepper"


@pytest.mark.parametrize(("raw", "expected"), [("5", 5), ("0", 1), ("-3", 1), ("", 2000), ("abc", 2000), ("2.5", 2000)])
def test_queue_max_is_at_least_one_and_falls_back_on_garbage(raw: str, expected: int) -> None:
    assert get_settings({"ENRICH_QUEUE_MAX": raw}).enrich_queue_max == expected


@pytest.mark.parametrize(("raw", "expected"), [("7", 7), ("0", 1), ("", 2), ("abc", 2)])
def test_live_max_is_at_least_one_and_falls_back_on_garbage(raw: str, expected: int) -> None:
    assert get_settings({"COUNTERPARTY_LIVE_MAX": raw}).counterparty_live_max == expected


def test_stale_running_seconds_fail_fast_on_garbage() -> None:
    """Unlike the two limits above this one always raised at import: a typo must not turn into a default."""
    assert get_settings({"QUEUE_STALE_RUNNING_S": "30"}).queue_stale_running_s == 30
    with pytest.raises(ValueError):
        get_settings({"QUEUE_STALE_RUNNING_S": "ten minutes"}).queue_stale_running_s  # noqa: B018


def test_a_garbage_value_breaks_only_its_own_setting() -> None:
    """Each setting is parsed when asked for, so one typo does not take the unrelated ones down with it."""
    s = get_settings({"QUEUE_STALE_RUNNING_S": "ten minutes"})
    assert s.data_schema == "data" and s.api_key == ""


class _Spy(dict[str, str]):
    """A mapping that remembers which variables were read."""

    def __init__(self, **values: str) -> None:
        super().__init__(values)
        self.read: list[str] = []

    def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
        self.read.append(key)
        return super().get(key, default)


def test_one_access_reads_one_variable() -> None:
    """tokenize() asks for the tokenizer once per document while an index is built: it must cost one read."""
    spy = _Spy(BM25_TOKENIZER="snowball")
    s = get_settings(spy)
    assert spy.read == []  # building the view reads nothing
    assert s.bm25_tokenizer == "snowball"
    assert spy.read == ["BM25_TOKENIZER"]


def test_reads_the_environment_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """No cache: tests (and `.env` reloads) set a variable after import and expect the next call to see it."""
    monkeypatch.setenv("DATA_SCHEMA", "first")
    assert get_settings().data_schema == "first"
    monkeypatch.setenv("DATA_SCHEMA", "second")
    assert get_settings().data_schema == "second"


def test_env_names_are_unique_upper_snake_case() -> None:
    names = settings.env_names()
    assert len(names) == len(set(names)) and len(names) > 10
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", n) for n in names)


def test_every_setting_is_documented_in_env_example() -> None:
    """A variable that exists only in the code is invisible to whoever deploys: .env.example lists them all
    (a commented `# NAME=` line counts — it documents the default without overriding it)."""
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M))
    missing = sorted(set(settings.env_names()) - documented)
    assert not missing, f"нет в .env.example: {missing}"


def test_gis2_reviews_defaults_and_overrides() -> None:
    """2GIS reviews on demand (#234, D-142): pacing, timeouts, the off switch and the live-read limit."""
    s = get_settings({})
    assert (s.gis2_min_interval_s, s.gis2_timeout_s, s.gis2_down_cooldown_s) == (3.0, 20.0, 90.0)
    assert s.gis2_disabled is False and s.reviews_live_max == 2
    s = get_settings({"GIS2_MIN_INTERVAL_S": "0.5", "GIS2_TIMEOUT_S": "5", "GIS2_DOWN_COOLDOWN_S": "30",
                      "REVIEWS_LIVE_MAX": "4"})
    assert (s.gis2_min_interval_s, s.gis2_timeout_s, s.gis2_down_cooldown_s, s.reviews_live_max) == (0.5, 5.0, 30.0, 4)


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), (" Yes ", True), ("0", False),
                                               ("no", False), ("", False)])
def test_gis2_disabled_switch(raw: str, expected: bool) -> None:
    assert get_settings({"GIS2_DISABLED": raw}).gis2_disabled is expected


@pytest.mark.parametrize(("raw", "expected"), [("0", 1), ("abc", 2), ("", 2)])
def test_reviews_live_max_is_at_least_one_and_falls_back_on_garbage(raw: str, expected: int) -> None:
    assert get_settings({"REVIEWS_LIVE_MAX": raw}).reviews_live_max == expected


def test_gis2_pacing_fails_fast_on_garbage() -> None:
    """As before #290 moved it here: a typo in a pause must not silently become the default."""
    with pytest.raises(ValueError):
        get_settings({"GIS2_MIN_INTERVAL_S": "three"}).gis2_min_interval_s  # noqa: B018
