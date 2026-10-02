"""Independent relevance check of the «С опытом» top-10 (#301, К2): does the shown supplier supply the subject?

recall@10 counts the participants of the real lot as relevant, i.e. it reproduces what customers did. This asks a
judge that does not rank (a sub-agent of the main session, never the tool) whether the supplier's own purchase
history (names of the items of the lots it took part in before the cutoff) contains the subject of the query.

    python -m src.eval.judge_hist export [--n 50]   -> blind packets eval/judge/hist/packets/*.json + key.json
    python -m src.eval.judge_hist prompt            -> the judge instruction
    python -m src.eval.judge_hist import ANSWER.json [...] [--model NAME]
                                                    -> eval/labels/judge_hist.json (idempotent, per query)
    python -m src.eval.judge_hist score --exp J-301 -> eval/results/J-301.json: precision@10 per system, paired delta

The packets hold neither the system, the rank nor the INN: candidates are shuffled and keyed c01…; the map
key → INN and the ranked lists per system are copied by import into the tracked labels file.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from src.contracts import Query
from src.eval import baselines
from src.eval import run as R
from src.eval.metrics import bootstrap_ci
from src.eval.split import ROOT, EvalCase, load_cases, split_config
from src.match.index import processed_dir

JUDGE_DIR = ROOT / "eval" / "judge" / "hist"  # git-ignored (eval/judge/): packets are derived, answers are imported
LABELS = ROOT / "eval" / "labels" / "judge_hist.json"  # tracked: score reproduces from it without the judge or key.json
SEED = 301
POOL = 300  # the sample is drawn once; --n takes its prefix, so 50 first and the rest later are one sample
SYSTEMS = {"B-bm25": ("baseline", "B-bm25"), "H-15f": ("config", "H-15f"), "product": ("config", None)}
TOP = 10
NAMES = 15  # the most frequent item names of the supplier in a packet

PROMPT = """Ты — судья релевантности. Тебе дан пакет JSON: запрос закупки (query_text) и список поставщиков-кандидатов.
У каждого кандидата (key) — перечень позиций, по которым он участвовал в закупках до даты запроса (history: название
позиции и число лотов) и общее число лотов (lots_total). Названий и контактов компаний у нас нет.

Для каждого кандидата реши, поставляет ли он предмет запроса:
1 — в перечне есть позиции того же товара (услуги) или его прямые разновидности; предмет запроса — часть его обычного
    ассортимента.
0 — перечень про другое: другая отрасль или другой товар, совпадение лишь по общему слову или по общему классу.
null — определить нельзя (в расчёт не идёт).

Правила: оценивай только по данным пакета; ничего не выдумывай и не используй внешние знания о конкретных компаниях.
Порядка выдачи и того, какая система предложила кандидата, ты не видишь и не угадываешь.

