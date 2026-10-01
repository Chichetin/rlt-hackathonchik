"""How many participants of a notice its own search puts in the top-10, with and without #249 (D-091, review #145 F-13).

    uv run python scripts/own_lot_check.py 6050858 5903085 5875992

The same query is sent twice through the API in-process: as free text (`ui-…`, the notice can find itself) and as
the notice (`lot-<N>`, the notice is left out). Organizers' data must be built (DATA_PROCESSED_DIR); no network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DISCOVERY_OFFLINE", "1")
os.environ.setdefault("ENRICH_OFFLINE", "1")

import duckdb  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.app import app  # noqa: E402
from src.match.store import processed_dir  # noqa: E402


def main(lots: list[str]) -> int:
    client = TestClient(app)
    parts = (processed_dir() / "participants.parquet").as_posix()
    for lot in lots:
        q = client.get(f"/api/lot/{lot}").json()
        sql = f"SELECT supplier_inn FROM read_parquet('{parts}') WHERE lot_id = {int(lot)}"
        own = {r[0] for r in duckdb.sql(sql).fetchall()}
        cells = []
        for qid in ("ui-free", q["query_id"]):
            r = client.post("/api/search?limit=10", json={**q, "query_id": qid})
            top = r.json()["history"]
            itself = sum(any(e["url"] == f"data:items#{lot}" for e in m["counterparty"]["evidence"]) for m in top)
            cells.append(f"{qid}: участников в топ-10 {sum(m['counterparty']['inn'] in own for m in top)}, "
                         f"«лучшее совпадение = сам лот» {itself}")
        print(lot, "|", " || ".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["6050858", "5903085", "5875992"]))
