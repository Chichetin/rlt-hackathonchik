"""Would a registry contact confirm a 2GIS card? Coverage measure before building it (#357, step 1).

    uv run python -m src.eval.review_contacts --exp R-contacts-v1 [--limit 40]

Sample: the top-10 of both blocks («С опытом», «Новые») of the queries of src/eval/fns_coverage.py (the e2e demo
lot and the 8 real cases), legal entities only (2GIS reviews are not searched for an individual entrepreneur),
first `--limit` distinct INNs. Per INN: the phone / email of the МСП registry quick search (src/sources/rmsp.py,
cached), the name and region of the ФНС card (enrich, cached), then the product's own `find_reviews` with those
contacts — the same comparison /api/reviews would make if enrich() returned contacts. Counted: with a registry
contact, a 2GIS card found, a card confirmed by the contact (basis phone / email / site), by the unique legal name,
by the brand only. Every request goes through SourceCache with the sources' own pacing; a second run is free.
Writes eval/results/<exp>.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import UTC, datetime

import httpx

from src.contracts import Counterparty
from src.eval.fns_coverage import BLOCKS, queries
from src.eval.run import RESULTS, git
from src.sources import rmsp
from src.sources.cache import SourceCache

log = logging.getLogger(__name__)
CONTACT_BASIS = ("phone", "email", "site")


def registry_rows(inn: str, cache: SourceCache) -> int | None:
    """`answer.inn_rows` of the cached quick answer: did the registry answer for this INN (None — no page)."""
    page = cache.get(rmsp.quick_url(inn), allow_stale=True)
    for line in (page.body if page else "").splitlines():
        if line.startswith("answer.inn_rows: "):
            return int(line.split(": ", 1)[1])
    return None


def registry_contacts(inn: str, cache: SourceCache) -> dict[str, str]:
    """Phone and email of the МСП registry quick search (the `row.<inn>.phone|email` lines of the cached page)."""
    page = rmsp.fetch_quick(inn, cache)
    out: dict[str, str] = {}
    for line in (page.body if page else "").splitlines():
        key, _, value = line.partition(": ")
        field = key.rsplit(".", 1)[-1]
        if key.startswith(f"row.{inn}.") and field in ("phone", "email") and value.strip() and field not in out:
            out[field] = value.strip()
    return out


def sample(limit: int) -> list[dict]:
    """Distinct 10-digit INNs of the top-10 of both blocks, query by query, until `limit` — with the query and
    the block each came from. The blocks are read offline, so the sample depends on SOURCE_CACHE (region,
    «Новые»): reproduce with the same cache."""
    from src.match import pipeline

    seen: dict[str, dict] = {}
    for q in queries():
        res = pipeline.pick(q, offline=True)
        for block in BLOCKS:
            for m in [m for m in res.matches if m.origin == block][:10]:
                inn = m.counterparty.inn
                if inn and len(inn) == 10 and inn not in seen:
                    seen[inn] = {"inn": inn, "query_id": q.query_id, "block": block}
    return list(seen.values())[:limit]


def check(picked: dict, cache: SourceCache) -> dict:
    from src.enrich import reviews
    from src.enrich.enrich import enrich

    inn = picked["inn"]
    row: dict = dict(picked)
    try:
        card = enrich(inn, cache, offline=False, open_data=False)
        contacts = registry_contacts(inn, cache)
    except (KeyError, ValueError, TimeoutError, httpx.HTTPError, RuntimeError) as e:
        row["outcome"] = f"no_card: {type(e).__name__}"
        return row
    row |= {"name": card.name, "region": card.region, "contacts": sorted(contacts),
            "registry_rows": registry_rows(inn, cache)}
    cp = Counterparty(inn=inn, name=card.name, region=card.region, kpp=card.kpp, contacts=contacts)
    res = reviews.find_reviews(cp, cache)
    row["status"] = res.status
    row["bases"] = [c.basis for c in res.cards]
    row["outcome"] = ("confirmed" if any(b in CONTACT_BASIS for b in row["bases"])
                      else "legal_name" if "legal_name" in row["bases"]
                      else "name_only" if row["bases"] else res.status)
    return row


def measure(limit: int, cache: SourceCache) -> dict:
    rows = [check(p, cache) for p in sample(limit)]
    for r in rows:
        log.info("%s %-12s %-40s %s", r["inn"], r["outcome"], (r.get("name") or "")[:40],
                 ",".join(r.get("contacts", [])))
    n = len(rows)
    with_contact = [r for r in rows if r.get("contacts")]
    found = [r for r in rows if r.get("bases")]
    outcomes = Counter(r["outcome"] for r in rows)
    return {
        "n": n,
        "registry_answered": sum(1 for r in rows if r.get("registry_rows")),
        "by_query": dict(Counter(r["query_id"] for r in rows)),
        "by_block": dict(Counter(r["block"] for r in rows)),
        "with_registry_contact": len(with_contact),
        "card_found": len(found),
        "card_found_with_contact": sum(1 for r in with_contact if r.get("bases")),
        "confirmed_by_contact": outcomes.get("confirmed", 0),
        "confirmed_share": round(outcomes.get("confirmed", 0) / n, 4) if n else None,
        "outcomes": dict(outcomes),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Подтверждение карточки 2ГИС контактом реестра МСП — охват (#357)")
    ap.add_argument("--exp", required=True)
    ap.add_argument("--limit", type=int, default=40, help="компаний в выборке (≥ 30 по задаче)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    body = measure(args.limit, SourceCache())
    rec = {"exp_id": args.exp, "kind": "review_contacts", "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
           "commit": git("rev-parse", "--short", "HEAD"), "limit": args.limit, **body}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in rec.items() if k != "rows"}, ensure_ascii=False, indent=1))
    print(f"-> {out.relative_to(RESULTS.parents[1]).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
