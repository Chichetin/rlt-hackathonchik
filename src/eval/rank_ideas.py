"""One-off experiment X-rank-ideas (#577): do three features from another team's ranker lift our LightGBM?

    DATA_PROCESSED_DIR=<fresh ingest + suppliers_2025-04-01.parquet> INDEX_DIR=<own> DATA_STORE=parquet \\
    uv run python -m src.eval.rank_ideas collect --part train|dev --shard I --nshards N --out DIR
    uv run python -m src.eval.rank_ideas fit --dir DIR --exp X-rank-ideas

collect: search() of the product config without a ranker (as src/match/ranker_train.py) over the R-198 training
window (as_of 2025-04-01, lots until 2025-07-01, n=3000) or the dev cases (as_of 2025-07-01); stores the ranker
features (ranker.NAMES) and the candidate INNs. fit: adds the new columns (one duckdb pass over participants,
strictly before as_of — the query's own lot is never in it), trains LightGBM (ranker_train.fit_lgbm) on the base
columns and on base + each group, reranks the dev candidates offline and compares by paired bootstrap.

New groups (idea of brawler2011/rlthack candidates.py, our own code):
  cust  — cust_grp_wins (log1p wins at THIS customer in lots of the query's OKPD2 groups, level 4),
          cust_grp_days (log1p days from the last such win to as_of), cust_wins (log1p wins at the customer, any lot)
  cont  — log_cont_bids (log1p ЭМ offers in lots with >= 2 offers), cont_share (of all ЭМ offers),
          cont_win_rate (wins / offers in such lots)
  smp   — smp_lot_x_share: the lot is «only for SMP» (notices.is_smp by lot_id) x the supplier's share of SMP wins
The product never changes: no config, no model file, ranker.NAMES is patched only inside this process.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import date
from pathlib import Path

import numpy as np

from src.eval.split import load_cases
from src.match import ranker
from src.match.ranker_train import fit_lgbm, training_cases
from src.match.search import load_config, search
from src.match.store import processed_dir

ROOT = Path(__file__).resolve().parents[2]
TRAIN_AS_OF, TRAIN_UNTIL, N_TRAIN = date(2025, 4, 1), date(2025, 7, 1), 3000
GROUPS = {"cust": ["cust_grp_wins", "cust_grp_days", "cust_wins"],
          "cont": ["log_cont_bids", "cont_share", "cont_win_rate"],
          "smp": ["smp_lot_x_share"]}
NEW = [n for g in GROUPS.values() for n in g]
BOOT, SEED = 1000, 577


def _cases(part: str) -> list:
    if part == "dev":
        return load_cases("dev")
    return training_cases(processed_dir(), TRAIN_AS_OF, TRAIN_UNTIL, N_TRAIN)


def collect(part: str, shard: int, nshards: int, out: Path) -> None:
    cfg = load_config()
    cfg.pop("ranker", None)
    cases = _cases(part)
    xs, qs, inns = [], [], []
    t0 = time.monotonic()
    for n, i in enumerate(range(shard, len(cases), nshards), start=1):
        got: list = []
        search(cases[i].query, k=10**6, config=cfg, features_out=got)
        for inn, row in got:
            xs.append(row)
            qs.append(i)
            inns.append(inn)
        if n % 50 == 0:
            print(f"{part} shard {shard}: {n} q, {time.monotonic() - t0:.0f} s", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out / f"{part}-{shard}.npz", x=np.asarray(xs, dtype=float), q=np.asarray(qs, dtype=np.int32),
                        inn=np.asarray(inns, dtype="U12"))
    done = len(range(shard, len(cases), nshards))
    print(f"{part} shard {shard}: done {done} q in {time.monotonic() - t0:.0f} s", flush=True)


def _load(d: Path, part: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    parts = [np.load(f) for f in sorted(d.glob(f"{part}-*.npz"))]
    x = np.concatenate([p["x"] for p in parts])
    q = np.concatenate([p["q"] for p in parts])
    inn = np.concatenate([p["inn"] for p in parts])
    order = np.argsort(q, kind="stable")  # rows of one query contiguous, search order kept
    return x[order], q[order], inn[order]


def new_features(cases: list, q: np.ndarray, inn: np.ndarray) -> np.ndarray:
    """(rows, len(NEW)) for (case q, supplier inn); history strictly before the case's as_of."""
    import duckdb
    import pyarrow as pa

    pdir = processed_dir()
    pairs = pa.table({"r": np.arange(len(q)), "inn": inn.tolist(), "qi": q,
                      "cust": [cases[i].query.customer_inn for i in q],
                      "as_of": [cases[i].query.as_of for i in q],
                      "lot_id": [int(cases[i].query.query_id.removeprefix("lot-")) for i in q]})
    gp = [(i, g) for i, c0 in enumerate(cases) for g in sorted({x[:5] for x in c0.query.okpd2 if len(x) >= 5})]
    groups = pa.table({"qi": [i for i, _ in gp], "grp": [g for _, g in gp]})
    con = duckdb.connect()
    con.register("pairs", pairs)
    con.register("qgroups", groups)
    part, notices, items = (f"'{(pdir / n).as_posix()}'" for n in
                            ("participants.parquet", "notices.parquet", "items.parquet"))
    df = con.sql(f"""
        WITH p AS (SELECT * FROM {part} WHERE inn_valid),
        lot_n AS (SELECT lot_id, count(*) AS n FROM p GROUP BY lot_id),
        lot_grp AS (SELECT DISTINCT lot_id, okpd2_l4 AS grp FROM {items} WHERE okpd2_l4 IS NOT NULL),
        cw AS (  -- wins at the customer, any lot
            SELECT r.r, count(*) AS wins FROM pairs r JOIN p ON p.supplier_inn = r.inn
              AND p.customer_inn = r.cust AND p.is_winner AND p.publish_date < r.as_of GROUP BY r.r),
        cg AS (  -- wins at the customer in lots of the query's groups
            SELECT r.r, count(DISTINCT p.lot_id) AS wins, max(p.publish_date) AS last, any_value(r.as_of) AS as_of
            FROM pairs r JOIN p ON p.supplier_inn = r.inn AND p.customer_inn = r.cust AND p.is_winner
              AND p.publish_date < r.as_of
            JOIN lot_grp g ON g.lot_id = p.lot_id JOIN qgroups qg ON qg.qi = r.qi AND qg.grp = g.grp GROUP BY r.r),
        ua AS (SELECT DISTINCT inn, as_of FROM pairs),
        ct AS (
            SELECT ua.inn, ua.as_of,
                   count(*) FILTER (WHERE p.channel = 'ЭМ') AS em,
                   count(*) FILTER (WHERE p.channel = 'ЭМ' AND ln.n >= 2) AS cb,
                   count(*) FILTER (WHERE p.channel = 'ЭМ' AND ln.n >= 2 AND p.is_winner) AS cwins,
                   count(*) FILTER (WHERE p.is_winner) AS wins,
                   count(*) FILTER (WHERE p.is_winner AND n.is_smp) AS smp_wins
            FROM ua JOIN p ON p.supplier_inn = ua.inn AND p.publish_date < ua.as_of
            JOIN lot_n ln ON ln.lot_id = p.lot_id JOIN {notices} n ON n.lot_id = p.lot_id GROUP BY ALL),
        lot_smp AS (SELECT lot_id, is_smp FROM {notices} WHERE lot_id IN (SELECT DISTINCT lot_id FROM pairs))
        SELECT r.r, r.cust IS NULL AS no_cust, coalesce(cg.wins, 0) AS cg_wins, cg.last, cg.as_of AS cg_as_of,
               coalesce(cw.wins, 0) AS c_wins, coalesce(ct.em, 0) AS em, coalesce(ct.cb, 0) AS cb,
               coalesce(ct.cwins, 0) AS cwins, coalesce(ct.wins, 0) AS wins, coalesce(ct.smp_wins, 0) AS smp_wins,
               coalesce(ls.is_smp, false) AS lot_smp
        FROM pairs r LEFT JOIN cg USING (r) LEFT JOIN cw USING (r)
        LEFT JOIN ct ON ct.inn = r.inn AND ct.as_of = r.as_of LEFT JOIN lot_smp ls ON ls.lot_id = r.lot_id
        ORDER BY r.r""")
    names = df.columns
    nan = math.nan
    out = np.full((len(q), len(NEW)), nan)
    for j, tup in enumerate(df.fetchall()):
        row = argparse.Namespace(**dict(zip(names, tup, strict=True)))
        if not row.no_cust:
            out[j, 0] = math.log1p(row.cg_wins)
            out[j, 1] = math.log1p((row.cg_as_of - row.last).days) if row.cg_wins else nan
            out[j, 2] = math.log1p(row.c_wins)
        out[j, 3] = math.log1p(row.cb)
        out[j, 4] = row.cb / row.em if row.em else nan
        out[j, 5] = row.cwins / row.cb if row.cb else nan
        out[j, 6] = (row.smp_wins / row.wins if row.wins else nan) if row.lot_smp else 0.0
    return out


