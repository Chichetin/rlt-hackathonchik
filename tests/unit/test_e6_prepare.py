"""Preparing a private E6 store must satisfy readiness without changing its source."""

from datetime import date

import pyarrow.parquet as pq
import pytest

from scripts.e6_prepare import prepare
from src.match.store import ParquetStore

CUTOFF = date(2025, 7, 1)


def test_private_store_is_ready_and_rebuilds_dated_snapshot_without_touching_source(fixture_processed, tmp_path):
    output = tmp_path / "private"
    output.mkdir()
    dated = f"suppliers_{CUTOFF}.parquet"
    source_snapshot = (fixture_processed / dated).read_bytes()
    (output / dated).symlink_to(fixture_processed / dated)
    # The source fixture has no eval directory, as with data copied without generated cases.
    private_eval = output / "eval"
    private_eval.mkdir()
    dev_full = private_eval / "dev_full.jsonl"
    dev_full.write_text("private dev cases\n", encoding="utf-8")

    prepare(fixture_processed, output, [CUTOFF])

    store = ParquetStore(output)
    assert store.manifest() == ParquetStore(fixture_processed).manifest()
    assert store.index_rows(CUTOFF)[0]  # actual store readiness and history reads, no mocked manifest
    assert (output / "suppliers.parquet").resolve() == fixture_processed / "suppliers.parquet"
    assert (output / "suppliers.parquet").is_symlink()
    assert not (output / dated).is_symlink()
    assert (fixture_processed / dated).read_bytes() == source_snapshot
    assert all(day < CUTOFF for day in pq.read_table(output / dated)["last_seen"].to_pylist())
    assert dev_full.read_text(encoding="utf-8") == "private dev cases\n"
    assert not private_eval.is_symlink()
    # Resume on the same directory without replacing the private evaluation cases.
    prepare(fixture_processed, output, [])
    assert dev_full.read_text(encoding="utf-8") == "private dev cases\n"


def test_missing_source_eval_does_not_create_a_dangling_link(fixture_processed, tmp_path):
    output = tmp_path / "private"
    prepare(fixture_processed, output, [])
    assert not (output / "eval").exists() and not (output / "eval").is_symlink()
    prepare(fixture_processed, output, [])
    assert ParquetStore(output).manifest()


def test_existing_source_eval_is_linked_and_conflicting_private_cases_are_refused(fixture_processed, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("notices.parquet", "participants.parquet", "items.parquet", "suppliers.parquet", "manifest.json"):
        (source / name).symlink_to(fixture_processed / name)
    (source / "eval").mkdir()
    output = tmp_path / "private"
    prepare(source, output, [])
    assert (output / "eval").is_symlink() and (output / "eval").resolve() == source / "eval"
    prepare(source, output, [])
    (output / "eval").unlink()
    (output / "eval").mkdir()
    with pytest.raises(ValueError, match="another dataset"):
        prepare(source, output, [])
