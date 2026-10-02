"""Recall of the «Новые» pool without hand labels (#187): hide the winner's history, does the pool find him?

    make eval EXP=N-pool-recall          # configs/baselines.yaml: method pool_recall; dev only, holdout sealed
    POOL_DIR=<dir> make eval EXP=…       # the pool parquet of another checkout (src/db/pool.py, make pool-load)

Per dev case with a winner and OKPD2 (without OKPD2 the block is not built, D-036): the companies of the MSP
registry pool (src/db/pool.py) whose main or additional OKVED is under the OKVED plan of the query (the class
and its wholesale codes, src/enrich/discover.py::okved_plan), in the customer's region and its neighbour.
Companies already in organizers' data before as_of are dropped as in the product — except the winners of
the case: we pretend their history is unknown (leave-one-out). Then:

- `in_pool` / `in_region` / `covered`: the winner is in the registry at all / in СПб+ЛО / among the candidates;
- `winner@10`: in the top-10 of a MODEL of the block's rule (product_fit by channel — class 1.0, wholesale 0.6,
  an additional OKVED × `extra_okved`; ties by INN; producers and distributors in turn, as discover._mix);
- `random_okved@10`: expectation for 10 random candidates of the same region and OKVED (exact, no sampling);
- `random_region@10`: expectation for 10 random pool companies of the same region, any OKVED;
- `random_cover`: expectation that as many random companies of the region as there are candidates contain the
  winner — the baseline of `covered`: what the OKVED plan adds over a random set of the same size.

How this differs from the product today (src/enrich/discover.py, rmsp.nalog.ru): the product asks the registry
by the MAIN OKVED only, for ЮЛ only (DISCOVERY_NP_TYPES=UL), and shows the first page of the answer in the
registry's order. `main_only: true` + `entities: [ЮЛ]` (N-pool-recall-ul) is the same channel on the pool; the
default (N-pool-recall) is the pool as #77 plans to use it — additional OKVED and ИП included. The order of the
registry page cannot be reproduced offline: `winner@10` is the modelled order, not the product's output.

The metric checks COMPLETENESS (does the pool contain the companies that really win procurements), not the
precision of new companies: it complements the labelling #21 and the judge #82, it does not replace them.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

from src.db.pool import POOL
from src.enrich import discover, okved_link
from src.eval.metrics import BOOTSTRAP_N, BOOTSTRAP_SEED, bootstrap_ci
from src.eval.split import EvalCase
from src.ingest.load import suppliers_path
from src.match import search as search_mod
from src.match.filters import REGION_CODE
from src.match.profiles import procurement_type
from src.match.store import processed_dir

log = logging.getLogger(__name__)
K = 10
METRICS = ("in_pool", "in_region", "covered", "random_cover", "winner@10", "random_okved@10", "random_region@10",
           "winner@100", "winner_rr")
PAIRED = ("winner@10", "winner@100", "winner_rr")  # the order metrics `compare` pairs (#377)
SIGNALS = ("fit", "main", "report", "link", "size", "geo")  # features of `order:` (#377)


def pool_dir(cfg: dict) -> Path:
    raw = os.environ.get("POOL_DIR") or cfg.get("pool_dir")
    return Path(raw) if raw else POOL


def _shown(path: Path) -> str:
    """The pool directory for the result file: relative to the checkout, never a home path."""
    parts = path.as_posix().split("/")
    return "/".join(parts[parts.index("data"):]) if "data" in parts else path.name


def hit_probability(population: int, winners: int, k: int = K) -> float:
    """P(at least one of `winners` among `k` drawn without replacement from `population`)."""
    if winners <= 0 or population <= 0:
        return 0.0
    if population - winners < k:
        return 1.0
    none = 1.0  # C(N - w, k) / C(N, k) as a product over the w winners: exact, cheap for k in the thousands
    for i in range(winners):
        none *= (population - k - i) / (population - i)
    return 1.0 - none


@dataclass(frozen=True)
class Candidate:
    inn: str
    relation: discover.Relation  # class | wholesale: the channel of the best matching code
    main: bool  # the code is the main OKVED of the company
    region: str


def rule_order(cands: Sequence[Candidate], target: str, weights: dict[str, float],
               extra_okved: float) -> list[Candidate]:
    """Every candidate by the block's rule: score of product_fit and geography, ties by INN."""
    def score(c: Candidate) -> float:
        fit = discover.FIT[c.relation] * (1.0 if c.main else extra_okved)
        geo = 1.0 if c.region == target else discover.NEIGHBOUR_VALUE
        return fit * weights.get("product_fit", 0.0) + geo * weights.get("geography", 0.0)

    return sorted(cands, key=lambda c: (-score(c), c.inn))


def mix(ordered: Sequence[Candidate], k: int = K) -> list[Candidate]:
    """Top-k with producers and distributors in turn (discover._mix), shown in the order of `ordered`."""
    pos = {c.inn: i for i, c in enumerate(ordered)}
    queues = [[c for c in ordered if c.relation == r] for r in ("class", "wholesale")]
    picked: list[Candidate] = []
    while len(picked) < k and any(queues):
        for q in queues:
            if q and len(picked) < k:
                picked.append(q.pop(0))
    return sorted(picked, key=lambda c: pos[c.inn])


