"""Training of the learned ranker (#198) without a time leak. Owner: MATCH.

    DATA_PROCESSED_DIR=<ingest with suppliers_2025-04-01.parquet> \\
    uv run python -m src.match.ranker_train [--n 3000] [--as-of 2025-04-01] [--until 2025-07-01]

`make` / `src.ingest.build` prepare supplier aggregates only for the cutoff; those of --as-of come from
`uv run python -m src.ingest.build --as-of 2025-04-01 --no-cutoff --out-dir <tmp>` — copy its
suppliers_2025-04-01.parquet next to the others (the manifest stays, so index keys do not change).

Queries: lots of [as_of, until) — before the dev window (cutoff 2025-07-01, configs/split.yaml) — turned
into cases by the logic of src/eval/build_cases.py (same exclusions, relevant INNs and grades), a stratified
sample of n with a fixed seed. Features: search() of the product configuration with `as_of` = the start of
the window — only history strictly before it (the BM25 index and the supplier aggregates of that date).
Labels: grade of the case (ЭМ: offer 1, winner 2; АИС ГЗ: winner 2), else 0.

Writes models/ranker/R-198-lr.json (logistic regression on standardized features, label > 0) and
models/ranker/R-198-gbm.txt (LightGBM LambdaMART, labels 0/1/2); features are cached in --cache (npz),
`--from-cache` retrains without search(). 15% of the training queries (by seeded hash) are the early-stopping
validation of LightGBM — dev is never read here.

`--experiment E6-cust-ranker` uses the same candidate/feature configuration as eval. Cache metadata rejects
a different configuration, feature schema, sample or window. `--lgbm-only` avoids the extra logreg fit;
`--shuffle-labels` and `--drop-features NAME ...` train negative controls on the unchanged feature cache.
`--model-prefix E6-cust` names separate models and a `*-gbm.meta.json` training/feature-gain sidecar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import numpy as np

from src.eval.build_cases import SEED, _h, exclusion, read_lots, stratified_sample, to_case
from src.eval.split import split_config
from src.match import ranker
from src.match import search as search_mod
from src.match.search import load_config, search
from src.match.store import processed_dir

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "models" / "ranker"
L2 = 1.0  # logreg: L2 penalty on the standardized weights (sum of the loss over rows / n)
VALID_SHARE = 0.15
CACHE_VERSION = 1


def validate_window(as_of: date, until: date, n: int) -> None:
    """Reject an invalid training window before any case or label is read."""
    cutoff = date.fromisoformat(str(split_config()["cutoff"]))
    if not as_of < until <= cutoff:
        raise ValueError(f"обучение требует as_of < until <= dev cutoff ({cutoff}), получено [{as_of}, {until})")
    if n < 1:
        raise ValueError("--n должно быть > 0")


def training_cases(processed: Path, as_of: date, until: date, n: int, seed: int = SEED) -> list:
    validate_window(as_of, until, n)
    lots = [lot for lot in read_lots(processed, as_of, until) if exclusion(lot) is None]
    chosen = stratified_sample([(f"{lot.channel}|{lot.main_class or '-'}", lot.lot_id) for lot in lots], n, seed)
    return [to_case(lot, as_of) for lot in lots if lot.lot_id in chosen]


def collect(cases: list, cfg: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(X, y, query index, channel ЭМ flag) over all candidates of every case."""
    xs, ys, qs = [], [], []
    n_rows = 0
    t0 = time.monotonic()
    strict, search_mod.STRICT_RETRIEVER = search_mod.STRICT_RETRIEVER, True
    try:
        for i, case in enumerate(cases):
            got: list = []
            search(case.query, k=10**6, config=cfg, features_out=got)
            if got:
                # Keep numerical blocks, not hundreds of thousands of Python lists/floats in RAM.
                xs.append(np.asarray([row for _, row in got], dtype=float))
                ys.append(np.asarray([case.grades.get(inn, 0) for inn, _ in got], dtype=np.int8))
                qs.append(np.full(len(got), i, dtype=np.int32))
                n_rows += len(got)
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(cases)} запросов, {n_rows} строк, {time.monotonic() - t0:.0f} с", flush=True)
    finally:
        search_mod.STRICT_RETRIEVER = strict
    return (np.concatenate(xs) if xs else np.empty((0, len(ranker.NAMES))),
            np.concatenate(ys) if ys else np.empty(0, dtype=np.int8),
            np.concatenate(qs) if qs else np.empty(0, dtype=np.int32),
            np.asarray([c.query.channel == "ЭМ" for c in cases]))


