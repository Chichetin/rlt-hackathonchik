"""Training of the learned ranker (#198) without a time leak. Owner: MATCH.

    DATA_PROCESSED_DIR=<ingest with suppliers_2025-04-01.parquet> \\
    uv run python -m src.match.ranker_train [--n 3000] [--as-of 2025-04-01] [--until 2025-07-01]

Queries: lots of [as_of, until) — before the dev window (cutoff 2025-07-01, configs/split.yaml) — turned
into cases by the logic of src/eval/build_cases.py (same exclusions, relevant INNs and grades), a stratified
sample of n with a fixed seed. Features: search() of the product configuration with `as_of` = the start of
the window — only history strictly before it (the BM25 index and the supplier aggregates of that date).
Labels: grade of the case (ЭМ: offer 1, winner 2; АИС ГЗ: winner 2), else 0.

Writes models/ranker/R-198-lr.json (logistic regression on standardized features, label > 0) and
models/ranker/R-198-gbm.txt (LightGBM LambdaMART, labels 0/1/2); features are cached in --cache (npz),
`--from-cache` retrains without search(). 15% of the training queries (by seeded hash) are the early-stopping
validation of LightGBM — dev is never read here.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

from src.eval.build_cases import SEED, _h, exclusion, read_lots, stratified_sample, to_case
from src.match import ranker
from src.match.search import load_config, search
from src.match.store import processed_dir

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "models" / "ranker"
L2 = 1.0  # logreg: L2 penalty on the standardized weights (sum of the loss over rows / n)
VALID_SHARE = 0.15


def training_cases(processed: Path, as_of: date, until: date, n: int, seed: int = SEED) -> list:
    lots = [lot for lot in read_lots(processed, as_of) if lot.date < until and exclusion(lot) is None]
    chosen = stratified_sample([(f"{lot.channel}|{lot.main_class or '-'}", lot.lot_id) for lot in lots], n, seed)
    return [to_case(lot, as_of) for lot in lots if lot.lot_id in chosen]


def collect(cases: list, cfg: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(X, y, query index, channel ЭМ flag) over all candidates of every case."""
    xs, ys, qs = [], [], []
    t0 = time.monotonic()
    for i, case in enumerate(cases):
        got: list = []
        search(case.query, k=10**6, config=cfg, features_out=got)
        for inn, row in got:
            xs.append(row)
            ys.append(case.grades.get(inn, 0))
            qs.append(i)
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(cases)} запросов, {len(xs)} строк, {time.monotonic() - t0:.0f} с", flush=True)
    return (np.asarray(xs, dtype=float), np.asarray(ys, dtype=np.int8), np.asarray(qs, dtype=np.int32),
            np.asarray([c.query.channel == "ЭМ" for c in cases]))


