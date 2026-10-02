"""Fingerprint learned-ranker inputs and reject stale supplier profile schemas before backtesting."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

from src.match import ranker
from src.match import store as store_mod

FINGERPRINT_VERSION = 2
HISTORY_FILES = ("notices.parquet", "participants.parquet", "items.parquet")


def _sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def audit(cfg: dict, as_ofs: Iterable[date], *, store: store_mod.Store | None = None) -> dict:
    """Only inspect the selected store and snapshots; absent values are legal, absent columns are not."""
    spec = ranker.spec(cfg) if cfg.get("method", "search") == "search" else None
    if spec is None:
        return {"status": "not_checked", "reason": "no learned ranker"}
    model = Path(spec["model"])
    if not model.is_absolute():
        model = ranker.ROOT / model
    provenance = {"model": {"path": str(model), "sha256": _sha(model)}}
    store = store if store is not None else store_mod.get_store()
    if not isinstance(store, store_mod.ParquetStore):
        return {**provenance, "store": store.name, "status": "not_checked",
                "reason": "supplier profile schema and data fingerprints are checked only for ParquetStore"}
    required = set((*ranker.PROFILE_LOG, *ranker.PROFILE_RAW, "top_class"))
    snapshots = []
    for as_of in sorted(set(as_ofs)):
        path = store.processed / f"suppliers_{as_of.isoformat()}.parquet"
        schema = pq.read_schema(path)
        if missing := required - set(schema.names):
            # build(..., cutoff=...) rebuilds this exact snapshot, including an earlier per-case as_of.
            code = ("from datetime import date; from pathlib import Path; import os; "
                    "from src.ingest.build import ROOT, build; "
                    "build(Path(os.environ.get('DATA_RAW_DIR') or ROOT / 'data/raw'), "
                    f"Path({str(store.processed)!r}), cutoff=date.fromisoformat({as_of.isoformat()!r}))")
            raise ValueError(f"{path}: устаревшая схема профиля, нет столбцов: {', '.join(sorted(missing))}. "
                             f"Пересоберите: uv run python -c {shlex.quote(code)}")
        snapshots.append({"as_of": as_of.isoformat(), "path": str(path), "sha256": _sha(path),
                          "schema": [{"name": f.name, "type": str(f.type), "nullable": f.nullable} for f in schema]})
    manifest = store.processed / "manifest.json"
    manifest_sha = _sha(manifest)
    # The manifest and supplier aggregates can stay unchanged while history is replaced.
    # Retrieval and customer candidate counts read these files directly, so hash their bytes too.
    history = {name: {"path": str(store.processed / name), "sha256": _sha(store.processed / name)}
               for name in HISTORY_FILES}
    fingerprint = {"version": FINGERPRINT_VERSION, "manifest_sha256": manifest_sha,
                   "history": {name: item["sha256"] for name, item in history.items()},
                   "snapshots": [{"as_of": s["as_of"], "sha256": s["sha256"]} for s in snapshots]}
    return {**provenance, "store": store.name, "status": "checked",
            "fingerprint_version": FINGERPRINT_VERSION,
            "manifest": {"path": str(manifest), "sha256": manifest_sha}, "snapshots": snapshots, "history": history,
            "dataset_sha256": hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()}
