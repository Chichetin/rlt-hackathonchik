"""Recall of the «Новые» pool without hand labels (#187): hide the winner's history, does the pool find him?

    make eval EXP=N-pool-recall          # configs/baselines.yaml: method pool_recall; dev only, holdout sealed
    POOL_DIR=<dir> make eval EXP=…       # the pool parquet of another checkout (src/db/pool.py, make pool-load)

Per dev case with a winner and OKPD2 (without OKPD2 the block is not built, D-036): the companies of the MSP
registry pool (src/db/pool.py) whose main or additional OKVED is under the OKVED plan of the query (the class
and its wholesale codes, src/enrich/discover.py::okved_plan), in the customer's region and its neighbour.
Companies already in organizers' data before as_of are dropped as in the product — except the winners of
the case: we pretend their history is unknown (leave-one-out). Then:

- `in_pool` / `in_region` / `covered`: the winner is in the registry at all / in СПб+ЛО / among the candidates;
- `winner@10`: in the top-10 of the block's rule (product_fit by channel — class 1.0, wholesale 0.6, an
  additional OKVED × `extra_okved`; ties by INN; producers and distributors in turn, as discover._mix);
- `random_okved@10`: expectation for 10 random candidates of the same region and OKVED (exact, no sampling);
- `random_region@10`: expectation for 10 random pool companies of the same region, any OKVED.

The metric checks COMPLETENESS (does the pool contain the companies that really win procurements), not the
precision of new companies: it complements the labelling #21 and the judge #82, it does not replace them.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from src.db.pool import POOL
from src.enrich import discover
from src.eval.metrics import bootstrap_ci
from src.eval.split import EvalCase
from src.ingest.load import suppliers_path
from src.match import search as search_mod
from src.match.filters import REGION_CODE
from src.match.profiles import procurement_type
from src.match.store import processed_dir

log = logging.getLogger(__name__)
K = 10
METRICS = ("in_pool", "in_region", "covered", "winner@10", "random_okved@10", "random_region@10")


def pool_dir(cfg: dict) -> Path:
    raw = os.environ.get("POOL_DIR") or cfg.get("pool_dir")
    return Path(raw) if raw else POOL


def hit_probability(population: int, winners: int, k: int = K) -> float:
    """P(at least one of `winners` among `k` drawn without replacement from `population`)."""
    if winners <= 0 or population <= 0:
        return 0.0
    if population - winners < k:
        return 1.0
    return 1.0 - math.comb(population - winners, k) / math.comb(population, k)


@dataclass(frozen=True)
class Candidate:
    inn: str
    relation: discover.Relation  # class | wholesale: the channel of the best matching code
    main: bool  # the code is the main OKVED of the company
    region: str


def rank(cands: Sequence[Candidate], target: str, weights: dict[str, float], extra_okved: float,
         k: int = K) -> list[str]:
    """Top-k INNs by the block's rule: score of product_fit and geography, ties by INN, kinds in turn."""
    def score(c: Candidate) -> float:
        fit = discover.FIT[c.relation] * (1.0 if c.main else extra_okved)
        geo = 1.0 if c.region == target else discover.NEIGHBOUR_VALUE
        return fit * weights.get("product_fit", 0.0) + geo * weights.get("geography", 0.0)

    ranked = sorted(cands, key=lambda c: (-score(c), c.inn))
    queues = [[c for c in ranked if c.relation == r] for r in ("class", "wholesale")]
    picked: list[Candidate] = []
    while len(picked) < k and any(queues):
        for q in queues:
            if q and len(picked) < k:
                picked.append(q.pop(0))
    return [c.inn for c in sorted(picked, key=lambda c: (-score(c), c.inn))]


class Pool:
    """The pool rows of the needed regions in memory: one scan of okved.parquet, then cheap lookups."""

    def __init__(self, directory: Path, regions: Sequence[str]) -> None:
        for name in ("companies", "okved"):
            if not (directory / f"{name}.parquet").exists():
                raise FileNotFoundError(f"нет {directory / name}.parquet — make pool-load (или POOL_DIR=…)")
        self.con = duckdb.connect()
        regs = ",".join(f"'{r}'" for r in regions if r.isdigit())
        c, o = (directory / "companies.parquet").as_posix(), (directory / "okved.parquet").as_posix()
        self.con.execute(f"CREATE TABLE co AS SELECT inn, entity, region_code FROM read_parquet('{c}')")
        self.con.execute(f"CREATE TABLE ok AS SELECT code, inn, main, region_code FROM read_parquet('{o}') "
                         f"WHERE region_code IN ({regs})")
        self.entity = dict(self.con.execute("SELECT inn, entity FROM co").fetchall())
        self.region = dict(self.con.execute(f"SELECT inn, region_code FROM co WHERE region_code IN ({regs})")
                           .fetchall())
        self._plans: dict[tuple[tuple[str, str], ...], list[Candidate]] = {}


    def candidates(self, plan: Sequence[discover.Channel]) -> list[Candidate]:
        """Companies under the plan's OKVED prefixes, one per INN: main before additional, class before wholesale."""
        key = tuple((ch.okved, ch.relation) for ch in plan)
        if key not in self._plans:
            like = " OR ".join(f"code LIKE '{ch.okved}%'" for ch in plan)  # okved_plan: digits and dots only
            best: dict[str, Candidate] = {}
            for code, inn, main, region in self.con.execute(
                    f"SELECT code, inn, main, region_code FROM ok WHERE {like}").fetchall():
                rel: discover.Relation = next(ch.relation for ch in plan if code.startswith(ch.okved))
                cand = Candidate(inn, rel, bool(main), region)
                old = best.get(inn)
                if old is None or (cand.main, cand.relation == "class") > (old.main, old.relation == "class"):
                    best[inn] = cand
            self._plans[key] = list(best.values())
        return self._plans[key]


