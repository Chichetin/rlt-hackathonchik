"""`make eval EXP=<id>`: dev-part backtest -> table + eval/results/<id>.json.

Any number on a slide is reproduced by this command (research/EVAL.md). The JSON holds the
config, commit, metrics with bootstrap intervals and per-query values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from src.contracts import Query
from src.eval import baselines
from src.eval.metrics import METRICS, bootstrap_ci
from src.eval.split import ROOT, EvalCase, load_cases, split_config
from src.match import pipeline
from src.match import search as search_mod
from src.match.profiles import procurement_type

log = logging.getLogger(__name__)
SEARCH_CONFIG = ROOT / "configs" / "search.yaml"
# Baselines (T-08): experiment id -> method and its parameters; configs/search.yaml stays the product method.
BASELINES_CONFIG = ROOT / "configs" / "baselines.yaml"
# Experiments of the scoring (#22): experiment id -> overrides of configs/search.yaml (weights, signals, options).
SAMPLE_SEED = 20261001  # --sample: one fixed subset, so screened experiments are paired
EXPERIMENTS_CONFIG = ROOT / "configs" / "experiments.yaml"
RANKED_LIMIT = 5000  # per-case top-k lists are stored for parts up to this size
RESULTS = ROOT / "eval" / "results"
# Files whose bytes define the evaluated configuration (sha256 for eval-final).
CONFIG_FILES = ("configs/search.yaml", "configs/split.yaml")
# Code that defines the method (not UI/API): its committed tree enters the hash.
METHOD_PATHS = ("src/contracts.py", "src/ingest", "src/match", "src/sources", "src/enrich", "src/eval", "src/llm",
                "src/db")  # src/db: normalization and the embedding model of retriever pg_hybrid


class GitError(RuntimeError):
    pass


def git(*args: str, root: Path | None = None, strict: bool = False) -> str:
    """git output; on failure "" — or GitError with strict=True (gates must fail closed)."""
    try:
        r = subprocess.run(["git", "-C", str(root or ROOT), *args], capture_output=True, text=True, check=False)
    except OSError as e:
        if strict:
            raise GitError(str(e)) from e
        return ""
    if r.returncode != 0:
        if strict:
            raise GitError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return ""
    return r.stdout.strip()


def config_sha256(root: Path | None = None) -> str:
    """sha256 of the evaluated configuration: config files + the committed method code (METHOD_PATHS).

    The code tree makes the hash name the method, not only its YAML; without git (no .git) the
    hash cannot be computed and eval-final refuses. Config bytes are LF-normalized, so a Windows
    checkout (core.autocrlf → CRLF) and a Linux one give the same hash (D-023).
    """
    root = root or ROOT
    h = hashlib.sha256()
    for rel in CONFIG_FILES:
        data = (root / rel).read_bytes().replace(b"\r\n", b"\n")
        h.update(rel.encode() + b"\0" + data + b"\0")
    h.update(b"method-tree\0" + git("ls-tree", "-r", "HEAD", "--", *METHOD_PATHS, root=root, strict=True).encode())
    return h.hexdigest()


def with_cutoff(query: Query, cutoff: date) -> Query:
    """Features only before the split cutoff (D-018, research/EVAL.md): as_of defaults to the cutoff.

    A case asking for history after the cutoff would see the evaluated period — refused.
    """
    if query.as_of is None:
        return query.model_copy(update={"as_of": cutoff})
    if query.as_of > cutoff:
        raise ValueError(f"{query.query_id}: as_of {query.as_of} позже cutoff {cutoff} — утечка будущего в признаки")
    return query


def _rank_search(query: Query, cfg: dict) -> list[str]:
    """What the user sees: the history block of /api/search — the same pipeline (src/match/pipeline.py, #220)
    in its backtest mode: user filters on organizers' data and the Evidence check, top-k as on screen; no
    «Новые» (never relevant) and nothing from the cache — status, РНП, МСП, role there are today's facts,
    a leak in a backtest (D-097)."""
    picked = pipeline.pick(query, config=cfg, backtest=True)
    shown = pipeline.verified(picked.history[: cfg.get("k", 20)])  # a row without an INN takes its slot, as on screen
    return [m.counterparty.inn for m in shown if m.counterparty.inn]


def _summary(rows: list[dict]) -> dict:
    metrics = {}
    for name in METRICS:
        vals = [q[name] for q in rows]
        lo, hi = bootstrap_ci(vals)
        metrics[name] = {"value": sum(vals) / len(vals) if vals else 0.0, "ci95": [lo, hi]}
    return metrics


def winner_novelty(cases: list[EvalCase], cutoff: date) -> list[bool | None] | None:
    """Per case: True — no winner of the lot took part at this customer before as_of ("new for the
    customer"), False — at least one did; None — no winner or no customer in the case.

    Returns None (with a warning) when the organizers' data is not built: the slice needs history.
    """
    winners = [[inn for inn, g in c.grades.items() if g >= 2] for c in cases]
    by_as_of: dict[date, list[int]] = {}
    for i, c in enumerate(cases):
        if winners[i] and c.query.customer_inn:
            by_as_of.setdefault(with_cutoff(c.query, cutoff).as_of or cutoff, []).append(i)
    out: list[bool | None] = [None] * len(cases)
    try:
        for as_of, idxs in by_as_of.items():
            known = baselines.history(as_of).known_pairs(cases[i].query.customer_inn or "" for i in idxs)
            for i in idxs:
                out[i] = not any((cases[i].query.customer_inn, w) in known for w in winners[i])
    except FileNotFoundError as e:
        log.warning("срез «новый для заказчика» не посчитан: %s", e)
        return None
    return out


def slice_names(q: dict) -> list[str]:
    """Slices a per_query row belongs to: channel, winner novelty, their combination and procurement
    type by the case's OKPD2 (#104: goods | services | unknown; rows of older results have no type)."""
    ch = f"channel={q['channel']}" if q.get("channel") else None
    nov = {True: "winner=new", False: "winner=known"}.get(q.get("winner_new"))  # type: ignore[arg-type]
    kind = f"type={q['type']}" if q.get("type") else None
    return [name for name in (ch, nov, f"{ch},{nov}" if ch and nov else None, kind) if name]


def slices(per_query: list[dict]) -> dict:
    """Metrics with bootstrap CI per channel, per winner novelty and per their combination."""
    groups: dict[str, list[dict]] = {}
    for q in per_query:
        for name in slice_names(q):
            groups.setdefault(name, []).append(q)
    return {name: {"n_queries": len(rows), "metrics": _summary(rows)} for name, rows in sorted(groups.items())}


def evaluate(cases: list[EvalCase], cfg: dict, cutoff: date | None = None, ranked_limit: int = RANKED_LIMIT) -> dict:
    """Runs the method of `cfg` (search() or a baseline, `cfg['method']`) over the cases.

    Per case: metrics, channel, winner novelty and — up to `ranked_limit` cases — the returned top-k
    INNs (research/ERROR_ANALYSIS.md needs them; a larger part would not fit the 5 MB guard).
    """
    cutoff = cutoff or date.fromisoformat(str(split_config()["cutoff"]))
    method = cfg.get("method", "search")
    novelty = winner_novelty(cases, cutoff)
    per_query = []
    audit = pipeline.TodayAudit() if method == "search" else None
    strict, search_mod.STRICT_RETRIEVER = search_mod.STRICT_RETRIEVER, True  # unavailable retriever fails the run
    started = time.monotonic()
    try:
        for i, case in enumerate(cases):
            query = with_cutoff(case.query, cutoff)
            ranked = _rank_search(query, cfg) if method == "search" else baselines.rank(query, cfg)
            rel = set(case.relevant_inns)
            if audit is not None:
                audit.add(ranked, rel)
            row: dict = {"query_id": case.query.query_id, **{name: fn(ranked, rel) for name, fn in METRICS.items()},
                         "channel": case.query.channel, "type": procurement_type(case.query.okpd2) or "unknown"}
            if novelty is not None:
                row["winner_new"] = novelty[i]
            if len(cases) <= ranked_limit:
                row["ranked"] = ranked
            per_query.append(row)
            if (i + 1) % 50 == 0 or i + 1 == len(cases):  # progress for `Get-Content logs/<run>.log -Wait`
                el = time.monotonic() - started
                log.warning("eval %d/%d, %.1f с/запрос, осталось ≈ %.0f мин", i + 1, len(cases), el / (i + 1),
                            el / (i + 1) * (len(cases) - i - 1) / 60)
    finally:
        search_mod.STRICT_RETRIEVER = strict
    out = {"n_queries": len(per_query), "elapsed_s": round(time.monotonic() - started, 1),
           "metrics": _summary(per_query), "slices": slices(per_query)}
    if audit is not None:
        out["today_filters"] = audit.record()
    return {**out, "per_query": per_query}


def result_record(exp_id: str, part: str, cfg: dict, body: dict) -> dict:
    from src.match.text import tokenizer_name, tokenizer_version

    cfg = {**cfg, "tokenizer": tokenizer_name()}  # BM25_TOKENIZER may differ from the yaml (#116)
    return {
        "exp_id": exp_id,
        "part": part,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--", "src", "configs")),
        "config": cfg,
        "tokenizer_version": tokenizer_version(),
        "config_sha256": _safe_sha(),
        "stub": bool(getattr(search_mod, "IS_STUB", False)) and cfg.get("method", "search") == "search",
        "vectors": _vectors(cfg),
        "store": _store(),
        **body,
    }


def _vectors(cfg: dict) -> dict | None:
    """Model, table and rows of the vector half (pg_hybrid) the run actually used."""
    if cfg.get("retriever") != "pg_hybrid":
        return None
    from src.match import pg_retriever

    return pg_retriever.coverage(cfg.get("embedding_model"))


def _store() -> str:
    """postgres | parquet: where search() read the data (src/match/store.py) — the same metrics expected (#49)."""
    from src.match.store import get_store

    return get_store().name


def _safe_sha() -> str | None:
    try:
        return config_sha256()
    except GitError:
        return None


def print_table(rec: dict) -> None:
    print(f"EXP {rec['exp_id']} | part={rec['part']} | n={rec['n_queries']} | commit={rec['commit'][:8]}"
          f"{' (dirty)' if rec['dirty'] else ''}{' | STUB' if rec['stub'] else ''}")
    names = list(rec["metrics"])
    print(f"{'slice':<32} {'n':>6}  " + "  ".join(f"{n + ' [95% CI]':<22}" for n in names))
    rows = [("all", rec["n_queries"], rec["metrics"]),
            *((name, s["n_queries"], s["metrics"]) for name, s in rec.get("slices", {}).items())]
    for name, n, metrics in rows:
        cells = (f"{m['value']:.3f} [{m['ci95'][0]:.3f}, {m['ci95'][1]:.3f}]" for m in metrics.values())
        print(f"{name:<32} {n:>6}  " + "  ".join(f"{c:<22}" for c in cells))


def dump_record(rec: dict) -> str:
    """JSON with one line per case: 2 000 cases with top-k lists stay readable and well under 5 MB."""
    head = json.dumps({k: v for k, v in rec.items() if k != "per_query"}, ensure_ascii=False, indent=2)
    lines = ",\n".join("    " + json.dumps(q, ensure_ascii=False) for q in rec.get("per_query", []))
    return head[:-2] + ',\n  "per_query": [\n' + lines + "\n  ]\n}\n"


def load_method_config(exp: str, baseline: str | None = None, config_of: str | None = None) -> dict:
    """configs/baselines.yaml entry for `baseline` (default: the experiment id), else configs/search.yaml
    with the overrides of configs/experiments.yaml for this experiment id (none — the product configuration).

    `config_of`: take the overrides of another experiment id (a run of H-15f on the real cases is saved
    as H-15f-real); an id that is not in configs/experiments.yaml is an error, never the product silently.
    """
    known = yaml.safe_load(BASELINES_CONFIG.read_text(encoding="utf-8")) if BASELINES_CONFIG.exists() else {}
    name = baseline or exp
    if name in known:
        return {"baseline": name, **known[name]}
    if baseline:
        raise ValueError(f"baseline {baseline!r} нет в {BASELINES_CONFIG.name}: {sorted(known)}")
    cfg = yaml.safe_load(SEARCH_CONFIG.read_text(encoding="utf-8"))
    overrides = yaml.safe_load(EXPERIMENTS_CONFIG.read_text(encoding="utf-8")) if EXPERIMENTS_CONFIG.exists() else {}
    overrides = overrides or {}
    if config_of is not None and config_of not in overrides:
        raise ValueError(f"эксперимента {config_of!r} нет в {EXPERIMENTS_CONFIG.name}: {sorted(overrides)}")
    applied = overrides.get(config_of or exp) or {}
    for key, value in applied.items():  # one level deep: weights, signals, options
        cfg[key] = {**(cfg.get(key) or {}), **value} if isinstance(value, dict) else value
    if applied:
        log.warning("эксперимент %s: поверх %s наложено %s", config_of or exp, SEARCH_CONFIG.name, applied)
    else:
        log.warning("id %s: записи в %s нет — конфигурация продукта (%s)", exp, EXPERIMENTS_CONFIG.name,
                    SEARCH_CONFIG.name)
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True, help="experiment id, e.g. H-07 (research/EXPERIMENTS.md)")
    ap.add_argument("--part", default="dev", choices=("dev", "dev_full", "real"),
                    help="dev sample (default), all dev cases or the 5–10 real cases; never holdout")
    ap.add_argument("--baseline", default=None,
                    help="entry of configs/baselines.yaml (B-pop, B-inc, B-bm25); default — the --exp id if it is one")
    ap.add_argument("--config-of", default=None,
                    help="entry of configs/experiments.yaml to run under another id (--exp H-15f-real)")
    ap.add_argument("--sample", type=int, default=None,
                    help="first screen on N cases of the part (fixed seed): winners only go to the full run")
    args = ap.parse_args(argv)
    if args.exp.startswith("final"):
        print("id final* зарезервирован за make eval-final", file=sys.stderr)
        return 2
    if args.baseline and args.config_of:
        print("--baseline и --config-of вместе не задаются", file=sys.stderr)
        return 2
    try:
        cfg = load_method_config(args.exp, args.baseline, args.config_of)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    cases = load_cases(args.part, split_config())
    part = args.part
    if args.sample and args.sample < len(cases):
        keep = set(random.Random(SAMPLE_SEED).sample(range(len(cases)), args.sample))
        cases = [c for i, c in enumerate(cases) if i in keep]  # the same N cases for every experiment
        part = f"{args.part}-s{args.sample}"
    rec = result_record(args.exp, part, cfg, evaluate(cases, cfg))
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(dump_record(rec), encoding="utf-8")
    print_table(rec)
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
