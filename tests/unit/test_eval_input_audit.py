"""A stale supplier snapshot must fail learned-ranker eval before it can publish misleading metrics."""

import hashlib
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.contracts import Query
from src.eval import run as R
from src.eval.input_audit import HISTORY_FILES, audit
from src.eval.split import EvalCase
from src.match import store as store_mod

CUTOFF = date(2025, 7, 1)


@pytest.fixture
def profile_input(tmp_path):
    required = (*R.ranker.PROFILE_LOG, *R.ranker.PROFILE_RAW, "top_class")
    pq.write_table(pa.table({name: [None] for name in required}), tmp_path / f"suppliers_{CUTOFF}.parquet")
    (tmp_path / "manifest.json").write_text('{"built_at": "fixture"}', encoding="utf-8")
    for name in HISTORY_FILES:
        pq.write_table(pa.table({"lot_id": [1]}), tmp_path / name)
    model = tmp_path / "ranker.txt"
    model.write_text("model-a", encoding="utf-8")
    return {"ranker": {"kind": "lgbm", "model": str(model)}}


def test_learned_eval_rejects_stale_profile_schema_before_search(tmp_path, monkeypatch):
    # The old aggregate is structurally readable but lacks the profile inputs used by LightGBM.
    pq.write_table(pa.table({"inn": ["7801000012"], "lots_participated": [3]}),
                   tmp_path / f"suppliers_{CUTOFF}.parquet")
    (tmp_path / "manifest.json").write_text('{"built_at": "old"}', encoding="utf-8")
    model = tmp_path / "ranker.txt"
    model.write_text("fixture-model", encoding="utf-8")
    monkeypatch.setattr(R.ranker, "load", lambda spec: None)
    monkeypatch.setattr(store_mod, "get_store", lambda: store_mod.ParquetStore(tmp_path))
    called = []
    monkeypatch.setattr(R, "_rank_search", lambda q, cfg: called.append(q) or [])
    case = EvalCase(query=Query(query_id="q", text="paper"), relevant_inns=[], date=date(2025, 7, 10))
    with pytest.raises(ValueError, match="okpd2_classes") as error:
        R.evaluate([case], {"ranker": {"kind": "lgbm", "model": str(model)}}, cutoff=CUTOFF)
    assert "src.ingest.build" in str(error.value)
    assert not called


def test_profile_nulls_are_legal_and_provenance_names_exact_snapshots(tmp_path, monkeypatch, profile_input):
    required = (*R.ranker.PROFILE_LOG, *R.ranker.PROFILE_RAW, "top_class")
    snapshot = tmp_path / f"suppliers_{CUTOFF}.parquet"
    manifest = tmp_path / "manifest.json"
    model = tmp_path / "ranker.txt"
    monkeypatch.setattr(store_mod, "get_store", lambda: store_mod.ParquetStore(tmp_path))
    cfg = profile_input
    first = audit(cfg, [CUTOFF, CUTOFF])
    assert first["status"] == "checked" and len(first["snapshots"]) == 1
    assert first["manifest"]["sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert first["snapshots"][0]["sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    assert {f["name"] for f in first["snapshots"][0]["schema"]} == set(required)
    model.write_text("model-b", encoding="utf-8")
    second = audit(cfg, [CUTOFF])
    assert first["model"]["sha256"] != second["model"]["sha256"]
    assert first["dataset_sha256"] == second["dataset_sha256"]
    pq.write_table(pa.table({name: [None, None] for name in required}), snapshot)
    assert audit(cfg, [CUTOFF])["dataset_sha256"] != second["dataset_sha256"]
    # A stale snapshot of a different date must never be silently replaced with the good cutoff snapshot.
    earlier = date(2025, 4, 1)
    pq.write_table(pa.table({"inn": ["7801000012"]}), tmp_path / f"suppliers_{earlier}.parquet")
    with pytest.raises(ValueError, match="2025-04-01"):
        audit(cfg, [earlier, CUTOFF])


@pytest.mark.parametrize("name", HISTORY_FILES)
def test_replaced_history_invalidates_fingerprint_even_with_unchanged_manifest_and_snapshot(
        tmp_path, profile_input, name):
    store = store_mod.ParquetStore(tmp_path)
    before = audit(profile_input, [CUTOFF], store=store)
    history = tmp_path / name
    assert before["history"][name]["sha256"] == hashlib.sha256(history.read_bytes()).hexdigest()
    pq.write_table(pa.table({"lot_id": [2]}), history)
    after = audit(profile_input, [CUTOFF], store=store)
    assert before["manifest"] == after["manifest"] and before["snapshots"] == after["snapshots"]
    assert before["model"] == after["model"]
    assert before["dataset_sha256"] != after["dataset_sha256"]


def test_missing_history_fails_instead_of_publishing_incomplete_fingerprint(tmp_path, profile_input):
    (tmp_path / "items.parquet").unlink()
    with pytest.raises(FileNotFoundError, match="items.parquet"):
        audit(profile_input, [CUTOFF], store=store_mod.ParquetStore(tmp_path))


def test_disabled_ranker_does_not_require_a_real_store(monkeypatch):
    def forbidden():
        raise AssertionError("a formula-only eval must not require a learned-ranker input audit")

    monkeypatch.setattr(store_mod, "get_store", forbidden)
    assert audit({}, [CUTOFF])["status"] == "not_checked"


def test_postgres_is_explicitly_unchecked_without_reading_local_parquet(tmp_path, monkeypatch):
    model = tmp_path / "model.txt"
    model.write_text("model", encoding="utf-8")

    class FakePostgres:
        name = "postgres"

    monkeypatch.setattr(store_mod, "get_store", lambda: FakePostgres())

    def forbidden(*args, **kwargs):
        raise AssertionError("Postgres eval must not fingerprint unrelated local parquet files")

    monkeypatch.setattr(pq, "read_schema", forbidden)
    out = audit({"ranker": {"kind": "lgbm", "model": str(model)}}, [CUTOFF])
    assert out["store"] == "postgres" and out["status"] == "not_checked"
    assert "snapshots" not in out and "manifest" not in out
