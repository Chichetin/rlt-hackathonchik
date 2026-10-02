"""РНП ЕИС on the demo lots (D-154): the shortlist for the РНП pass and the coverage of the cache.

    uv run python scripts/rnp_shortlist.py 5903085 5875992 6050858 > shortlist.txt     # top-10 of each lot
    uv run python -m src.worker.prefetch --rnp --inns-file shortlist.txt                 # live, 1 INN/min
    uv run python scripts/rnp_shortlist.py --coverage 5903085 5875992 6050858          # what the demo shows

The lots go through the API in-process, as in the demo (`/api/lot/<N>` → `/api/search?limit=10`), offline: the
search reads the source cache (SOURCE_CACHE) only. `--coverage` counts, per lot, the shortlist INNs with a cached
РНП answer whose Evidence passes (src/enrich/evidence.py) and what the history block shows (`in_rnp` set or not;
an INN in the РНП is dropped by the hard filter, so the block itself has none). Organizers' data must be built.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DISCOVERY_OFFLINE", "1")
os.environ.setdefault("ENRICH_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

from src.api.app import app  # noqa: E402
from src.enrich.evidence import cached_pages, check_evidence  # noqa: E402
from src.sources import eis_rnp  # noqa: E402
from src.sources.cache import SourceCache  # noqa: E402


def top10(client: TestClient, lot: str) -> list[dict]:
    q = client.get(f"/api/lot/{lot}").json()
    return client.post("/api/search?limit=10", json=q).json()["history"]


def rnp_backed(inn: str, cache: SourceCache) -> bool | None:
    """The cached РНП answer of the INN with a passing Evidence; None — no answer in the cache."""
    try:
        res = eis_rnp.check(inn, cache, offline=True)
    except KeyError:
        return None
    pages = cached_pages(cache.page_text)
    return res.in_rnp is not None and any(not check_evidence(e, pages) for e in res.evidence)


def main(argv: list[str]) -> int:
    coverage = "--coverage" in argv
    lots = [a for a in argv if not a.startswith("--")] or ["5903085", "5875992", "6050858"]
    client = TestClient(app)
    cache = SourceCache()
    seen: set[str] = set()
    for lot in lots:
        top = top10(client, lot)
        inns = [m["counterparty"]["inn"] for m in top if m["counterparty"].get("inn")]
        if not coverage:
            print(f"# lot {lot}: top-{len(inns)} of the history block")
            for inn in inns:
                if inn not in seen:
                    print(inn)
                seen.add(inn)
            continue
        answers = [rnp_backed(inn, cache) for inn in inns]
        shown = sum(m["counterparty"].get("in_rnp") is not None for m in top)
        ok, weak, none = (sum(a is v for a in answers) for v in (True, False, None))
        print(f"lot {lot}: top-{len(inns)}, РНП в кеше с проверенным Evidence {ok}, "
              f"ответ без подтверждения {weak}, нет в кеше {none}; в выдаче in_rnp указан у {shown}")
        seen.update(inns)
    if coverage:
        print(f"всего ИНН в топ-10 лотов: {len(seen)}, РНП в кеше с проверенным Evidence: "
              f"{sum(rnp_backed(i, cache) is True for i in seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