Ответ — только JSON:
{"query_id": "<из пакета>", "labels": {"<key>": {"label": 0|1|null, "reason": "одна короткая фраза"}}}
Ключ (key) каждого кандидата из пакета должен быть в ответе ровно один раз."""


def sample(n: int, cfg: dict | None = None) -> list[EvalCase]:
    """n dev_full cases outside the tuning sample (as scripts/fresh_eval.py), seeded; the free-text UI path."""
    cfg = cfg or split_config()
    if os.environ.get("DEV_FULL"):  # a worktree without data/, as in scripts/fresh_eval.py
        cfg = {**cfg, "cases": {**cfg["cases"], "dev_full": os.environ["DEV_FULL"]}}
    tuned = {c.query.query_id for c in load_cases("dev", cfg)}
    pool = sorted((c for c in load_cases("dev_full", cfg) if c.query.query_id not in tuned),
                  key=lambda c: c.query.query_id)
    drawn = random.Random(SEED).sample(pool, POOL)[:n]
    return [c.model_copy(update={"query": c.query.model_copy(
        update={"text": c.query.text.split("; ")[0], "okpd2": []})}) for c in drawn]


def history_names(inns: list[str], cutoff: date, processed: Path | None = None) -> dict[str, dict]:
    """inn → {lots_total, history: [{name, lots}]} from the lots the supplier took part in before the cutoff."""
    p = (processed or processed_dir()).as_posix()
    con = duckdb.connect()
    rows = con.execute(
        "SELECT pt.supplier_inn, i.product_name, count(DISTINCT pt.lot_id) AS lots "
        f"FROM read_parquet('{p}/participants.parquet') pt JOIN read_parquet('{p}/items.parquet') i USING (lot_id) "
        "WHERE pt.supplier_inn IN (SELECT unnest(?)) AND pt.publish_date < ? AND i.product_name IS NOT NULL "
        "GROUP BY 1, 2", [inns, cutoff]).fetchall()
    totals = dict(con.execute(
        f"SELECT supplier_inn, count(DISTINCT lot_id) FROM read_parquet('{p}/participants.parquet') "
        "WHERE supplier_inn IN (SELECT unnest(?)) AND publish_date < ? GROUP BY 1", [inns, cutoff]).fetchall())
    by: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for inn, name, lots in rows:
        by[inn].append((name, lots))
    return {inn: {"lots_total": totals.get(inn, 0),
                  "history": [{"name": n, "lots": c} for n, c in sorted(by[inn], key=lambda x: (-x[1], x[0]))[:NAMES]]}
            for inn in inns}


def make_packet(query_id: str, text: str, ranked: dict[str, list[str]], info: dict[str, dict],
                seed: int = SEED) -> tuple[dict, dict]:
    """(blind packet, key record). The union of the systems' top-10, shuffled; the packet has no INN, rank or system."""
    inns = sorted({i for lst in ranked.values() for i in lst})
    random.Random(f"{seed}:{query_id}").shuffle(inns)
    keys = {inn: f"c{n:02d}" for n, inn in enumerate(inns, start=1)}
    packet = {"query_id": query_id, "query_text": text,
              "candidates": [{"key": keys[i], **info[i]} for i in inns]}
    record = {"query_id": query_id, "keys": {k: i for i, k in keys.items()},
              "ranked": {s: [keys[i] for i in lst] for s, lst in ranked.items()}}
    return packet, record


def top_of(query: Query, cfg: dict) -> list[str]:
    """The top-10 INNs of a method as `make eval` ranks them (search() pipeline or a baseline)."""
    ranked = R._picked_search(query, cfg)[0] if cfg.get("method", "search") == "search" else baselines.rank(query, cfg)
    return ranked[:TOP]


def export(n: int, out: Path = JUDGE_DIR) -> int:
    cfg = split_config()
    cutoff = date.fromisoformat(str(cfg["cutoff"]))
    methods = {}
    for system, (kind, name) in SYSTEMS.items():
        methods[system] = R.load_method_config(f"J-{system}", baseline=name if kind == "baseline" else None,
                                               config_of=name if kind == "config" else None)
    (out / "packets").mkdir(parents=True, exist_ok=True)
    records = {}
    for case in sample(n):
        q = R.with_cutoff(case.query, cutoff)
        ranked = {s: top_of(q, m) for s, m in methods.items()}
        info = history_names(sorted({i for lst in ranked.values() for i in lst}), cutoff)
        packet, rec = make_packet(q.query_id, q.text, ranked, info)
        (out / "packets" / f"{q.query_id}.json").write_text(json.dumps(packet, ensure_ascii=False, indent=1) + "\n",
                                                            encoding="utf-8")
        records[q.query_id] = rec
    (out / "key.json").write_text(json.dumps(records, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"пакетов {len(records)}, кандидатов {sum(len(r['keys']) for r in records.values())} -> {out}")
    return 0


def import_answers(paths: list[Path], model: str, labels: Path = LABELS, packets: Path = JUDGE_DIR / "packets") -> int:
    """Answers → the tracked labels file; every candidate of the packet must be answered exactly once."""
    data: dict[str, Any] = json.loads(labels.read_text(encoding="utf-8")) if labels.exists() else {"queries": {}}
    data.setdefault("records", {})
    key = json.loads((packets.parent / "key.json").read_text(encoding="utf-8"))
    for path in paths:
        ans = json.loads(path.read_text(encoding="utf-8"))
        qid = ans["query_id"]
        want = {c["key"] for c in json.loads((packets / f"{qid}.json").read_text(encoding="utf-8"))["candidates"]}
        got = ans["labels"]
        if set(got) != want:
            raise ValueError(f"{qid}: ответ не совпадает с пакетом (нет {sorted(want - set(got))}, "
                             f"лишнее {sorted(set(got) - want)})")
        bad = {k: v["label"] for k, v in got.items() if v["label"] not in (0, 1, None)}
        if bad:
            raise ValueError(f"{qid}: метки вне 0/1/null: {bad}")
        data["queries"][qid] = {k: v["label"] for k, v in got.items()}
        data["records"][qid] = key[qid]  # key → INN and ranked lists travel with the labels
    data["model"] = model
    labels.parent.mkdir(parents=True, exist_ok=True)
    labels.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=0) + "\n", encoding="utf-8")
    print(f"запросов с метками: {len(data['queries'])} (модель судьи: {model}) -> {labels}")
    return 0


