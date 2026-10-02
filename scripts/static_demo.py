"""Answers of the API for the static demo stand (#283): `make static-demo` → dist-static/ opens without the API.

Records, through the real FastAPI app in this process (no server, no network — DISCOVERY_OFFLINE=1, the
enrichment from the cache only), what the UI asks for on the demo path:
- POST /api/search (all candidates, limit = CANDIDATES) for the example queries of the UI and the real lots of
  eval/cases/real.jsonl, with the headers the UI reads (totals, facets, notes, corrections, warnings);
- GET /api/lot/<id> and /api/lot/<id>/backtest for those lots;
- GET /api/okpd2 for every OKPD2 code of the queries and of the suppliers' profiles.
The file holds organizers' data (INNs, statistics): it goes to cache/static-demo/ (not in git) and into the
built stand only; publishing the stand is an external action — human confirmation №4 (CLAUDE.md).

    uv run python scripts/static_demo.py [--out cache/static-demo/static-demo.json] [--query "текст" ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXAMPLES = ["Бумага офисная А4, 80 г/м²", "Офисная мебель", "Компьютерное оборудование"]  # src/ui/src/App.tsx
HEADERS = ("X-Total-History", "X-Total-Discovered", "X-Facets", "X-Search-Context", "X-Search-Note", "X-Excluded-Lot",
           "X-Discovery-Error", "X-Query-Corrected", "X-Weak-Match")
UI_FILTERS = {"near_customer": "auto"}  # what buildQuery() sends with NO_FILTERS (src/ui/src/api.ts)
_CODE = re.compile(r"\d{2}\.\d{2}(?:\.\d{1,2}(?:\.\d{3})?)?")


def key(text: str, okpd2: list[str] | None) -> str:
    """The lookup key of a search — the same function in src/ui/src/staticApi.ts."""
    norm = " ".join(text.lower().replace("ё", "е").split())
    return f"{norm}|{','.join(sorted(okpd2 or []))}"


def real_lots(path: Path) -> list[str]:
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            qid = json.loads(line)["query"]["query_id"]
            if qid.startswith("lot-"):
                ids.append(qid.split("-", 1)[1])
    return ids


def record(client, examples: list[str], lots: list[str]) -> dict:  # noqa: ANN001 — fastapi TestClient
    from src.match import search as search_mod

    out: dict = {"searches": {}, "lots": {}, "backtests": {}, "okpd2": {}, "examples": examples, "lot_ids": []}
    queries: list[dict] = [{"query_id": f"static-ex-{i}", "text": t, "filters": UI_FILTERS, "region": "Санкт-Петербург"}
                           for i, t in enumerate(examples)]
    for lot in lots:
        r = client.get(f"/api/lot/{lot}")
        if r.status_code != 200:
            print(f"лот {lot}: {r.status_code} — пропущен", file=sys.stderr)
            continue
        q = r.json()
        out["lots"][lot] = q
        out["lot_ids"].append(lot)
        queries.append({**q, "filters": UI_FILTERS})
        b = client.get(f"/api/lot/{lot}/backtest")
        if b.status_code == 200:
            out["backtests"][lot] = b.json()
    codes: set[str] = set()
    for q in queries:
        r = client.post(f"/api/search?limit={search_mod.CANDIDATES}&offset=0", json=q)
        if r.status_code != 200:
            print(f"запрос «{q['text'][:60]}»: {r.status_code} — пропущен", file=sys.stderr)
            continue
        body = r.json()
        out["searches"][key(q["text"], q.get("okpd2"))] = {
            "body": body, "headers": {h: r.headers[h] for h in HEADERS if h in r.headers}}
        codes.update(q.get("okpd2") or [])
        for m in body["history"] + body["discovered"]:
            codes.update(_CODE.findall(str(m["counterparty"].get("stats", {}).get("top_okpd2", ""))))
        print(f"«{q['text'][:60]}»: история {len(body['history'])}, новые {len(body['discovered'])}")
    if codes:
        r = client.get(f"/api/okpd2?codes={quote(','.join(sorted(codes)))}")
        if r.status_code == 200:
            out["okpd2"] = r.json()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "cache" / "static-demo" / "static-demo.json")
    ap.add_argument("--query", action="append", default=[], help="ещё один текстовый запрос для стенда")
    ap.add_argument("--cases", type=Path, default=ROOT / "eval" / "cases" / "real.jsonl")
    args = ap.parse_args(argv)
    os.environ.setdefault("DISCOVERY_OFFLINE", "1")  # cache only: the stand shows what the cache holds
    os.environ.setdefault("ENRICH_OFFLINE", "1")
    from fastapi.testclient import TestClient

    from src.api.app import app

    with TestClient(app) as client:
        rec = record(client, [*EXAMPLES, *args.query], real_lots(args.cases) if args.cases.exists() else [])
    if not rec["searches"]:
        print("ни одного ответа /api/search — данные организаторов не загружены?", file=sys.stderr)
        return 2
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    rec["built_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    rec["commit"] = commit
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    print(f"-> {args.out} ({args.out.stat().st_size / 1e6:.1f} МБ): запросов {len(rec['searches'])}, "
          f"лотов {len(rec['lots'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
