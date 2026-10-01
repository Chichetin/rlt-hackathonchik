"""`make eval EXP=<id>`: dev-part backtest -> table + eval/results/<id>.json.

Any number on a slide is reproduced by this command (research/EVAL.md). The JSON holds the
config, commit, metrics with bootstrap intervals and per-query values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from src.contracts import Query
from src.enrich.evidence import strip_unverified
from src.eval.metrics import METRICS, bootstrap_ci
from src.eval.split import ROOT, EvalCase, load_cases, split_config
from src.match import search as search_mod
from src.sources.cache import SourceCache

SEARCH_CONFIG = ROOT / "configs" / "search.yaml"
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


def evaluate(cases: list[EvalCase], cfg: dict, cutoff: date | None = None) -> dict:
    cutoff = cutoff or date.fromisoformat(str(split_config()["cutoff"]))
    per_query = []
    strict, search_mod.STRICT_RETRIEVER = search_mod.STRICT_RETRIEVER, True  # unavailable retriever fails the run
    try:
        for case in cases:
            query = with_cutoff(case.query, cutoff)
            matches = search_mod.search(query, k=cfg.get("k", 20), weights=cfg.get("weights"))
            if not getattr(search_mod, "IS_STUB", False):  # measure what the user sees (as /api/search)
                cache = SourceCache()
                matches = [m.model_copy(update={"counterparty": strip_unverified(m.counterparty, cache.page_text)})
                           for m in matches]
            ranked = [m.counterparty.inn for m in matches if m.origin == "history" and m.counterparty.inn]
            rel = set(case.relevant_inns)
            per_query.append({"query_id": case.query.query_id,
                              **{name: fn(ranked, rel) for name, fn in METRICS.items()}})
    finally:
        search_mod.STRICT_RETRIEVER = strict
    metrics = {}
    for name in METRICS:
        vals = [q[name] for q in per_query]
        lo, hi = bootstrap_ci(vals)
        metrics[name] = {"value": sum(vals) / len(vals) if vals else 0.0, "ci95": [lo, hi]}
    return {"n_queries": len(per_query), "metrics": metrics, "per_query": per_query}


def result_record(exp_id: str, part: str, cfg: dict, body: dict) -> dict:
    return {
        "exp_id": exp_id,
        "part": part,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--", "src", "configs")),
        "config": cfg,
        "config_sha256": _safe_sha(),
        "stub": bool(getattr(search_mod, "IS_STUB", False)),
        "vectors": _vectors(cfg),
        "store": _store(),
        **body,
    }


def _vectors(cfg: dict) -> dict | None:
    """Model, table and rows of the vector half (pg_hybrid) the run actually used."""
    if cfg.get("retriever") != "pg_hybrid":
        return None
    from src.match import pg_retriever

    return pg_retriever.coverage()


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
    print(f"{'metric':<10} {'value':>7}  95% CI")
    for name, m in rec["metrics"].items():
        print(f"{name:<10} {m['value']:>7.3f}  [{m['ci95'][0]:.3f}, {m['ci95'][1]:.3f}]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True, help="experiment id, e.g. H-07 (research/EXPERIMENTS.md)")
    ap.add_argument("--part", default="dev", choices=("dev", "dev_full", "real"),
                    help="dev sample (default), all dev cases or the 5–10 real cases; never holdout")
    args = ap.parse_args(argv)
    if args.exp.startswith("final"):
        print("id final* зарезервирован за make eval-final", file=sys.stderr)
        return 2
    cfg = yaml.safe_load(SEARCH_CONFIG.read_text(encoding="utf-8"))
    rec = result_record(args.exp, args.part, cfg, evaluate(load_cases(args.part, split_config()), cfg))
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_table(rec)
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
