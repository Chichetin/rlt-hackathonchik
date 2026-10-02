"""`python -m src.enrich.fns_lookup <inn>`: ФНС open-data facts of one INN and the evidence check of each (#59).

The facts come from the offline source (src/sources/fns_opendata.py); the check is enrich's
(src/enrich/evidence.py). The command lives here, not in the source: a source does not import enrich (#294, D-106).
"""

from __future__ import annotations

import sys

from src.enrich.evidence import check_evidence
from src.sources.cache import SourceCache
from src.sources.fns_opendata import lookup


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    cache = SourceCache()
    for e in lookup(argv[0], cache):
        errors = check_evidence(e, cache.page_text)
        print(f"{e.fact}\n  {e.source} | {e.url} | {e.fetched_at:%Y-%m-%d %H:%M} UTC | {e.field} = {e.value!r}"
              f" | проверка: {'OK' if not errors else errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