def fit_logreg(x: np.ndarray, y: np.ndarray) -> dict:
    """L2 logistic regression by L-BFGS on standardized features; NaN -> the training median."""
    from scipy.optimize import minimize

    fill = np.nanmedian(x, axis=0)
    fill = np.where(np.isnan(fill), 0.0, fill)
    xf = np.where(np.isnan(x), fill, x)
    mean, std = xf.mean(axis=0), xf.std(axis=0)
    std = np.where(std > 1e-12, std, 1.0)
    z = (xf - mean) / std
    t = (y > 0).astype(float)
    n = len(t)

    def loss(w: np.ndarray) -> tuple[float, np.ndarray]:
        s = z @ w[:-1] + w[-1]
        p = 1.0 / (1.0 + np.exp(-s))
        val = np.sum(np.logaddexp(0.0, s) - t * s) / n + 0.5 * L2 / n * np.sum(w[:-1] ** 2)
        g = p - t
        grad = np.append(z.T @ g / n + L2 / n * w[:-1], g.mean())
        return val, grad

    res = minimize(loss, np.zeros(z.shape[1] + 1), jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return {"features": ranker.NAMES, "fill": fill.tolist(), "mean": mean.tolist(), "std": std.tolist(),
            "coef": res.x[:-1].tolist(), "intercept": float(res.x[-1]), "converged": bool(res.success)}


def fit_lgbm(x: np.ndarray, y: np.ndarray, q: np.ndarray, seed: int = SEED):
    import lightgbm as lgb

    queries = np.unique(q)
    valid_q = {int(i) for i in queries if int(_h(seed, int(i)), 16) % 1000 < VALID_SHARE * 1000}
    is_valid = np.isin(q, list(valid_q))

    def dataset(mask: np.ndarray, ref=None):
        _, sizes = np.unique(q[mask], return_counts=True)  # q is sorted: rows of a query are contiguous
        return lgb.Dataset(x[mask], label=y[mask], group=sizes, feature_name=ranker.NAMES, reference=ref,
                           free_raw_data=False)

    train = dataset(~is_valid)
    valid = dataset(is_valid, train)
    params = {"objective": "lambdarank", "metric": "ndcg", "eval_at": [10], "learning_rate": 0.05,
              "num_leaves": 31, "min_data_in_leaf": 50, "feature_fraction": 0.8, "bagging_fraction": 0.8,
              "bagging_freq": 1, "lambdarank_truncation_level": 20, "seed": seed, "deterministic": True,
              "num_threads": 4, "verbose": -1}
    booster = lgb.train(params, train, num_boost_round=1000, valid_sets=[valid],
                        callbacks=[lgb.early_stopping(50, verbose=False)])
    return booster, len(valid_q)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--as-of", type=date.fromisoformat, default=date(2025, 4, 1))
    ap.add_argument("--until", type=date.fromisoformat, default=date(2025, 7, 1))
    ap.add_argument("--cache", type=Path, default=ROOT / "data" / "ranker" / "train-2025-04-01.npz")
    ap.add_argument("--from-cache", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config()
    cfg.pop("ranker", None)  # features come from the product scoring (v2), never from a ranker
    t0 = time.monotonic()
    if args.from_cache:
        d = np.load(args.cache)
        x, y, q = d["x"], d["y"], d["q"]
        n_cases = int(d["n_cases"])
    else:
        processed = processed_dir()
        print(f"данные: {processed} (DATA_PROCESSED_DIR), INDEX_DIR={os.environ.get('INDEX_DIR', 'indexes')}")
        cases = training_cases(processed, args.as_of, args.until, args.n)
        if any(c.query.as_of != args.as_of for c in cases):
            raise AssertionError("as_of признаков не равен началу окна обучения")
        print(f"обучающих запросов: {len(cases)} из [{args.as_of}, {args.until}), as_of признаков = {args.as_of}")
        x, y, q, _ = collect(cases, cfg)
        n_cases = len(cases)
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(args.cache, x=x, y=y, q=q, n_cases=n_cases)
    t_feat = time.monotonic() - t0
    hit = len(np.unique(q[y > 0]))
    print(f"строк {len(y)}, положительных {int((y > 0).sum())}, запросов с участником среди кандидатов "
          f"{hit}/{n_cases} ({hit / max(n_cases, 1):.1%}); признаки {t_feat:.0f} с")
    args.out.mkdir(parents=True, exist_ok=True)
    meta = {"as_of": args.as_of.isoformat(), "until": args.until.isoformat(), "n_cases": n_cases,
            "rows": len(y), "positives": int((y > 0).sum()), "queries_with_positive": hit}
    t1 = time.monotonic()
    lr = fit_logreg(x, y)
    lr["meta"] = {**meta, "train_s": round(time.monotonic() - t1, 1), "l2": L2}
    (args.out / "R-198-lr.json").write_text(json.dumps(lr, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"logreg: {lr['meta']['train_s']} с, сходимость {lr['converged']}")
    for name, w in sorted(zip(ranker.NAMES, lr["coef"], strict=True), key=lambda t: -abs(t[1]))[:12]:
        print(f"  {name:<20} {w:+.3f}")
    t2 = time.monotonic()
    booster, n_valid = fit_lgbm(x, y, q)
    (args.out / "R-198-gbm.txt").write_text(booster.model_to_string(), encoding="utf-8")  # C API: no non-ASCII paths
    gain = booster.feature_importance("gain")
    print(f"lgbm: {time.monotonic() - t2:.1f} с, деревьев {booster.best_iteration}, валидация {n_valid} запросов, "
          f"ndcg@10 {booster.best_score['valid_0']['ndcg@10']:.4f}")
    for name, g in sorted(zip(ranker.NAMES, gain, strict=True), key=lambda t: -t[1])[:12]:
        print(f"  {name:<20} {g / max(gain.sum(), 1e-12):.3f}")
    if math.isnan(float(np.sum(lr["coef"]))):
        print("logreg: NaN в весах", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