def _metrics(rank_inns: list[list[str]], cases: list) -> dict[str, np.ndarray]:
    w10, w1, r10, has_w = [], [], [], []
    for ranked, c in zip(rank_inns, cases, strict=True):
        winners = {i for i, g in c.grades.items() if g >= 2}
        rel = set(c.relevant_inns)
        has_w.append(bool(winners))
        w10.append(float(bool(set(ranked[:10]) & winners)))
        w1.append(float(bool(set(ranked[:1]) & winners)))
        r10.append(len(set(ranked[:10]) & rel) / len(rel) if rel else 0.0)
    m = np.asarray(has_w, dtype=bool)
    return {"winner@10": np.asarray(w10)[m], "winner@1": np.asarray(w1)[m], "recall@10": np.asarray(r10)}


def _rank(raw: np.ndarray, score: np.ndarray, q: np.ndarray, inn: np.ndarray, n_cases: int) -> list[list[str]]:
    out: list[list[str]] = [[] for _ in range(n_cases)]
    starts = np.flatnonzero(np.r_[True, q[1:] != q[:-1]])
    for s, e in zip(starts, np.r_[starts[1:], len(q)], strict=True):
        idx = sorted(range(s, e), key=lambda i: (-raw[i], -score[i], inn[i]))[:20]
        out[int(q[s])] = [str(inn[i]) for i in idx]
    return out