def rank(cands: Sequence[Candidate], target: str, weights: dict[str, float], extra_okved: float,
         k: int = K) -> list[str]:
    """Top-k INNs by the block's rule, kinds in turn."""
    return [c.inn for c in mix(rule_order(cands, target, weights, extra_okved), k)]


def signal_order(cands: Sequence[Candidate], order: dict[str, float], *, plan: Sequence[discover.Channel],
                 feat: dict[str, tuple], linked: set[str], target: str) -> list[Candidate]:
    """Every candidate by a weighted sum of registry signals (#377), ties by INN. Signals, each 0…1:
    fit — the channel (class 1.0, wholesale 0.6); main — matched by the MAIN OKVED; report — an OKVED of the
    company's reporting (`okved_report`) is under the plan; link — its main OKVED is learned for the query class
    from history before the cutoff (#250); size — log headcount, 100+ → 1; geo — the customer's region."""
    unknown = set(order) - set(SIGNALS)
    if unknown:
        raise ValueError(f"order: неизвестные сигналы {sorted(unknown)} (есть: {', '.join(SIGNALS)})")
    prefixes = [ch.okved for ch in plan]

    def score(c: Candidate) -> float:
        main_code, report, employees = feat.get(c.inn, (None, (), None))
        x = {"fit": discover.FIT[c.relation], "main": float(c.main),
             "report": float(any(r.startswith(p) for r in report or () for p in prefixes)),
             "link": float(bool(main_code) and main_code[:5] in linked),
             "size": min(1.0, math.log1p(employees or 0) / math.log1p(100)),
             "geo": 1.0 if c.region == target else discover.NEIGHBOUR_VALUE}
        return sum(w * x[name] for name, w in order.items())

    return sorted(cands, key=lambda c: (-score(c), c.inn))


def linked_okveds(path: Path) -> dict[str, set[str]]:
    """OKPD2 class → main OKVED (class level) learned from history (#250, src/enrich/okved_link.py)."""
    if not path.exists():
        raise FileNotFoundError(f"нет {path} — uv run python -m src.enrich.okved_link build --as-of <cutoff>")
    out: dict[str, set[str]] = {}
    sql = okved_link.select(okved_link.MIN_LIFT, okved_link.MIN_SUPPLIERS, okved_link.TOP, okved_link.src_of(path))
    for cls, okved, *_ in duckdb.connect().execute(sql).fetchall():
        out.setdefault(cls, set()).add(okved)
    return out


class Pool:
    """The pool rows of the needed regions in memory: one scan of okved.parquet, then cheap lookups."""

    def __init__(self, directory: Path, regions: Sequence[str], *, entities: Sequence[str] | None = None,
                 main_only: bool = False) -> None:
        for name in ("companies", "okved"):
            if not (directory / f"{name}.parquet").exists():
                raise FileNotFoundError(f"нет {directory / name}.parquet — make pool-load (или POOL_DIR=…)")
        self.con = duckdb.connect()
        regs = ",".join(f"'{r}'" for r in regions if r.isdigit())
        c, o = (directory / "companies.parquet").as_posix(), (directory / "okved.parquet").as_posix()
        listed = ",".join("'" + e.replace("'", "") + "'" for e in entities or ())
        kinds = f" AND entity IN ({listed})" if listed else ""
        self.con.execute(f"CREATE TABLE co AS SELECT inn, entity, region_code FROM read_parquet('{c}')")
        # candidates and the region population: only the entities and OKVED kinds the channel can return
        self.con.execute(f"CREATE TABLE ok AS SELECT code, inn, main, region_code FROM read_parquet('{o}') "
                         f"WHERE region_code IN ({regs}){' AND main' if main_only else ''} "
                         f"AND inn IN (SELECT inn FROM co WHERE true{kinds})")
        self.entity = dict(self.con.execute("SELECT inn, entity FROM co").fetchall())
        self.region = dict(self.con.execute(f"SELECT inn, region_code FROM co WHERE region_code IN ({regs}){kinds}")
                           .fetchall())
        # signals of `order:` (#377): main OKVED, reporting OKVED, headcount of the region's companies
        self.feat: dict[str, tuple] = {r[0]: r[1:] for r in self.con.execute(
            f"SELECT inn, okved_main, okved_report, employees FROM read_parquet('{c}') "
            f"WHERE region_code IN ({regs}){kinds}").fetchall()}
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
    inns = discover._all_inns(suppliers_path(as_of, processed_dir()))  # the product's own reader
    if inns is None:
        raise FileNotFoundError(f"данные организаторов на {as_of} не прочитаны — uv run python -m src.ingest.build")
    return inns