def cache_metadata(cfg: dict, as_of: date, until: date, n: int, seed: int) -> dict:
    """The candidate pool, features and temporal sample that produced a feature cache."""
    from src.match.text import tokenizer_name, tokenizer_version

    encoded = json.dumps(cfg, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {"version": CACHE_VERSION, "features": ranker.NAMES, "config": cfg,
            "config_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "as_of": as_of.isoformat(), "until": until.isoformat(), "requested_n": n, "seed": seed,
            "tokenizer": tokenizer_name(), "tokenizer_version": tokenizer_version()}


def input_fingerprint(processed: Path, as_of: date) -> dict:
    """Invalidate stale feature caches after rebuilding the data or changing their extraction code."""
    paths = {"manifest": processed / "manifest.json",
             "suppliers": processed / f"suppliers_{as_of.isoformat()}.parquet"}
    paths.update({name: processed / f"{name}.parquet" for name in ("notices", "items", "participants")})
    paths.update({f"src/{name}.py": ROOT / "src" / f"{name}.py" for name in (
        "match/ranker_train", "match/ranker", "match/search", "match/candidates", "match/store",
        "match/index", "match/text", "match/signals", "match/profiles", "match/fallback", "eval/build_cases",
        "ingest/load",
    )})
    out = {}
    for name, path in paths.items():
        if path.exists():
            with path.open("rb") as source:
                out[name] = hashlib.file_digest(source, "sha256").hexdigest()
        else:
            out[name] = None
    return out


@contextmanager
def configured_tokenizer(cfg: dict) -> Iterator[None]:
    """Search reads the tokenizer from the environment; preserve an explicit benchmark override."""
    previous = os.environ.get("BM25_TOKENIZER")
    if not previous and cfg.get("tokenizer"):
        os.environ["BM25_TOKENIZER"] = str(cfg["tokenizer"])
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("BM25_TOKENIZER", None)
        else:
            os.environ["BM25_TOKENIZER"] = previous


def read_cache(path: Path, expected: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    with np.load(path, allow_pickle=False) as data:
        if "metadata" not in data:
            raise ValueError(f"{path}: кеш без provenance; пересоберите без --from-cache")
        saved = json.loads(str(data["metadata"].item()))
        changed = [key for key, value in expected.items() if saved.get(key) != value]
        if changed:
            raise ValueError(f"{path}: кеш другой конфигурации/выборки ({', '.join(changed)}); пересоберите")
        x, y, q = data["x"], data["y"], data["q"]
        n_cases = int(data["n_cases"])
    if (x.ndim != 2 or x.shape != (len(y), len(ranker.NAMES)) or y.shape != q.shape
            or not 0 < n_cases <= expected["requested_n"] or np.any(q < 0) or np.any(q >= n_cases)
            or np.any(np.diff(q) < 0) or not np.isin(y, [0, 1, 2]).all()):
        raise ValueError(f"{path}: некорректные размеры, группы или метки кеша")
    return x, y, q, n_cases


def shuffled_labels(y: np.ndarray, q: np.ndarray, seed: int) -> np.ndarray:
    """Negative control: shuffle within each query, preserving its grades and candidate pool."""
    out = y.copy()
    rng = np.random.default_rng(seed)
    for start, stop in zip(np.r_[0, np.flatnonzero(np.diff(q)) + 1],
                           np.r_[np.flatnonzero(np.diff(q)) + 1, len(q)], strict=True):
        out[start:stop] = rng.permutation(y[start:stop])
    return out


def ablated_features(x: np.ndarray, names: list[str]) -> np.ndarray:
    """Constant columns cannot enter a tree split; the deployed model then ignores their real values."""
    unknown = set(names) - set(ranker.NAMES)
    if unknown:
        raise ValueError(f"неизвестные признаки --drop-features: {', '.join(sorted(unknown))}")
    if not names:
        return x
    out = x.copy()
    out[:, [ranker.NAMES.index(name) for name in names]] = 0.0
    return out


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
    if not valid_q or len(valid_q) == len(queries):
        raise ValueError("мало запросов для независимых train/validation групп; увеличьте --n")
    is_valid = np.isin(q, list(valid_q))

    def dataset(mask: np.ndarray, ref=None):
        _, sizes = np.unique(q[mask], return_counts=True)  # q is sorted: rows of a query are contiguous
        return lgb.Dataset(x[mask], label=y[mask], group=sizes, feature_name=ranker.NAMES, reference=ref,
                           free_raw_data=True)

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
    ap.add_argument("--experiment", help="use exactly this configs/experiments.yaml candidate/feature configuration")
    ap.add_argument("--model-prefix", default="R-198", help="output <prefix>-gbm.txt and <prefix>-lr.json")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--lgbm-only", action="store_true", help="skip the logistic regression fit")
    ap.add_argument("--shuffle-labels", action="store_true", help="negative control: permute grades within queries")
    ap.add_argument("--drop-features", nargs="+", default=[], help="ablation: constant columns, e.g. bm25 cust_lots")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    validate_window(args.as_of, args.until, args.n)
    if not args.model_prefix or Path(args.model_prefix).name != args.model_prefix:
        raise ValueError("--model-prefix должен быть непустым именем, без пути")
    unknown = set(args.drop_features) - set(ranker.NAMES)
    if unknown:
        raise ValueError(f"неизвестные признаки --drop-features: {', '.join(sorted(unknown))}")
    if args.experiment:
        from src.eval.run import load_method_config

        cfg = load_method_config(args.experiment, config_of=args.experiment)
        if cfg.get("method", "search") != "search" or "baseline" in cfg:
            raise ValueError("--experiment должен задавать конфигурацию search(), не baseline")
    else:
        cfg = load_config()
    cfg.pop("ranker", None)  # features come from the product scoring (v2), never from a ranker
    with configured_tokenizer(cfg):
        return _train(args, cfg)


def _train(args: argparse.Namespace, cfg: dict) -> int:
    provenance = cache_metadata(cfg, args.as_of, args.until, args.n, args.seed)
    provenance["inputs_sha256"] = input_fingerprint(processed_dir(), args.as_of)
    t0 = time.monotonic()
    if args.from_cache:
        x, y, q, n_cases = read_cache(args.cache, provenance)
    else:
        processed = processed_dir()
        print(f"данные: {processed} (DATA_PROCESSED_DIR), INDEX_DIR={os.environ.get('INDEX_DIR', 'indexes')}")
        cases = training_cases(processed, args.as_of, args.until, args.n, args.seed)
        if any(c.query.as_of != args.as_of for c in cases):
            raise AssertionError("as_of признаков не равен началу окна обучения")
        print(f"обучающих запросов: {len(cases)} из [{args.as_of}, {args.until}), as_of признаков = {args.as_of}")
        x, y, q, _ = collect(cases, cfg)
        n_cases = len(cases)
        if not len(y):
            raise ValueError("обучающая выборка не содержит кандидатов")
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(args.cache, x=x, y=y, q=q, n_cases=n_cases,
                            metadata=json.dumps(provenance, sort_keys=True, ensure_ascii=False))
    if not len(y):
        raise ValueError("обучающая выборка не содержит кандидатов")
    t_feat = time.monotonic() - t0
    x = ablated_features(x, args.drop_features)
    if args.shuffle_labels:
        y = shuffled_labels(y, q, args.seed)
    hit = len(np.unique(q[y > 0]))
    print(f"строк {len(y)}, положительных {int((y > 0).sum())}, запросов с участником среди кандидатов "
          f"{hit}/{n_cases} ({hit / max(n_cases, 1):.1%}); признаки {t_feat:.0f} с")
    args.out.mkdir(parents=True, exist_ok=True)
    meta = {**provenance, "experiment": args.experiment, "n_cases": n_cases, "rows": len(y),
            "positives": int((y > 0).sum()), "queries_with_positive": hit, "feature_s": round(t_feat, 1),
            "shuffle_labels": args.shuffle_labels, "drop_features": sorted(set(args.drop_features))}
    if not args.lgbm_only:
        t1 = time.monotonic()
        lr = fit_logreg(x, y)
        lr["meta"] = {**meta, "train_s": round(time.monotonic() - t1, 1), "l2": L2}
        (args.out / f"{args.model_prefix}-lr.json").write_text(
            json.dumps(lr, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"logreg: {lr['meta']['train_s']} с, сходимость {lr['converged']}")
        for name, w in sorted(zip(ranker.NAMES, lr["coef"], strict=True), key=lambda t: -abs(t[1]))[:12]:
            print(f"  {name:<20} {w:+.3f}")
        if math.isnan(float(np.sum(lr["coef"]))):
            print("logreg: NaN в весах", file=sys.stderr)
            return 1
    t2 = time.monotonic()
    booster, n_valid = fit_lgbm(x, y, q, args.seed)
    splits = booster.feature_importance("split")
    if any(splits[ranker.NAMES.index(name)] for name in args.drop_features):
        raise AssertionError("модель использовала исключённый признак — небезопасно для inference")
    model_path = args.out / f"{args.model_prefix}-gbm.txt"
    model_text = booster.model_to_string()
    model_path.write_text(model_text, encoding="utf-8")  # C API: no non-ASCII paths
    gain = booster.feature_importance("gain")
    train_s = round(time.monotonic() - t2, 1)
    model_meta = {**meta, "train_s": train_s, "validation_queries": n_valid,
                  "best_iteration": booster.best_iteration,
                  "validation_ndcg@10": booster.best_score['valid_0']['ndcg@10'],
                  "feature_importance_gain": dict(zip(ranker.NAMES, gain.tolist(), strict=True)),
                  "model_sha256": hashlib.sha256(model_text.encode()).hexdigest()}
    model_path.with_suffix(".meta.json").write_text(
        json.dumps(model_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"lgbm: {train_s:.1f} с, деревьев {booster.best_iteration}, валидация {n_valid} запросов, "
          f"ndcg@10 {booster.best_score['valid_0']['ndcg@10']:.4f}")
    for name, g in sorted(zip(ranker.NAMES, gain, strict=True), key=lambda t: -t[1])[:12]:
        print(f"  {name:<20} {g / max(gain.sum(), 1e-12):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