def _boot(a: np.ndarray, b: np.ndarray) -> list[float]:
    rng = np.random.default_rng(SEED)
    d = b - a
    s = rng.integers(0, len(d), size=(BOOT, len(d)))
    m = d[s].mean(axis=1)
    return [round(float(d.mean()), 4), round(float(np.percentile(m, 2.5)), 4), round(float(np.percentile(m, 97.5)), 4)]


def fit(d: Path, exp: str) -> dict:
    t0 = time.monotonic()
    train_cases, dev_cases = _cases("train"), _cases("dev")
    xt, qt, it = _load(d, "train")
    xd, qd, idv = _load(d, "dev")
    cache = d / "new.npz"
    if cache.exists():
        z = np.load(cache)
        nt, nd = z["nt"], z["nd"]
    else:
        nt, nd = new_features(train_cases, qt, it), new_features(dev_cases, qd, idv)
        np.savez_compressed(cache, nt=nt, nd=nd)
    print(f"new features {time.monotonic() - t0:.0f} s", flush=True)
    yt = np.asarray([train_cases[i].grades.get(str(s), 0) for i, s in zip(qt, it, strict=True)], dtype=np.int8)
    assert not {c.query.query_id for c in train_cases} & {c.query.query_id for c in dev_cases}, "dev lot in training"
    assert all(c.query.as_of == TRAIN_AS_OF for c in train_cases)
    base = list(ranker.NAMES)
    w = load_config()["weights"]
    score = (w["product_fit"] * xd[:, base.index("fit")] + w["experience"] * xd[:, base.index("exp")]
             + w["activity"] * xd[:, base.index("act")] + w["geography"] * xd[:, base.index("geo")])
    full_t, full_d = np.hstack([xt, nt]), np.hstack([xd, nd])
    names_all = base + NEW
    variants = {"base": base, **{f"+{g}": base + cols for g, cols in GROUPS.items()}, "+all": names_all}
    res: dict = {"exp": exp, "n_train_cases": len(train_cases), "train_rows": len(yt),
                 "train_positives": int((yt > 0).sum()), "n_dev_cases": len(dev_cases), "dev_rows": len(qd),
                 "groups": GROUPS, "nan_share_dev": {n: round(float(np.isnan(nd[:, j]).mean()), 3)
                                                     for j, n in enumerate(NEW)}, "variants": {}}
    per: dict[str, dict[str, np.ndarray]] = {}
    # control: the shipped R-198 model on the same dev features (its offline rerank vs make eval's 0.597)
    sp = {"kind": "lgbm", "model": "models/ranker/R-198-gbm.txt"}
    m198 = ranker.load(sp)
    per["R-198-shipped"] = _metrics(_rank(m198.raw(xd), score, qd, idv, len(dev_cases)), dev_cases)
    for name, cols in variants.items():
        idx = [names_all.index(c) for c in cols]
        ranker.NAMES = cols  # fit_lgbm names the columns by ranker.NAMES; process-local
        booster, _ = fit_lgbm(full_t[:, idx], yt, qt)
        ranker.NAMES = base
        raw = np.asarray(booster.predict(full_d[:, idx], raw_score=True, num_threads=4), dtype=float)
        per[name] = _metrics(_rank(raw, score, qd, idv, len(dev_cases)), dev_cases)
        gain = booster.feature_importance("gain")
        res["variants"][name] = {"trees": booster.best_iteration,
                                 "gain_new": {c: round(float(g / gain.sum()), 4) for c, g in zip(cols, gain,
                                                                                                strict=True)
                                              if c in NEW}}
        print(name, {k: round(float(v.mean()), 4) for k, v in per[name].items()}, flush=True)
    for name, m in per.items():
        entry = res["variants"].setdefault(name, {})
        entry["metrics"] = {k: round(float(v.mean()), 4) for k, v in m.items()}
        if name != "base":
            entry["delta_vs_base"] = {k: _boot(per["base"][k], v) for k, v in m.items()}
    # training-noise check: the same variants with other seeds (validation split and LightGBM bagging change)
    seeds: dict[str, dict] = {}
    for sd in (1, 2, 3):
        got = {}
        for name in ("base", "+cust", "+smp", "+all"):
            cols = variants[name]
            idx = [names_all.index(c) for c in cols]
            ranker.NAMES = cols
            booster, _ = fit_lgbm(full_t[:, idx], yt, qt, seed=sd)
            ranker.NAMES = base
            raw = np.asarray(booster.predict(full_d[:, idx], raw_score=True, num_threads=4), dtype=float)
            got[name] = _metrics(_rank(raw, score, qd, idv, len(dev_cases)), dev_cases)
        seeds[str(sd)] = {name: {"metrics": {k: round(float(v.mean()), 4) for k, v in m.items()},
                                 **({"delta_vs_base": {k: _boot(got["base"][k], v) for k, v in m.items()}}
                                    if name != "base" else {})} for name, m in got.items()}
        print("seed", sd, {n: v["metrics"]["winner@10"] for n, v in seeds[str(sd)].items()}, flush=True)
    res["seed_check"] = seeds
    res["n_winner_cases"] = int(len(per["base"]["winner@10"]))
    res["bootstrap"] = {"n": BOOT, "seed": SEED, "ci": "percentile 95%, paired over dev queries"}
    res["elapsed_s"] = round(time.monotonic() - t0, 1)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["collect", "fit"])
    ap.add_argument("--part", choices=["train", "dev"])
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--out", "--dir", dest="dir", type=Path, required=True)
    ap.add_argument("--exp", default="X-rank-ideas")
    args = ap.parse_args(argv)
    if args.mode == "collect":
        collect(args.part, args.shard, args.nshards, args.dir)
        return 0
    res = fit(args.dir, args.exp)
    path = ROOT / "eval" / "results" / f"{args.exp}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