def _known(as_of: date | None) -> frozenset[str]:
    """INNs in organizers' data before as_of — the product drops them from «Новые» (discover.known_inns)."""
    path = suppliers_path(as_of, processed_dir())
    rows = duckdb.connect().execute("SELECT DISTINCT inn FROM read_parquet(?) WHERE inn IS NOT NULL",
                                    [path.as_posix()]).fetchall()
    return frozenset(r[0] for r in rows)


def _summary(rows: list[dict]) -> dict:
    out = {}
    for name in METRICS:
        vals = [r[name] for r in rows]
        lo, hi = bootstrap_ci(vals)
        out[name] = {"value": sum(vals) / len(vals) if vals else 0.0, "ci95": [lo, hi]}
    rand = out["random_region@10"]["value"]
    out["lift_vs_random_region"] = {"value": out["winner@10"]["value"] / rand if rand else None}
    return out


def evaluate(cases: list[EvalCase], cfg: dict, cutoff: date) -> dict:
    from src.eval.run import with_cutoff  # run.py dispatches here: a top-level import would be circular

    started = time.monotonic()
    weights = cfg.get("weights") or search_mod.load_config()["weights"]  # the product weights, as discover()
    extra = float(cfg.get("extra_okved", 0.5))
    regions = sorted(set(REGION_CODE.values()))
    pool = Pool(pool_dir(cfg), regions)
    known: dict[date | None, frozenset[str]] = {}
    known_in_region: dict[date | None, int] = {}
    skipped = {"no_winner": 0, "no_okpd2": 0}
    rows: list[dict] = []
    for case in cases:
        query = with_cutoff(case.query, cutoff)
        winners = {inn for inn, g in case.grades.items() if g >= 2}
        plan = discover.okved_plan(query.okpd2)
        if not winners:
            skipped["no_winner"] += 1
            continue
        if not plan:
            skipped["no_okpd2"] += 1
            continue
        if query.as_of not in known:
            known[query.as_of] = _known(query.as_of)
        hidden = known[query.as_of] - winners  # leave-one-out: the winners' history is hidden
        if query.as_of not in known_in_region:
            known_in_region[query.as_of] = sum(1 for inn in pool.region if inn in known[query.as_of])
        target = REGION_CODE.get(search_mod.customer_region(query)[0], "78")  # «География» of the block (#218)
        cands = [c for c in pool.candidates(plan) if c.inn not in hidden]
        top = rank(cands, target, weights, extra)
        # the region population after the product's drop of known INNs, with the hidden winners returned
        in_region = (len(pool.region) - known_in_region[query.as_of]
                     + len(winners & known[query.as_of] & pool.region.keys()))
        cand_inns = {c.inn for c in cands}
        n_cov = len(winners & cand_inns)
        n_reg = sum(1 for w in winners if w in pool.region)
        entity = {e for w in winners if (e := pool.entity.get(w))}
        rows.append({
            "query_id": case.query.query_id, "channel": case.query.channel,
            "type": procurement_type(case.query.okpd2) or "unknown",
            "winner_known": bool(winners & known[query.as_of]),  # False: the winner is really new to the data
            "winner_entity": "+".join(sorted(entity)) or "нет в пуле",
            "n_candidates": len(cands), "n_region": in_region,
            "in_pool": float(bool(entity)), "in_region": float(n_reg > 0), "covered": float(n_cov > 0),
            "winner@10": float(bool(winners & set(top))),
            "random_okved@10": hit_probability(len(cands), n_cov),
            "random_region@10": hit_probability(in_region, n_reg),
        })
    groups: dict[str, list[dict]] = {}
    for r in rows:
        for name in (f"channel={r['channel']}", f"type={r['type']}",
                     "winner=known(hidden)" if r["winner_known"] else "winner=new", f"entity={r['winner_entity']}"):
            groups.setdefault(name, []).append(r)
    sizes = sorted(r["n_candidates"] for r in rows)
    return {
        "n_queries": len(rows), "skipped": skipped, "elapsed_s": round(time.monotonic() - started, 1),
        "pool_dir": pool_dir(cfg).as_posix(), "regions": regions,
        "n_candidates_median": sizes[len(sizes) // 2] if sizes else 0,
        "metrics": _summary(rows),
        "slices": {n: {"n_queries": len(g), "metrics": _summary(g)} for n, g in sorted(groups.items())},
        "note": "полнота пула (находит ли пул тех, кто реально выигрывает), не precision «Новых»; "
                "дополняет разметку #21 и судью #82",
        "per_query": rows,
    }


def print_table(rec: dict) -> None:
    print(f"EXP {rec['exp_id']} | part={rec['part']} | n={rec['n_queries']} | skipped={rec['skipped']} "
          f"| медиана кандидатов {rec['n_candidates_median']}")
    print(f"{'slice':<28} {'n':>5}  " + "  ".join(f"{m:<22}" for m in METRICS))
    for name, n, m in [("all", rec["n_queries"], rec["metrics"]),
                       *((k, s["n_queries"], s["metrics"]) for k, s in rec["slices"].items())]:
        cells = (f"{m[x]['value']:.4f} [{m[x]['ci95'][0]:.3f}, {m[x]['ci95'][1]:.3f}]" for x in METRICS)
        print(f"{name:<28} {n:>5}  " + "  ".join(f"{c:<22}" for c in cells))