def precision(labels: dict[str, int | None], keys: list[str]) -> float | None:
    """Share of 1 among the labelled keys of a system's top-10; None when the judge labelled none of them."""
    got = [v for k in keys if (v := labels.get(k)) is not None]
    return sum(got) / len(got) if got else None


def score_records(labels: dict[str, dict[str, int | None]], records: dict[str, dict]) -> dict:
    per_query: dict[str, dict[str, float]] = {}
    for qid, lab in labels.items():
        if qid not in records:
            continue
        p = {s: precision(lab, ks) for s, ks in records[qid]["ranked"].items()}
        if all(v is not None for v in p.values()):  # a query counts when every system has a labelled candidate
            per_query[qid] = p  # type: ignore[assignment]
    systems = sorted({s for p in per_query.values() for s in p})
    out: dict = {"n_queries": len(per_query), "precision_at_10": {}, "paired": {}}
    for s in systems:
        v = [p[s] for p in per_query.values()]
        lo, hi = bootstrap_ci(v)
        out["precision_at_10"][s] = {"mean": round(sum(v) / len(v), 4), "ci95": [round(lo, 4), round(hi, 4)]}
    for a in systems:
        for b in systems:
            if a < b:
                d = [p[a] - p[b] for p in per_query.values()]
                lo, hi = bootstrap_ci(d)
                out["paired"][f"{a} - {b}"] = {"mean": round(sum(d) / len(d), 4), "ci95": [round(lo, 4), round(hi, 4)]}
    unjudged = sum(1 for qid, lab in labels.items() if qid in per_query for v in lab.values() if v is None)
    out["null_labels"] = unjudged
    return {**out, "per_query": [{"query_id": q, **p} for q, p in per_query.items()]}


def score(exp: str) -> int:
    if not LABELS.exists():
        print(f"нет {LABELS.relative_to(ROOT)} — сначала import", file=sys.stderr)
        return 2
    data = json.loads(LABELS.read_text(encoding="utf-8"))
    body = score_records(data["queries"], data["records"])
    cfg = {"judge_model": data.get("model"), "systems": {k: v[1] or "product default" for k, v in SYSTEMS.items()},
           "top": TOP, "seed": SEED, "pool": POOL, "names_per_supplier": NAMES,
           "internal_only": True, "human_agreement": None, "decision": "D-133"}
    rec = R.result_record(exp, f"dev_fresh_free_text(n={body['n_queries']},seed={SEED})", cfg, body)
    R.RESULTS.mkdir(parents=True, exist_ok=True)
    out = R.RESULTS / f"{exp}.json"
    out.write_text(R.dump_record(rec), encoding="utf-8")
    for s, m in body["precision_at_10"].items():
        print(f"{s:8s} precision@10 {m['mean']:.3f} {m['ci95']}")
    for k, m in body["paired"].items():
        print(f"{k}: {m['mean']:+.3f} {m['ci95']}")
    print(f"-> {out.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--n", type=int, default=50)
    sub.add_parser("prompt")
    i = sub.add_parser("import")
    i.add_argument("answers", nargs="+", type=Path)
    i.add_argument("--model", required=True)
    s = sub.add_parser("score")
    s.add_argument("--exp", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "export":
        if not 1 <= args.n <= POOL:
            print(f"--n: от 1 до {POOL}", file=sys.stderr)
            return 2
        return export(args.n)
    if args.cmd == "prompt":
        print(PROMPT)
        return 0
    if args.cmd == "import":
        return import_answers(args.answers, args.model)
    return score(args.exp)


if __name__ == "__main__":
    sys.exit(main())
