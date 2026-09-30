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
from datetime import UTC, datetime
from pathlib import Path

import yaml

from src.eval.metrics import METRICS, bootstrap_ci
from src.eval.split import ROOT, EvalCase, load_cases, split_config
from src.match import search as search_mod

SEARCH_CONFIG = ROOT / "configs" / "search.yaml"
RESULTS = ROOT / "eval" / "results"
# Files whose bytes define the evaluated configuration (sha256 for eval-final).
CONFIG_FILES = ("configs/search.yaml", "configs/split.yaml")


def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def config_sha256(root: Path = ROOT) -> str:
    h = hashlib.sha256()
    for rel in CONFIG_FILES:
        h.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    return h.hexdigest()


def evaluate(cases: list[EvalCase], cfg: dict) -> dict:
    per_query = []
    for case in cases:
        matches = search_mod.search(case.query, k=cfg.get("k", 20), weights=cfg.get("weights"))
        ranked = [m.counterparty.inn for m in matches if m.origin == "history" and m.counterparty.inn]
        rel = set(case.relevant_inns)
        per_query.append({"query_id": case.query.query_id,
                          **{name: fn(ranked, rel) for name, fn in METRICS.items()}})
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
        "config_sha256": config_sha256(),
        "stub": bool(getattr(search_mod, "IS_STUB", False)),
        **body,
    }


def print_table(rec: dict) -> None:
    print(f"EXP {rec['exp_id']} | part={rec['part']} | n={rec['n_queries']} | commit={rec['commit'][:8]}"
          f"{' (dirty)' if rec['dirty'] else ''}{' | STUB' if rec['stub'] else ''}")
    print(f"{'metric':<10} {'value':>7}  95% CI")
    for name, m in rec["metrics"].items():
        print(f"{name:<10} {m['value']:>7.3f}  [{m['ci95'][0]:.3f}, {m['ci95'][1]:.3f}]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", required=True, help="experiment id, e.g. H-07 (research/EXPERIMENTS.md)")
    args = ap.parse_args(argv)
    if args.exp.startswith("final"):
        print("id final* зарезервирован за make eval-final", file=sys.stderr)
        return 2
    cfg = yaml.safe_load(SEARCH_CONFIG.read_text(encoding="utf-8"))
    rec = result_record(args.exp, "dev", cfg, evaluate(load_cases("dev", split_config()), cfg))
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.exp}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_table(rec)
    print(f"-> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
