"""#500: reach of СвПрод (declared products of the MSP registry) on dev — is a «manufacturer» role by it worth it?

    uv run python scripts/svprod_reach.py [--pool data/external/rsmp/pool] [--out eval/results/K3-svprod-reach.json]

For every dev case with OKPD2 and a winner: СПб+ЛО pool companies whose declared product (`company_class` — СвПрод,
src/db/pool.py) is in the lot's OKPD2 class XX or group XX.XX, and whether the lot's winner is among them. Dev only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=Path, default=ROOT / "data" / "external" / "rsmp" / "pool")
    ap.add_argument("--out", type=Path, default=ROOT / "eval" / "results" / "K3-svprod-reach.json")
    args = ap.parse_args()
    text = (ROOT / "eval" / "cases" / "dev.jsonl").read_text(encoding="utf-8")
    cases = [json.loads(line) for line in text.splitlines() if line.strip()]
    con = duckdb.connect()
    cc, co = (args.pool / "company_class.parquet").as_posix(), (args.pool / "companies.parquet").as_posix()
    con.execute(f"CREATE TABLE cc AS SELECT c.inn, c.okpd2, p.region_code FROM read_parquet('{cc}') c "
                f"JOIN read_parquet('{co}') p USING (inn)")
    total, local = con.execute("SELECT count(DISTINCT inn), count(DISTINCT inn) FILTER "
                               "(WHERE region_code IN ('78', '47')) FROM cc").fetchone()  # type: ignore[misc]
    by_class: dict[str, set[str]] = {}
    by_group: dict[str, set[str]] = {}
    for inn, code in con.execute("SELECT inn, okpd2 FROM cc WHERE region_code IN ('78', '47')").fetchall():
        by_class.setdefault(code[:2], set()).add(inn)
        by_group.setdefault(code[:5], set()).add(inn)
    lots = cand_class = cand_group = win_class = win_group = 0
    sizes: list[int] = []
    winners_all: set[str] = set()
    for c in cases:
        codes = [x.strip() for x in c["query"]["okpd2"] if x.strip()]
        winners = {inn for inn, g in c["grades"].items() if g >= 2}
        if not codes or not winners:
            continue
        lots += 1
        winners_all |= winners
        s_class = set().union(*(by_class.get(x[:2], set()) for x in codes))
        s_group = set().union(*(by_group.get(x[:5], set()) for x in codes))
        cand_class += bool(s_class)
        cand_group += bool(s_group)
        sizes.append(len(s_group))
        win_class += bool(winners & s_class)
        win_group += bool(winners & s_group)
    sizes.sort()
    listed = ",".join(f"'{inn}'" for inn in sorted(winners_all) if inn.isdigit())
    with_svprod = con.execute(f"SELECT count(DISTINCT inn) FROM cc WHERE inn IN ({listed})").fetchone()[0]  # type: ignore[index]
    git = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    commit = git.stdout.strip()
    res = {"exp_id": "K3-svprod-reach", "commit": commit, "part": "dev", "companies_with_svprod": total,
           "companies_with_svprod_spb_lo": local, "lots": lots, "lots_with_candidate_class": cand_class,
           "lots_with_candidate_group": cand_group, "candidates_group_median": sizes[len(sizes) // 2],
           "candidates_group_p90": sizes[int(len(sizes) * 0.9)], "winner_in_candidates_class": win_class,
           "winner_in_candidates_group": win_group, "dev_winner_inns": len(winners_all),
           "dev_winners_with_svprod": with_svprod}
    args.out.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
