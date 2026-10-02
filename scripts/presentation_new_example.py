"""Saved answer of block «Новые» for the presentation (#415, jury finding F5 in #388).

    ENRICH_OFFLINE=1 DISCOVERY_OFFLINE=1 uv run python -m scripts.presentation_new_example [LOT]

Runs the product API in-process (FastAPI TestClient, the default configuration) on a lot from the organizers'
data, offline: the registry pages come from the source cache only. Writes presentation/assets/example-new.json:
the query, the X-Facets header and the discovered companies with their components and Evidence — nothing is
edited by hand. Needs data/processed, indexes and cache/sources.sqlite (DATA_PROCESSED_DIR, INDEX_DIR,
SOURCE_CACHE may point elsewhere).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.app import app

OUT = Path(__file__).resolve().parents[1] / "presentation" / "assets" / "example-new.json"


def main(lot: str = "5903085") -> None:
    client = TestClient(app)
    lot_r = client.get(f"/api/lot/{lot}")
    lot_r.raise_for_status()
    query = lot_r.json()
    query = query.get("query", query)
    res = client.post("/api/search", json=query)
    res.raise_for_status()
    body = res.json()
    if not body["discovered"]:
        raise SystemExit(f"лот {lot}: блок «Новые» пуст — нужен кеш реестра МСП по его ОКВЭД")
    OUT.write_text(json.dumps({
        "lot": lot,
        "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "query": query,
        "facets": json.loads(res.headers.get("X-Facets", "{}")),
        "history_count": len(body["history"]),
        "discovered": body["discovered"],
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{OUT}: история {len(body['history'])}, новые {len(body['discovered'])}")


if __name__ == "__main__":
    main(*sys.argv[1:2])
