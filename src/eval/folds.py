"""Rolling-origin (walk-forward) eval: several time folds instead of one dev period (#79, research/EVAL.md).

    make eval-folds EXP=<id> [BASELINE=B-pop] [CONFIG_OF=<id>]
    uv run python -m src.eval.run --exp <id> --part folds [--baseline B-inc | --config-of H-15g]
    uv run python -m src.eval.folds compare <A> <B>          # paired delta A - B per fold and the mean

Fold k (configs/folds.yaml, `cutoffs`): features — only lots published strictly before cutoff_k; queries —
lots of [cutoff_k, cutoff_{k+1}). The window of the last fold ends at `holdout_from` (configs/split.yaml):
a cutoff in the holdout is refused, a fold never reads past it (D-010). Cases of a fold are the stratified
sample of the case builder (channel x OKPD2 class, seed — D-032), `as_of = cutoff_k`; the last fold is the
current dev with the same sample as eval/cases/dev.jsonl. Every fold is evaluated by `run.evaluate()`: the same
metrics, slices and bootstrap per fold; the aggregate is the mean over folds with a bootstrap that resamples
queries inside each fold. Supplier aggregates as of a fold's cutoff (suppliers_<cutoff>.parquet) are built
here from the ingest parquet, the same way `src.ingest.build` builds the one of the split cutoff.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import yaml

from src.eval import baselines, run
from src.eval import build_cases as bc
from src.eval.compare import paired
from src.eval.metrics import BOOTSTRAP_N, BOOTSTRAP_SEED, METRICS
from src.eval.split import ROOT, EvalCase, split_config
from src.ingest.build import build_suppliers
from src.match.index import pq_path
from src.match.store import ParquetStore, processed_dir

log = logging.getLogger(__name__)
FOLDS_CONFIG = ROOT / "configs" / "folds.yaml"
STAMP = "manifest_built_at"  # parquet key of a supplier file built here: which ingest it was built from
COMPARED = ("all", "channel=ЭМ", "channel=АИС ГЗ", "winner=new", "winner=known")


@dataclass(frozen=True)
class Fold:
    index: int  # 1-based
    cutoff: date  # features: lots published strictly before it
    until: date  # queries: lots published in [cutoff, until)


def load_folds_config(path: Path | None = None) -> dict:
    return yaml.safe_load((path or FOLDS_CONFIG).read_text(encoding="utf-8"))


def _day(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def make_folds(cfg: Mapping, split: Mapping | None = None) -> list[Fold]:
    """Folds of `cfg['cutoffs']`: each window ends where the next begins, the last one at `holdout_from`."""
    split = split or split_config()
    holdout_from = _day(split["holdout_from"])
    cutoffs = [_day(c) for c in cfg.get("cutoffs") or []]
    if not cutoffs or any(b <= a for a, b in zip(cutoffs, cutoffs[1:], strict=False)):
        raise ValueError(f"cutoffs: нужен непустой строго возрастающий список дат, получено {cfg.get('cutoffs')}")
    if cutoffs[-1] >= holdout_from:
        raise ValueError(f"cutoffs: {cutoffs[-1]} не раньше holdout_from {holdout_from} — fold в holdout, "
                         "он запечатан (D-010)")
    return [Fold(i, c, u) for i, (c, u) in enumerate(zip(cutoffs, [*cutoffs[1:], holdout_from], strict=True), 1)]


def check_fold_cases(fold: Fold, cases: Sequence[EvalCase], holdout_from: date) -> None:
    """The leak gate: window (which ends no later than the holdout) and features of every case of a fold.

    ValueError, never a silent filter: a case outside its fold is a bug of the case builder."""
    if fold.until > holdout_from:
        raise ValueError(f"fold {fold.index}: окно до {fold.until} заходит в holdout (с {holdout_from})")
    for c in cases:
        if not fold.cutoff <= c.date < fold.until:  # until <= holdout_from (above): a holdout case is outside
            raise ValueError(f"fold {fold.index}: кейс {c.query.query_id} от {c.date} вне окна "
                             f"[{fold.cutoff}, {fold.until}) — утечка")
        if c.query.as_of != fold.cutoff:
            raise ValueError(f"fold {fold.index}: кейс {c.query.query_id} с as_of={c.query.as_of}, нужен cutoff "
                             f"fold {fold.cutoff} — признаки только до него")


def fold_cases(lots: Sequence[bc.Lot], fold: Fold, n: int, seed: int) -> tuple[list[EvalCase], int]:
    """(stratified sample of the fold's cases, number of eligible lots): the rule of build_cases.build_all."""
    kept = [lot for lot in lots if fold.cutoff <= lot.date < fold.until and bc.exclusion(lot) is None]
    chosen = bc.stratified_sample([(f"{lot.channel}|{lot.main_class or '-'}", lot.lot_id) for lot in kept], n, seed)
    return [bc.to_case(lot, fold.cutoff) for lot in kept if lot.lot_id in chosen], len(kept)


def build_fold_cases(processed: Path, folds: Sequence[Fold], split: Mapping, n: int,
                     seed: int) -> dict[int, tuple[list[EvalCase], int]]:
    """{fold: (cases, eligible)}. Lots from `holdout_from` on are dropped right after reading: no fold sees them."""
    holdout_from = _day(split["holdout_from"])
    lots = [lot for lot in bc.read_lots(processed, folds[0].cutoff) if lot.date < holdout_from]
    return {f.index: fold_cases(lots, f, n, seed) for f in folds}


# --- features of a fold ----------------------------------------------------------------------------------

def ensure_suppliers(processed: Path, cutoff: date, force: bool = False) -> Path:
    """suppliers_<cutoff>.parquet next to the ingest parquet (search() reads it for as_of = cutoff).

    Built from the ingest tables with `src.ingest.build.build_suppliers` — lots strictly before cutoff. A file
    that carries no stamp (written by `src.ingest.build`) is kept; a file built here is rebuilt when the ingest
    it was built from is not the current one (manifest.built_at) or with `force`."""
    path = processed / f"suppliers_{cutoff.isoformat()}.parquet"
    built_at = str(ParquetStore(processed).manifest()["built_at"])
    if path.exists() and not force:
        stamp = dict(duckdb.execute(f"SELECT key::varchar, value::varchar FROM parquet_kv_metadata({pq_path(path)})")
                     .fetchall())
        if stamp.get(STAMP, built_at) == built_at:
            return path
        log.warning("%s собран из другой выгрузки (%s ≠ %s) — пересобираю", path.name, stamp[STAMP], built_at)
    started = time.monotonic()
    con = duckdb.connect()
    try:
        for table in ("notices", "participants", "items"):
            con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet({pq_path(processed / f'{table}.parquet')})")
        build_suppliers(con, cutoff, table="suppliers_fold")
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        con.execute(f"COPY suppliers_fold TO {pq_path(tmp)} (FORMAT parquet, KV_METADATA {{{STAMP}: '{built_at}'}})")
        os.replace(tmp, path)  # a symlink to someone else's file is replaced, never written through
    finally:
        con.close()
    log.info("%s: %.1f с", path.name, time.monotonic() - started)
    return path


def history_stats(processed: Path, cutoff: date) -> dict:
    """Lots with participants strictly before `cutoff` and the dates they span (how much history the fold has)."""
    lots, first, last = duckdb.execute(
        f"SELECT count(DISTINCT lot_id), min(publish_date), max(publish_date) "
        f"FROM read_parquet({pq_path(processed / 'participants.parquet')}) WHERE publish_date < $as_of",
        {"as_of": cutoff},
    ).fetchone() or (0, None, None)
    return {"lots": int(lots), "from": first, "to": last}


def cold_start_share(cases: Sequence[EvalCase], cutoff: date) -> float:
    """Share of cases whose relevant suppliers all have no participation before cutoff: recall cannot reach them."""
    known = baselines.history(cutoff).valid_inns
    return sum(1 for c in cases if not set(c.relevant_inns) & known) / len(cases) if cases else 0.0


# --- aggregation ----------------------------------------------------------------------------------------

def macro_bootstrap_ci(groups: Sequence[Sequence[float]], n: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED,
                       alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap of the mean of group means: queries are resampled inside their own fold."""
    rng = np.random.default_rng(seed)
    means = np.zeros(n)
    for g in groups:
        arr = np.asarray(g, dtype=float)
        if arr.size == 0:
            continue
        means += arr[rng.integers(0, arr.size, size=(n, arr.size))].mean(axis=1) if arr.size > 1 else arr[0]
    means /= max(sum(1 for g in groups if len(g)), 1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def summarize(rows_by_fold: Mapping[int, Sequence[dict]]) -> dict:
    """Mean over folds (each fold counts once, whatever its size) with a bootstrap interval and the spread."""
    metrics, spread = {}, {}
    for name in METRICS:
        groups = [[q[name] for q in rows] for _, rows in sorted(rows_by_fold.items())]
        per_fold = [sum(g) / len(g) if g else 0.0 for g in groups]
        lo, hi = macro_bootstrap_ci(groups)
        metrics[name] = {"value": sum(per_fold) / len(per_fold), "ci95": [lo, hi]}
        spread[name] = {"per_fold": per_fold, "min": min(per_fold), "max": max(per_fold),
                        "std": float(np.std(per_fold, ddof=1)) if len(per_fold) > 1 else 0.0}
    return {"metrics": metrics, "spread": spread}


# --- run ------------------------------------------------------------------------------------------------

def run_folds(cfg: dict, folds: Sequence[Fold], cases: Mapping[int, Sequence[EvalCase]], holdout_from: date,
              ranked_limit: int = 0, eligible: Mapping[int, int] | None = None) -> dict:
    """`run.evaluate()` on every fold with its own cutoff; a record body for `run.result_record`.

    Per-case top-k lists are not stored by default (`ranked_limit=0`): three folds of 2 000 cases stay ~1 MB."""
    processed = processed_dir()
    started = time.monotonic()
    blocks: list[dict] = []
    per_query: list[dict] = []
    rows_by_fold: dict[int, list[dict]] = {}
    for fold in folds:
        fold_set = list(cases[fold.index])
        if not fold_set:  # a mean over an empty fold would be a made-up zero
            raise ValueError(f"fold {fold.index}: в окне [{fold.cutoff}, {fold.until}) нет кейсов")
        check_fold_cases(fold, fold_set, holdout_from)
        if cfg.get("method", "search") == "search":
            ensure_suppliers(processed, fold.cutoff)
        log.warning("fold %d: cutoff %s, окно [%s, %s), %d кейсов", fold.index, fold.cutoff, fold.cutoff, fold.until,
                    len(fold_set))
        body = run.evaluate(fold_set, cfg, cutoff=fold.cutoff, ranked_limit=ranked_limit)
        hist = history_stats(processed, fold.cutoff)
        blocks.append({
            "fold": fold.index, "cutoff": fold.cutoff.isoformat(), "until": fold.until.isoformat(),
            "n_queries": body["n_queries"], "eligible": (eligible or {}).get(fold.index, len(fold_set)),
            "history": {"lots": hist["lots"], "from": hist["from"] and hist["from"].isoformat(),
                        "to": hist["to"] and hist["to"].isoformat()},
            "cold_start_share": round(cold_start_share(fold_set, fold.cutoff), 4),
            "elapsed_s": body["elapsed_s"], "metrics": body["metrics"], "slices": body["slices"],
        })
        rows = [{**q, "fold": fold.index} for q in body["per_query"]]
        rows_by_fold[fold.index] = rows
        per_query += rows
        baselines.clear_cache()  # the next fold has its own history; keep memory flat
    return {
        "protocol": {"cutoffs": [f.cutoff.isoformat() for f in folds],
                     "windows": [[f.cutoff.isoformat(), f.until.isoformat()] for f in folds],
                     "holdout_from": holdout_from.isoformat()},
        "n_queries": len(per_query), "elapsed_s": round(time.monotonic() - started, 1),
        **summarize(rows_by_fold), "folds": blocks, "per_query": per_query,
    }


def run_experiment(exp_id: str, cfg: dict) -> int:
    """`run.py --part folds`: build the cases of every fold, run, write eval/results/<id>.json."""
    split, fcfg = split_config(), load_folds_config()
    folds = make_folds(fcfg, split)
    if (os.environ.get("DATA_STORE") or "parquet").lower() != "parquet":
        log.warning("DATA_STORE=%s: fold-срезы поставщиков есть только в parquet — читаю parquet",
                    os.environ["DATA_STORE"])
    os.environ["DATA_STORE"] = "parquet"
    built = build_fold_cases(processed_dir(), folds, split, int(fcfg["sample"]), int(fcfg["seed"]))
    body = run_folds(cfg, folds, {k: v[0] for k, v in built.items()}, _day(split["holdout_from"]),
                     eligible={k: v[1] for k, v in built.items()})
    body["protocol"] |= {"sample": int(fcfg["sample"]), "seed": int(fcfg["seed"])}
    rec = run.result_record(exp_id, "folds", cfg, body)
    run.RESULTS.mkdir(parents=True, exist_ok=True)
    out = run.RESULTS / f"{exp_id}.json"
    out.write_text(run.dump_record(rec), encoding="utf-8")
    print_table(rec)
    print(f"-> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0


def print_table(rec: dict) -> None:
    print(f"EXP {rec['exp_id']} | part=folds | folds={len(rec['folds'])} | n={rec['n_queries']} | "
          f"commit={rec['commit'][:8]}{' (dirty)' if rec['dirty'] else ''}{' | STUB' if rec['stub'] else ''}")
    names = list(rec["metrics"])
    print(f"{'fold':<5} {'cutoff':<11} {'history':>8} {'cold':>5} {'n':>5}  "
          + "  ".join(f"{n + ' [95% CI]':<22}" for n in names))

    def cells(metrics: dict) -> str:
        return "  ".join(f"{m['value']:.3f} [{m['ci95'][0]:.3f}, {m['ci95'][1]:.3f}]".ljust(22)
                         for m in metrics.values())

    for f in rec["folds"]:
        print(f"{f['fold']:<5} {f['cutoff']:<11} {f['history']['lots']:>8} {f['cold_start_share']:>5.1%} "
              f"{f['n_queries']:>5}  {cells(f['metrics'])}")
    print(f"{'mean':<5} {'':<11} {'':>8} {'':>5} {rec['n_queries']:>5}  {cells(rec['metrics'])}")
    print("spread (min … max, std over folds): " + "; ".join(
        f"{n} {s['min']:.3f} … {s['max']:.3f}, {s['std']:.3f}" for n, s in rec["spread"].items()))


# --- comparison -----------------------------------------------------------------------------------------

def compare_folds(a: dict, b: dict) -> dict:
    """Paired delta A - B per fold (same cases) and its mean over folds, per slice.

    {"folds": {k: compare.paired(...)}, "macro": {slice: {n_queries, metrics: {m: {delta, ci95,
    per_fold, folds_better, n_folds}}}}}; the macro interval resamples the paired differences inside each fold."""
    def by_fold(rec: dict) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {}
        for q in rec["per_query"]:
            out.setdefault(q["fold"], []).append(q)
        return out

    ra, rb = by_fold(a), by_fold(b)
    if set(ra) != set(rb):
        raise ValueError(f"{a['exp_id']} и {b['exp_id']}: разные наборы fold ({sorted(ra)} и {sorted(rb)})")
    per_fold = {k: paired({"exp_id": a["exp_id"], "per_query": ra[k]}, {"exp_id": b["exp_id"], "per_query": rb[k]})
                for k in sorted(ra)}
    macro: dict[str, dict] = {}
    names = sorted(set.intersection(*(set(t) for t in per_fold.values())), key=lambda n: (n != "all", n))
    for name in names:  # every slice present in all folds
        metrics = {}
        for m in METRICS:
            deltas = [t[name]["metrics"][m]["delta"] for t in per_fold.values()]
            lo, hi = macro_bootstrap_ci([_diffs(ra[k], rb[k], name, m) for k in sorted(ra)])
            metrics[m] = {"delta": sum(deltas) / len(deltas), "ci95": [lo, hi], "per_fold": deltas,
                          "folds_better": sum(1 for d in deltas if d > 0), "n_folds": len(deltas)}
        macro[name] = {"n_queries": sum(t[name]["n_queries"] for t in per_fold.values()), "metrics": metrics}
    return {"folds": per_fold, "macro": macro}


def _diffs(rows_a: Sequence[dict], rows_b: Sequence[dict], name: str, metric: str) -> list[float]:
    by_id = {q["query_id"]: q for q in rows_b}
    return [q[metric] - by_id[q["query_id"]][metric] for q in rows_a if name == "all" or name in run.slice_names(q)]


def _compare_main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.eval.folds compare")
    ap.add_argument("a", help="experiment id (minuend), a result of --part folds")
    ap.add_argument("b", help="experiment id (subtrahend), e.g. the best baseline")
    args = ap.parse_args(argv)
    try:
        a, b = (json.loads((run.RESULTS / f"{x}.json").read_text(encoding="utf-8")) for x in (args.a, args.b))
        out = compare_folds(a, b)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(f"Δ = {args.a} − {args.b} | part=folds | парный bootstrap по запросам внутри fold; "
          "mean — среднее по fold, интервал — bootstrap среднего")
    n_folds = len(out["folds"])
    head = "  ".join(f"{'fold ' + str(k):<10}" for k in out["folds"])
    print(f"{'slice':<28} {'metric':<10} {head}  {'mean Δ [95% CI]':<26} {'folds Δ>0':>9}")
    for name in (n for n in out["macro"] if n in COMPARED):
        for m, v in out["macro"][name]["metrics"].items():
            per = "  ".join(f"{d:+.3f}".ljust(10) for d in v["per_fold"])
            mean = f"{v['delta']:+.3f} [{v['ci95'][0]:+.3f}, {v['ci95'][1]:+.3f}]"
            print(f"{name:<28} {m:<10} {per}  {mean:<26} {v['folds_better']:>4}/{n_folds}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["compare"]:
        return _compare_main(argv[1:])
    print("usage: python -m src.eval.folds compare <A> <B>; запуск — python -m src.eval.run --exp <id> --part folds",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