def _summary(rows: list[dict]) -> dict:
    out = {}
    for name in METRICS:
        vals = [r[name] for r in rows]
        lo, hi = bootstrap_ci(vals)
        out[name] = {"value": sum(vals) / len(vals) if vals else 0.0, "ci95": [lo, hi]}
    out["cover_lift"] = lift(rows)  # OKVED plan vs a random set of the same size
    return out


def lift(rows: list[dict], n: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED) -> dict:
    """mean(covered) / mean(random_cover) with a percentile bootstrap over cases (the same resamples for both)."""
    cov = np.array([r["covered"] for r in rows], dtype=float)
    rnd = np.array([r["random_cover"] for r in rows], dtype=float)
    if not rows or rnd.sum() == 0:
        return {"value": None, "ci95": [None, None]}
    idx = np.random.default_rng(seed).integers(0, len(rows), size=(n, len(rows)))
    boot = cov[idx].mean(axis=1) / np.maximum(rnd[idx].mean(axis=1), 1e-12)
    return {"value": float(cov.mean() / rnd.mean()),
            "ci95": [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))]}


def evaluate(cases: list[EvalCase], cfg: dict, cutoff: date) -> dict:
    from src.eval.run import with_cutoff  # run.py dispatches here: a top-level import would be circular

    started = time.monotonic()
    weights = cfg.get("weights") or search_mod.load_config()["weights"]  # the product weights, as discover()
    extra = float(cfg.get("extra_okved", 0.5))
    regions = sorted(set(REGION_CODE.values()))
    entities = cfg.get("entities")
    pool = Pool(pool_dir(cfg), regions, entities=entities, main_only=bool(cfg.get("main_only")))
    order = cfg.get("order")  # None — the block's rule; {signal: weight} — #377
    link_file = str(cfg.get("okved_link", f"okved_link_{cutoff.isoformat()}.parquet"))
    link = linked_okveds(processed_dir() / link_file) if order and order.get("link") else {}
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
        if order:
            classes = discover.okpd_classes(query.okpd2)[:discover.MAX_CLASSES]
            linked = set().union(*(link.get(cls, set()) for cls in classes))
            full = signal_order(cands, order, plan=plan, feat=pool.feat, linked=linked, target=target)
        else:
            full = rule_order(cands, target, weights, extra)
        top = {c.inn for c in mix(full)}
        first = next((i for i, c in enumerate(full) if c.inn in winners), None)
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
            "winner@10": float(bool(winners & top)),
            "winner@100": float(first is not None and first < 100),
            "winner_rr": 1.0 / (first + 1) if first is not None else 0.0,
            "winner_pos": first,  # position of the first winner in the full order, None — not a candidate
            "random_okved@10": hit_probability(len(cands), n_cov),
            "random_region@10": hit_probability(in_region, n_reg),
            "random_cover": hit_probability(in_region, n_reg, len(cands)),
        })
    groups: dict[str, list[dict]] = {}
    for r in rows:
        for name in (f"channel={r['channel']}", f"type={r['type']}",
                     "winner=known(hidden)" if r["winner_known"] else "winner=new", f"entity={r['winner_entity']}"):
            groups.setdefault(name, []).append(r)
    sizes = sorted(r["n_candidates"] for r in rows)
    return {
        "n_queries": len(rows), "skipped": skipped, "elapsed_s": round(time.monotonic() - started, 1),
        "pool_dir": _shown(pool_dir(cfg)), "regions": regions,
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


def paired(a: dict, b: dict) -> dict:
    """Δ = a − b of the order metrics over the cases both runs have, with a bootstrap interval (#377)."""
    rows_b = {q["query_id"]: q for q in b["per_query"]}
    common = [(q, rows_b[q["query_id"]]) for q in a["per_query"] if q["query_id"] in rows_b]
    out: dict = {"n_queries": len(common)}
    for m in PAIRED:
        d = [x[m] - y[m] for x, y in common]
        lo, hi = bootstrap_ci(d)
        out[m] = {"a": sum(x[m] for x, _ in common) / max(len(common), 1),
                  "b": sum(y[m] for _, y in common) / max(len(common), 1),
                  "delta": sum(d) / max(len(d), 1), "ci95": [lo, hi]}
    return out


def main(argv: list[str] | None = None) -> int:
    from src.eval.run import RESULTS

    ap = argparse.ArgumentParser(description="pool order: paired Δ of two pool_recall runs (#377)")
    ap.add_argument("command", choices=("compare",))
    ap.add_argument("a")
    ap.add_argument("b")
    args = ap.parse_args(argv)
    a, b = (json.loads((RESULTS / f"{x}.json").read_text(encoding="utf-8")) for x in (args.a, args.b))
    res = paired(a, b)
    print(f"{args.a} - {args.b}, n={res['n_queries']}")  # ASCII: a Windows console is cp1251
    for m in PAIRED:
        r = res[m]
        print(f"  {m:<11} {r['a']:.4f} - {r['b']:.4f} = {r['delta']:+.4f} [{r['ci95'][0]:+.4f}, {r['ci95'][1]:+.4f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
