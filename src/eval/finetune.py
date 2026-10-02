"""E3 (#270): contrastive fine-tuning of an embedder on our own pairs of position names.

    # 1) pairs from lots published before AS_OF only (no leak into dev / holdout):
    python -m src.eval.finetune pairs --kind okpd2 --n 100000 --out /mnt/storage/rlt236/e3/pairs/okpd2-100k.parquet
    # 2) one epoch of MultipleNegativesRankingLoss from the base model (GPU):
    python -m src.eval.finetune train --base deepvk/USER-base --pairs <pairs.parquet> --model e3/user-base-okpd2
    # 3) encode the corpus and measure stage 1 against BM25 and the same base model (src/eval/emb_bench.py):
    python -m src.eval.emb_bench encode --model e3/user-base-okpd2
    python -m src.eval.emb_bench bench --model deepvk/USER-base --model e3/user-base-okpd2 --tag E3

Positives: (a) `okpd2` — two distinct normalized names (src/db/norm.py, the corpus key) with the same full
OKPD2 code; (b) `supplier` — two distinct names of lots where the same supplier took part. Negatives are
in-batch only: a batch never holds two pairs of one group (code / supplier) nor a repeated text, so an
in-batch negative is never a known positive. Groups are drawn with probability ~ sqrt(number of names).
AS_OF = 2025-04-01 (Notices.publish_date, the split date field): the pairs serve both stage 1 (cutoff
2025-07-01) and the ranker of #198 (as_of 2025-04-01); dev and holdout start later (configs/split.yaml).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from src.db.norm import normalize

log = logging.getLogger(__name__)
AS_OF = date(2025, 4, 1)
KINDS = ("okpd2", "supplier")
_GROUP_SQL = {
    "okpd2": """select i.okpd2_code as g, i.product_name as t, i.lot_id as lot, n.publish_date as d
                from read_parquet(?) i join read_parquet(?) n using (lot_id)
                where n.publish_date < ? and i.okpd2_code is not null and i.product_name is not null""",
    "supplier": """select p.supplier_inn as g, i.product_name as t, i.lot_id as lot, n.publish_date as d
                   from read_parquet(?) i join read_parquet(?) n using (lot_id)
                   join read_parquet(?) p on p.lot_id = i.lot_id
                   where n.publish_date < ? and p.supplier_inn is not null and i.product_name is not null""",
}


def load_groups(processed: Path, kind: str, as_of: date = AS_OF) -> tuple[dict[str, list[str]], dict]:
    """group (full OKPD2 code / supplier INN) -> sorted distinct normalized names, groups with >= 2 names.

    Only lots with publish_date < as_of. The stats carry the lots used, for the leak check.
    """
    files = [(processed / f).as_posix() for f in ("items.parquet", "notices.parquet")]
    if kind == "supplier":
        files.append((processed / "participants.parquet").as_posix())
    t = duckdb.connect().execute(_GROUP_SQL[kind], [*files, as_of]).to_arrow_table()
    names: dict[str, set[str]] = defaultdict(set)
    for g, text in zip(t.column("g").to_pylist(), t.column("t").to_pylist(), strict=True):
        if n := normalize(text):
            names[g].add(n)
    groups = {g: sorted(v) for g, v in names.items() if len(v) >= 2}
    dates = t.column("d")
    stats = {"rows": t.num_rows, "groups_total": len(names), "groups": len(groups),
             "names": len({n for v in groups.values() for n in v}),
             "max_publish_date": str(max(dates.to_pylist())) if t.num_rows else None,
             "lots": set(t.column("lot").to_pylist())}
    return groups, stats


def sample_batches(groups: dict[str, list[str]], n_pairs: int, batch_size: int,
                   seed: int) -> list[tuple[str, str, str, int]]:
    """(anchor, positive, group, batch): every batch has distinct groups and distinct texts.

    A batch draws min(batch_size, groups) distinct groups (p ~ sqrt(names)), then one random pair of
    distinct names per group; a pair repeating a text already in the batch is dropped.
    """
    rng = np.random.default_rng(seed)
    keys = sorted(groups)
    w = np.sqrt(np.array([len(groups[k]) for k in keys], dtype=float))
    p = w / w.sum()
    out: list[tuple[str, str, str, int]] = []
    for b in range(math.ceil(n_pairs / batch_size)):
        seen: set[str] = set()
        for j in rng.choice(len(keys), size=min(batch_size, len(keys)), replace=False, p=p):
            names = groups[keys[j]]
            i, k = rng.choice(len(names), size=2, replace=False)
            a, pos = names[i], names[k]
            if a in seen or pos in seen:
                continue
            seen.update((a, pos))
            out.append((a, pos, keys[j], b))
    return out[:n_pairs]


def case_lots(cases) -> set[int]:
    """lot ids of eval cases (query_id = lot-<id>)."""
    return {int(c.query.query_id.split("-", 1)[1]) for c in cases if c.query.query_id.startswith("lot-")}


def build_pairs(processed: Path, kind: str, n_pairs: int, batch_size: int, seed: int, out: Path,
                as_of: date = AS_OF, eval_lots: set[int] | None = None) -> dict:
    """Pairs parquet + <out>.json manifest; fails if a pair could come from an eval lot."""
    t0 = time.time()
    groups, stats = load_groups(processed, kind, as_of)
    lots = stats.pop("lots")
    overlap = sorted(lots & (eval_lots or set()))
    if overlap:
        raise ValueError(f"{len(overlap)} обучающих лотов среди кейсов оценки: {overlap[:5]} — утечка")
    pairs = sample_batches(groups, n_pairs, batch_size, seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = list(zip(*pairs, strict=True)) if pairs else [[], [], [], []]
    pq.write_table(pa.table({"anchor": list(cols[0]), "positive": list(cols[1]), "group": list(cols[2]),
                             "batch": pa.array(cols[3], pa.int32())}), out)
    manifest = {"kind": kind, "as_of": as_of.isoformat(), "date_field": "notices.publish_date",
                "n_pairs": len(pairs), "batch_size": batch_size, "batches": len({b for *_, b in pairs}),
                "seed": seed, **stats, "lots_used": len(lots), "eval_lots_checked": len(eval_lots or ()),
                "eval_lots_overlap": 0, "seconds": round(time.time() - t0, 1),
                "created": datetime.now(UTC).isoformat(timespec="seconds")}
    out.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def train(base: str, pairs: Path, model_id: str, lr: float = 2e-5, warmup: float = 0.1, seed: int = 0,
          log_every: int = 50) -> dict:
    """One epoch of MultipleNegativesRankingLoss (GradCache variant) over the pre-built batches, order kept."""
    import torch
    from sentence_transformers.sentence_transformer.losses import (  # pyright: ignore[reportMissingImports]
        CachedMultipleNegativesRankingLoss,
    )
    from transformers import get_linear_schedule_with_warmup  # pyright: ignore[reportMissingImports]

    from src.eval.emb_bench import FINETUNED, PREFIX, build_model, model_path

    if FINETUNED.get(model_id) != base:
        raise ValueError(f"{model_id}: в emb_bench.FINETUNED базовая модель {FINETUNED.get(model_id)}, не {base}")
    torch.manual_seed(seed)
    t = pq.read_table(pairs)
    batch_ids = t.column("batch").to_numpy()
    anchors, positives = t.column("anchor").to_pylist(), t.column("positive").to_pylist()
    bounds = np.flatnonzero(np.diff(batch_ids)) + 1
    batches = [(s, e) for s, e in zip(np.r_[0, bounds], np.r_[bounds, len(batch_ids)], strict=True)]
    qp, pp = PREFIX[base]
    model = build_model(base, device="cuda")
    # the MNRL loss with GradCache: the same in-batch softmax over the whole batch, forward in mini-batches of 64
    # (plain MNRL at 256 pairs x 128 tokens took > 13 GB on the 16 GB card shared with E4)
    loss_fn = CachedMultipleNegativesRankingLoss(model, mini_batch_size=64)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    sched = get_linear_schedule_with_warmup(opt, int(warmup * len(batches)), len(batches))
    model.train()
    losses: list[float] = []
    t0 = time.time()

    def feats(texts: list[str]) -> dict:
        return {k: (v.to("cuda") if hasattr(v, "to") else v) for k, v in model.preprocess(texts).items()}

    # fp32 weights + TF32 matmuls: DeBERTa (USER-base) attention fails under bf16/fp16 autocast (mask fill overflow)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    for step, (s, e) in enumerate(batches, 1):
        loss = loss_fn([feats([qp + x for x in anchors[s:e]]), feats([pp + x for x in positives[s:e]])], None)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
        opt.zero_grad(set_to_none=True)
        losses.append(float(loss.detach()))
        if step % log_every == 0 or step == len(batches):
            log.info("шаг %d/%d, loss %.4f (среднее за %d: %.4f), %.0f с", step, len(batches), losses[-1],
                     log_every, float(np.mean(losses[-log_every:])), time.time() - t0)
    model.eval()
    out = Path(model_path(model_id))
    model.save(str(out))
    manifest_path = pairs.with_suffix(".json")
    info = {"model": model_id, "base": base, "pairs": str(pairs),
            "pairs_manifest": json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None,
            "loss": "CachedMultipleNegativesRankingLoss (= MNRL, scale 20, cos; mini-batch 64), in-batch negatives",
            "epochs": 1,
            "steps": len(batches), "batch_size_max": int(max(e - s for s, e in batches)), "lr": lr,
            "warmup": warmup, "weight_decay": 0.01, "precision": "fp32 + TF32", "prefix": [qp, pp],
            "loss_first50": round(float(np.mean(losses[:50])), 4),
            "loss_last50": round(float(np.mean(losses[-50:])), 4),
            "train_seconds": round(time.time() - t0, 1), "gpu": torch.cuda.get_device_name(),
            "created": datetime.now(UTC).isoformat(timespec="seconds")}
    (out / "e3_train.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pairs")
    p.add_argument("--kind", choices=KINDS, default="okpd2")
    p.add_argument("--n", type=int, default=100_000)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--seed", type=int, default=20261002)
    p.add_argument("--as-of", type=date.fromisoformat, default=AS_OF)
    p.add_argument("--out", type=Path, required=True)
    t = sub.add_parser("train")
    t.add_argument("--base", required=True)
    t.add_argument("--pairs", type=Path, required=True)
    t.add_argument("--model", required=True, help="id from emb_bench.FINETUNED; saved to MODELS_DIR/<slug>")
    t.add_argument("--lr", type=float, default=2e-5)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.cmd == "pairs":
        from src.eval.split import load_cases, split_config
        from src.match.store import processed_dir

        cfg = split_config()
        if args.as_of > date.fromisoformat(str(cfg["cutoff"])):
            raise SystemExit(f"--as-of {args.as_of} позже cutoff {cfg['cutoff']}: пары попадут в период dev")
        eval_lots = case_lots(load_cases("dev", cfg))
        try:  # all dev cases, outside git; holdout is sealed and starts after cutoff anyway (date filter)
            eval_lots |= case_lots(load_cases("dev_full", cfg))
        except FileNotFoundError:
            log.warning("dev_full нет — проверка пересечения только по dev")
        m = build_pairs(processed_dir(), args.kind, args.n, args.batch_size, args.seed, args.out, args.as_of, eval_lots)
        print(json.dumps(m, ensure_ascii=False, indent=1))
        return 0
    print(json.dumps(train(args.base, args.pairs, args.model, args.lr), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
