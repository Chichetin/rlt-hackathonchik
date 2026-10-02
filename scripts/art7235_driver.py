"""Runs the engine and the baselines of art7235/RLT_hack on given lots (#342); per-lot ranks go to a json file.

Their code stays in their clone (not copied here); this file only drives it. Run from the root of the clone,
with its own requirements installed and its DuckDB + indexes built from the organizers' CSV (their README):

    TH_DATA_DIR=<dir with raw/ and tender.duckdb> PYTHONPATH=. \\
        python <this repo>/scripts/art7235_driver.py ids.csv 2025-01-01 art_A.json

ids.csv — the lots our `src.eval.external` kept (column lot_id); the date is the history cutoff of both systems.
Hit@10 = a winner of the lot is in their top-10 for the full card (name, positions, OKPD2, customer, price, platform).

`--name-only` (#491): their own «только название» input, Query(text=..., before_date=cutoff), with the text from the
`text` column of ids.csv (`src.eval.external --name-only --dump-ids`), so both systems get the same string;
per lot `name` (rank) and `recall`, no baselines (they need OKPD2 and the customer).
"""

# pyright: reportMissingImports=false
import json
import sys
from collections import defaultdict
from datetime import date

import app.eval.evaluate as ev
import app.eval.procedure_eval as pe
import duckdb
import numpy as np
import pandas as pd
from app.config import DB_PATH
from app.search import engine as eng_mod
from app.search.engine import OKPD_LEVEL, Query


def name_only(ids_csv: str, cutoff: date, out: str) -> None:
    ev.SPLIT = pe.SPLIT = cutoff
    pe.LOTS_FILE = type(pe.LOTS_FILE)(ids_csv)
    texts = dict(pd.read_csv(ids_csv, dtype={"text": str})[["lot_id", "text"]].itertuples(index=False))
    engine = eng_mod.get_engine()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    engine.profile, engine.sokpd = ev._history_tables(con)
    lots = pe.test_lots(con, len(texts))
    con.close()
    rows = []
    for i, row in enumerate(lots.itertuples(index=False)):
        query = Query(text=texts[row.lot_id], limit=ev.K, before_date=cutoff)
        top = [s["inn"] for s in engine.search(query)["suppliers"]]
        hit = [j for j, x in enumerate(top[:ev.K]) if x in set(row.winners)]
        rows.append({"lot_id": int(row.lot_id), "platform": row.platform, "name": hit[0] + 1 if hit else 0,
                     "recall": len(set(row.participants) & set(top)) / len(set(row.participants))})
        if (i + 1) % 100 == 0:
            print(i + 1, "/", len(lots), flush=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"cutoff": str(cutoff), "n": len(rows), "mode": "name-only", "rows": rows}, f, ensure_ascii=False)
    print("name hit@10", round(float(np.mean([r["name"] > 0 for r in rows])), 4), "n", len(rows))


def main(ids_csv: str, cutoff: date, out: str) -> None:
    ev.SPLIT = pe.SPLIT = cutoff  # their history cutoff: both modules read it as a global
    pe.LOTS_FILE = type(pe.LOTS_FILE)(ids_csv)
    k = ev.K
    engine = eng_mod.get_engine()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    engine.profile, engine.sokpd = ev._history_tables(con)
    lots = pe.test_lots(con, len(pd.read_csv(ids_csv)))
    cust_cls: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    for cust, cls, inn, w in con.execute(f"""
        SELECT l.customer_inn, substr(i.okpd2_code, 1, 5), p.inn, count(DISTINCT l.lot_id)
        FROM participations p JOIN lots l USING (lot_id)
        JOIN (SELECT DISTINCT lot_id, okpd2_code FROM lot_items WHERE okpd2_code IS NOT NULL) i USING (lot_id)
        WHERE p.is_winner AND l.publish_date < DATE '{cutoff}' GROUP BY ALL""").fetchall():
        cust_cls[(cust, cls)][inn] += w
    con.close()
    by_code = {c: g.sort_values("n_wins", ascending=False)["inn"].head(50).tolist()
               for c, g in engine.sokpd.groupby("code8")}
    rows = []
    for i, row in enumerate(lots.itertuples(index=False)):
        card = pe._card(row)
        winners, parts = set(row.winners), set(row.participants)

        def rank_of(top: list, winners: set = winners) -> int:
            hit = [j for j, x in enumerate(top[:k]) if x in winners]
            return hit[0] + 1 if hit else 0

        full = [s["inn"] for s in engine.search(Query(limit=k, before_date=cutoff, **card))["suppliers"]]
        top_code, seen = [], set()
        for c in dict.fromkeys(c[:OKPD_LEVEL] for c in card["okpd_codes"] if c):
            for inn in by_code.get(c, []):
                if inn not in seen:
                    seen.add(inn)
                    top_code.append(inn)
        cc: dict[str, int] = defaultdict(int)
        for cls in {c[:5] for c in card["okpd_codes"] if c}:
            for inn, w in cust_cls.get((row.customer_inn, cls), {}).items():
                cc[inn] += w
        strong = sorted(cc, key=cc.__getitem__, reverse=True) + [x for x in top_code if x not in cc]
        rows.append({"lot_id": int(row.lot_id), "platform": row.platform, "card": rank_of(full),
                     "recall": len(parts & set(full)) / len(parts), "base_okpd": rank_of(top_code),
                     "base_customer": rank_of(strong)})
        if (i + 1) % 100 == 0:
            print(i + 1, "/", len(lots), flush=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"cutoff": str(cutoff), "n": len(rows), "rows": rows}, f, ensure_ascii=False)
    for name in ("card", "base_customer", "base_okpd"):
        print(name, "hit@10", round(float(np.mean([r[name] > 0 for r in rows])), 4), "n", len(rows))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--name-only"]
    (name_only if "--name-only" in sys.argv else main)(args[0], date.fromisoformat(args[1]), args[2])
