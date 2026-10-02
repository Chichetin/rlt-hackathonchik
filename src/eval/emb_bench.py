"""Stage 1 of the light embedding models comparison (#115): candidate recall@200 on dev, no Postgres.

    # 1) encode the product names corpus with a model (GPU machine, .venv-gpu):
    python -m src.eval.emb_bench encode --model cointegrated/rubert-tiny2
    # 2) compare BM25, vectors and their RRF fusion on a dev subsample:
    python -m src.eval.emb_bench bench --model cointegrated/rubert-tiny2 --n 500

Corpus = distinct normalized product names (src/db/norm.py), the same texts as `vec.names_e5_small`
(data/gpu/texts-*.parquet). The benchmark keeps only names that are documents of the BM25 index of the
split cutoff, so a name seen only after the cutoff never retrieves anything (no leak, as pg_retriever).

Per query and retriever: documents -> lots -> participants before the cutoff (store.candidates, the
same SQL as search()) -> the first CANDIDATES INNs. Metrics: cand_recall@200 (share of relevant INNs
among them), winner_hit@200 (a grade-2 INN among them), recall@10 in the raw candidate order (not the
component scoring of search() — that is stage 2, #98). Slices: channel, BM25 found nothing.
Exact kNN in memory on GPU (torch), top KNN names as pg_retriever. Result: eval/results/emb-<slug>-s1.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from src.db.norm import normalize
from src.eval.metrics import bootstrap_ci
from src.eval.run import winner_novelty
from src.eval.split import ROOT, load_cases, split_config
from src.match.index import get_index
from src.match.pg_retriever import KNN, norm_map, rrf
from src.match.store import ParquetStore, processed_dir
from src.match.text import tokenize, tokenizer_name, tokenizer_version

log = logging.getLogger(__name__)
GPU_DIR = Path(os.environ.get("DATA_GPU_DIR") or ROOT / "data" / "gpu")
TEXTS = ("texts-e5-small.parquet", "texts-rest.parquet")  # 1 031 361 distinct normalized names
TOP_DOCS = 500  # as search.TOP_DOCS
CANDIDATES = 200  # as search.CANDIDATES
MAX_SEQ = 128
# model -> (query prefix, passage prefix); e5 and USER need them (model cards), rubert-tiny2 has none.
PREFIX = {
    "intfloat/multilingual-e5-small": ("query: ", "passage: "),
    "intfloat/multilingual-e5-base": ("query: ", "passage: "),
    "deepvk/USER-base": ("query: ", "passage: "),
    "cointegrated/rubert-tiny2": ("", ""),
    # #267 (E2), from the HF model cards: bge-m3 "no longer requires adding instructions to the queries",
    # USER-bge-m3 encodes plain texts, both CLS pooling; e5-large as e5; FRIDA retrieval prefixes, CLS pooling.
    "BAAI/bge-m3": ("", ""),
    "deepvk/USER-bge-m3": ("", ""),
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
    "ai-forever/FRIDA": ("search_query: ", "search_document: "),
}
# T5 encoders overflow in fp16 (inf/NaN activations): bf16 keeps the range at the same speed
BF16 = {"ai-forever/FRIDA"}
METRICS = ("cand_recall@200", "winner_hit@200", "recall@10", "mrr")
DELTA_METRICS = ("cand_recall@200", "recall@10", "mrr")  # paired Δ to BM25 and per_query (#267)
# vectors already encoded for production (#48): model -> files
EXISTING = {"intfloat/multilingual-e5-small": ("vecs-e5-small.parquet", "vecs-rest.parquet")}


def slug(model: str) -> str:
    return model.split("/")[-1].lower()


def vec_files(model: str, gpu_dir: Path = GPU_DIR) -> list[Path]:
    names = EXISTING.get(model) or (f"vecs-{slug(model)}.parquet",)
    return [gpu_dir / n for n in names]


def load_model(model: str, device: str | None = None):
    from sentence_transformers import SentenceTransformer, models  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue]

    try:
        m = SentenceTransformer(model, device=device)
    except TypeError:
        # USER-base keeps Normalize at the model root: sentence-transformers 6 reads the HF config.json
        # into it and fails. The same pipeline by hand: Transformer -> mean pooling -> Normalize.
        tr = models.Transformer(model, max_seq_length=MAX_SEQ)
        pool = models.Pooling(tr.get_word_embedding_dimension(), pooling_mode="mean")
        m = SentenceTransformer(modules=[tr, pool, models.Normalize()], device=device)
    m.max_seq_length = MAX_SEQ
    if m.device.type == "cuda" and model in BF16:
        import torch

        m.to(torch.bfloat16)
    elif m.device.type == "cuda":
        m.half()  # fp16 on the laptop GPU: ~2x faster, retrieval quality unchanged at this precision
    return m


def encode_corpus(model: str, gpu_dir: Path = GPU_DIR, chunk: int = 50_000) -> Path:
    """All distinct names -> vecs-<slug>.parquet (norm, emb float16[dim]); resumable by whole chunks."""
    texts = [t for f in TEXTS for t in pq.read_table(gpu_dir / f, columns=["norm"]).column("norm").to_pylist()]
    m = load_model(model)
    dim = m.get_sentence_embedding_dimension()
    passage = PREFIX[model][1]
    bs = 512 if dim <= 384 else 256  # pyright: ignore[reportOptionalOperand]
    out = gpu_dir / f"vecs-{slug(model)}.parquet"
    schema = pa.schema([("norm", pa.string()), ("emb", pa.list_(pa.float16(), dim))],
                       metadata={"model": model, "prefix": passage})
    # longest first inside a chunk: less padding, the order of the file is the order of TEXTS
    started = time.monotonic()
    with pq.ParquetWriter(out, schema) as w:
        for i in range(0, len(texts), chunk):
            part = texts[i:i + chunk]
            emb = m.encode([passage + t for t in part], batch_size=bs, normalize_embeddings=True,
                           show_progress_bar=False, convert_to_numpy=True)
            flat = np.asarray(emb, dtype=np.float16).reshape(-1)
            w.write_table(pa.table({"norm": part, "emb": pa.FixedSizeListArray.from_arrays(flat, dim)}, schema=schema))
            done = i + len(part)
            rate = done / max(time.monotonic() - started, 1e-3)
            log.info("%s %d/%d (%.0f текстов/с, осталось ≈ %.1f мин)", slug(model), done, len(texts), rate,
                     (len(texts) - done) / rate / 60)
    return out


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=ROOT, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class Corpus:
    """Vectors of the names that are documents of the cutoff index, on GPU; name row -> doc_ids."""

    def __init__(self, model: str, idx, gpu_dir: Path = GPU_DIR):
        import torch

        m = norm_map(idx)
        norms: list[str] = []
        chunks: list = []
        # batch by batch straight to the GPU: the whole table in RAM took ~9 GB for a 768-d model (#267)
        for f in vec_files(model, gpu_dir):
            pf = pq.ParquetFile(f)
            dim = pf.schema_arrow.field("emb").type.list_size
            for b in pf.iter_batches(batch_size=100_000):
                names = b.column("norm").to_pylist()
                keep = np.fromiter((n in m for n in names), bool, len(names))
                emb = b.column("emb").flatten().to_numpy(zero_copy_only=False).reshape(-1, dim)[keep]
                norms.extend(n for n, k in zip(names, keep, strict=True) if k)
                chunks.append(torch.from_numpy(np.ascontiguousarray(emb, dtype=np.float16)).cuda())
        self.docs = [m[n] for n in norms]
        self.emb = torch.cat(chunks)
        del chunks
        log.info("%s: %d имён в индексе cutoff из корпуса", slug(model), len(norms))

    def search(self, q: np.ndarray, k: int = KNN) -> list[list[tuple[int, float]]]:
        """(doc_id, cosine) nearest for each query vector, best first (as pg_retriever.vector_docs + sim, #313).

        A document with several names keeps the cosine of its best name.
        """
        import torch

        out: list[list[tuple[int, float]]] = []
        qs = torch.from_numpy(np.asarray(q, dtype=np.float16)).cuda()
        for i in range(0, len(qs), 64):
            top = torch.topk((qs[i:i + 64] @ self.emb.T).float(), k, dim=1)
            for rows, sims in zip(top.indices.cpu().numpy(), top.values.cpu().numpy(), strict=True):
                best: dict[int, float] = {}
                for r, sim in zip(rows, sims, strict=True):
                    for d in self.docs[r]:
                        best.setdefault(d, float(sim))
                out.append(list(best.items()))
        return out


def convex(bm25_hits: list[tuple[int, float]], vec_hits: list[tuple[int, float]], alpha: float,
           top: int = TOP_DOCS) -> list[tuple[int, float]]:
    """Convex combination (Bruch et al. 2023): alpha * vector_norm + (1 - alpha) * bm25_norm.

    Each list is min-max normalized per query (a single hit or equal scores -> 1.0); a document missing
    from a list gets 0 there. Ties by doc_id, as rrf. alpha = 0 is the BM25 order, alpha = 1 the vector order.
    """
    def norm(hits: list[tuple[int, float]]) -> dict[int, float]:
        if not hits:
            return {}
        lo = min(s for _, s in hits)
        span = max(s for _, s in hits) - lo
        return {d: ((s - lo) / span if span > 0 else 1.0) for d, s in hits}

    b, v = norm(bm25_hits), norm(vec_hits)
    score = {d: (1 - alpha) * b.get(d, 0.0) + alpha * v.get(d, 0.0) for d in b.keys() | v.keys()}
    return sorted(score.items(), key=lambda x: (-x[1], x[0]))[:top]


def _cands(store, idx, hits: list[tuple[int, float]], as_of: date) -> list[str]:
    if not hits:
        return []
    return [r[0] for r in store.candidates(idx.lot_hits(hits), as_of, CANDIDATES)]


def _row(ranked: list[str], case) -> dict[str, float]:
    rel = set(case.relevant_inns)
    winners = {i for i, g in case.grades.items() if g >= 2} or rel
    got = set(ranked)
    first = next((i for i, inn in enumerate(ranked) if inn in rel), None)
    return {"cand_recall@200": len(got & rel) / len(rel), "winner_hit@200": float(bool(got & winners)),
            "recall@10": len(set(ranked[:10]) & rel) / len(rel), "mrr": 0.0 if first is None else 1 / (first + 1)}


def _summary(rows: list[dict[str, float]]) -> dict:
    out: dict = {"n": len(rows)}
    for k in METRICS:
        vals = [r[k] for r in rows]
        lo, hi = bootstrap_ci(vals)
        out[k] = {"mean": round(float(np.mean(vals)) if vals else 0.0, 4), "ci95": [round(lo, 4), round(hi, 4)]}
    return out


def _paired(a: list[dict], b: list[dict], key: str = "cand_recall@200") -> dict:
    d = [x[key] - y[key] for x, y in zip(a, b, strict=True)]
    lo, hi = bootstrap_ci(d)
    return {"mean": round(float(np.mean(d)), 4), "ci95": [round(lo, 4), round(hi, 4)]}


def _deltas(a: list[dict], b: list[dict]) -> dict:
    """Paired bootstrap Δ (a − b) per metric; before #267 only cand_recall@200 was compared."""
    return {k: _paired(a, b, k) for k in DELTA_METRICS}


def cpu_latency(model: str, texts: list[str], n: int = 50) -> dict:
    """ms per single query encoded on CPU (the API encodes one query per request)."""
    m = load_model(model, device="cpu")
    q = PREFIX[model][0]
    m.encode([q + "прогрев"])
    ms = []
    for t in texts[:n]:
        t0 = time.perf_counter()
        m.encode([q + t], normalize_embeddings=True, show_progress_bar=False)
        ms.append((time.perf_counter() - t0) * 1000)
    p50, p95 = np.percentile(ms, [50, 95])
    return {"p50_ms": round(float(p50), 1), "p95_ms": round(float(p95), 1), "n": len(ms)}


def short_text(text: str) -> str:
    """A user-like query: the first position name of the lot (Query.text = subject; name; name ...)."""
    parts = [p.strip() for p in text.split("; ") if p.strip()]
    return parts[1] if len(parts) > 1 else parts[0]


def _slice_groups(cases, bm25_hits: list, idx, as_of: date) -> dict[str, list[int]]:
    """Slices (#267): channel, winner new for the customer (as src.eval.run), a "new name" — the normalized
    short query (first position) is not a name of the cutoff index, so BM25 can only match it by tokens."""
    names = norm_map(idx)
    novelty = winner_novelty(cases, as_of) or [None] * len(cases)
    groups = {ch: [i for i, c in enumerate(cases) if c.query.channel == ch] for ch in ("ЭМ", "АИС ГЗ")}
    groups["winner=new"] = [i for i, v in enumerate(novelty) if v is True]
    groups["winner=known"] = [i for i, v in enumerate(novelty) if v is False]
    groups["new_name"] = [i for i, c in enumerate(cases) if normalize(short_text(c.query.text)) not in names]
    groups["bm25_empty"] = [i for i, h in enumerate(bm25_hits) if not h]
    return groups


def _slices(groups: dict[str, list[int]], rows: list[dict], vs: list[dict] | None = None,
            rr: list[dict] | None = None) -> dict:
    """Per slice: summary and paired Δ to BM25 (vs) and, if given, to RRF (rr)."""
    out = {}
    for name, ix in groups.items():
        out[name] = _summary([rows[i] for i in ix])
        if vs is not None and ix:
            out[name]["delta_vs_bm25"] = _deltas([rows[i] for i in ix], [vs[i] for i in ix])
        if rr is not None and ix:
            out[name]["delta_vs_rrf"] = _deltas([rows[i] for i in ix], [rr[i] for i in ix])
    return out


def bench(models: list[str], n: int, seed: int, out_dir: Path, short: bool = False,
          latency: bool = True) -> dict[str, dict]:
    cfg = split_config()
    as_of = date.fromisoformat(str(cfg["cutoff"]))
    store = ParquetStore(processed_dir())
    idx = get_index(store, as_of)
    cases = load_cases("dev", cfg)
    cases = random.Random(seed).sample(cases, min(n, len(cases)))
    raw = [short_text(c.query.text) if short else c.query.text for c in cases]
    texts = [normalize(t) for t in raw]
    t0 = time.time()
    bm25_hits = [idx.retrieve(tokenize(t), TOP_DOCS) for t in raw]
    base = [_row(_cands(store, idx, h, as_of), c) for h, c in zip(bm25_hits, cases, strict=True)]
    log.info("BM25: %.0f с", time.time() - t0)
    groups = _slice_groups(cases, bm25_hits, idx, as_of)

    def slices(rows: list[dict], vs: list[dict] | None = None) -> dict:
        return _slices(groups, rows, vs)

    meta = {"part": "dev", "query": "short" if short else "lot", "n": len(cases), "seed": seed,
            "as_of": as_of.isoformat(), "commit": _commit(),
            "created": datetime.now(UTC).isoformat(timespec="seconds"), "top_docs": TOP_DOCS, "knn": KNN,
            "candidates": CANDIDATES, "store": store.name, "index_docs": idx.n_docs,
            "slice_def": {"new_name": "normalized first position not a name of the cutoff index",
                          "winner=new": "src.eval.run.winner_novelty"}}
    meta["tokenizer"] = tokenizer_version()
    bm25_key = "bm25" if tokenizer_name() == "prefix5" else f"bm25-{tokenizer_name()}"
    ids = [c.query.query_id for c in cases]

    def per_query(rows: list[dict]) -> dict[str, dict]:  # paired comparison across runs (same seed)
        return {i: {k: r[k] for k in DELTA_METRICS} for i, r in zip(ids, rows, strict=True)}

    results = {bm25_key: {**meta, "retriever": "bm25", "metrics": _summary(base), "slices": slices(base),
                          "per_query": per_query(base)}}
    out_dir.mkdir(parents=True, exist_ok=True)

    def save(key: str) -> None:  # after each model: a crash later keeps what is measured
        name = f"emb-{key}-s1{'-short' if short else ''}.json"
        (out_dir / name).write_text(json.dumps(results[key], ensure_ascii=False, indent=2), encoding="utf-8")

    save(bm25_key)
    for model in models:
        import torch

        corpus = Corpus(model, idx)
        m = load_model(model)
        t0 = time.time()
        qv = m.encode([PREFIX[model][0] + t for t in texts], batch_size=64, normalize_embeddings=True,
                      show_progress_bar=False, convert_to_numpy=True)
        vec_docs = [[d for d, _ in v] for v in corpus.search(qv)]
        vec = [_row(_cands(store, idx, [(d, 1.0 / (1 + r)) for r, d in enumerate(v[:TOP_DOCS])], as_of), c)
               for v, c in zip(vec_docs, cases, strict=True)]
        hyb = [_row(_cands(store, idx, rrf(h, v, TOP_DOCS), as_of), c)
               for h, v, c in zip(bm25_hits, vec_docs, cases, strict=True)]
        elapsed = time.time() - t0
        del corpus, m
        torch.cuda.empty_cache()
        results[slug(model)] = {
            **meta, "model": model, "prefix": PREFIX[model], "corpus_files": [p.name for p in vec_files(model)],
            "vector": {"metrics": _summary(vec), "slices": slices(vec, base), "delta_vs_bm25": _deltas(vec, base),
                    "per_query": per_query(vec)},
            "hybrid_rrf": {"metrics": _summary(hyb), "slices": slices(hyb, base), "delta_vs_bm25": _deltas(hyb, base),
                    "per_query": per_query(hyb)},
            "gpu_seconds": round(elapsed, 1), "cpu_query_latency": cpu_latency(model, texts) if latency else None,
        }
        r = results[slug(model)]
        log.info("%s: recall@10 vec %.3f, hybrid %.3f, bm25 %.3f", model, r["vector"]["metrics"]["recall@10"]["mean"],
                 r["hybrid_rrf"]["metrics"]["recall@10"]["mean"], results[bm25_key]["metrics"]["recall@10"]["mean"])
        save(slug(model))
    return results


# --- #313 (E5): convex combination instead of RRF ---------------------------------------------------------
ALPHA_GRID = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
VARIANTS = ("lot", "short", "mixed")  # mixed: the short query to the vector branch, the full one to BM25
TUNE_SEED = 20261003


def tune_cases(n: int, seed: int = TUNE_SEED) -> list:
    """alpha-tuning sample: dev_full cases that are not among the dev cases (no leak into the evaluation)."""
    cfg = split_config()
    dev_ids = {c.query.query_id for c in load_cases("dev", cfg)}
    pool = sorted((c for c in load_cases("dev_full", cfg) if c.query.query_id not in dev_ids),
                  key=lambda c: c.query.query_id)
    return random.Random(seed).sample(pool, min(n, len(pool)))


def _query_texts(cases, variant: str) -> tuple[list[str], list[str]]:
    """(BM25 raw texts, vector raw texts) of a variant."""
    full = [c.query.text for c in cases]
    short = [short_text(t) for t in full]
    return {"lot": (full, full), "short": (short, short), "mixed": (full, short)}[variant]


def _mean(rows: list[dict], key: str) -> float:
    return float(np.mean([r[key] for r in rows])) if rows else 0.0


def convex_bench(models: list[str], variants: list[str], n: int, seed: int, tune_n: int, out_dir: Path,
                 tune_seed: int = TUNE_SEED) -> None:
    import torch

    cfg = split_config()
    as_of = date.fromisoformat(str(cfg["cutoff"]))
    store = ParquetStore(processed_dir())
    idx = get_index(store, as_of)
    ev = random.Random(seed).sample(load_cases("dev", cfg), n)
    tune = tune_cases(tune_n, tune_seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    tune_ids = [c.query.query_id for c in tune]
    (out_dir / "E5-tune-sample.json").write_text(json.dumps(
        {"source": "dev_full minus dev", "seed": tune_seed, "n": len(tune_ids), "query_ids": tune_ids},
        ensure_ascii=False), encoding="utf-8")
    groups = _slice_groups(ev, [[1]] * len(ev), idx, as_of)  # bm25_empty is per variant, set below
    novelty_ids = [c.query.query_id for c in ev]

    def cands(hits: list[tuple[int, float]]) -> list[str]:
        return _cands(store, idx, hits, as_of)

    for model in models:
        corpus = Corpus(model, idx)
        m = load_model(model)

        def encode(texts: list[str], m=m, prefix: str = PREFIX[model][0]) -> np.ndarray:
            return m.encode([prefix + normalize(t) for t in texts], batch_size=64,
                            normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True)

        for variant in variants:
            t0 = time.time()
            # --- alpha on the tuning sample ---
            bt, vt = _query_texts(tune, variant)
            b_hits = [idx.retrieve(tokenize(t), TOP_DOCS) for t in bt]
            v_hits = corpus.search(encode(vt))
            grid = {}
            for a in ALPHA_GRID:
                rows = [_row(cands(convex(h, v, a)), c) for h, v, c in zip(b_hits, v_hits, tune, strict=True)]
                grid[a] = {"recall@10": round(_mean(rows, "recall@10"), 4), "mrr": round(_mean(rows, "mrr"), 4)}
            rows = [_row(cands(h), c) for h, c in zip(b_hits, tune, strict=True)]
            grid["bm25"] = {"recall@10": round(_mean(rows, "recall@10"), 4), "mrr": round(_mean(rows, "mrr"), 4)}
            alpha = max(ALPHA_GRID, key=lambda a: (grid[a]["recall@10"], grid[a]["mrr"], -a))
            log.info("%s/%s: alpha=%.2f on %d tuning cases (%s)", slug(model), variant, alpha, len(tune), grid)
            # --- evaluation on dev ---
            be, ve = _query_texts(ev, variant)
            bm = [idx.retrieve(tokenize(t), TOP_DOCS) for t in be]
            vh = corpus.search(encode(ve))
            base = [_row(cands(h), c) for h, c in zip(bm, ev, strict=True)]
            vec = [_row(cands([(d, 1.0 / (1 + r)) for r, (d, _s) in enumerate(v[:TOP_DOCS])]), c)
                   for v, c in zip(vh, ev, strict=True)]
            rr = [_row(cands(rrf(h, [d for d, _s in v], TOP_DOCS)), c) for h, v, c in zip(bm, vh, ev, strict=True)]
            cv = [_row(cands(convex(h, v, alpha)), c) for h, v, c in zip(bm, vh, ev, strict=True)]
            g = {**groups, "bm25_empty": [i for i, h in enumerate(bm) if not h]}

            def pq_(rows: list[dict], ids: list[str] = novelty_ids) -> dict:
                return {i: {k: r[k] for k in DELTA_METRICS} for i, r in zip(ids, rows, strict=True)}

            res = {
                "model": model, "variant": variant, "part": "dev", "n": len(ev), "seed": seed,
                "as_of": as_of.isoformat(), "commit": _commit(),
                "created": datetime.now(UTC).isoformat(timespec="seconds"),
                "top_docs": TOP_DOCS, "knn": KNN, "candidates": CANDIDATES, "store": store.name,
                "index_docs": idx.n_docs, "tokenizer": tokenizer_version(), "prefix": PREFIX[model],
                "corpus_files": [p.name for p in vec_files(model)],
                "alpha": alpha, "alpha_grid": list(ALPHA_GRID),
                "tuning": {"source": "dev_full minus dev", "seed": tune_seed, "n": len(tune), "grid": grid,
                           "criterion": "max recall@10, then MRR, then smaller alpha",
                           "ids_file": "E5-tune-sample.json"},
                "queries": {"bm25": "short" if variant == "short" else "lot",
                            "vector": "lot" if variant == "lot" else "short"},
                "bm25": {"metrics": _summary(base), "slices": _slices(g, base), "per_query": pq_(base)},
                "vector": {"metrics": _summary(vec), "slices": _slices(g, vec, base),
                           "delta_vs_bm25": _deltas(vec, base)},
                "hybrid_rrf": {"metrics": _summary(rr), "slices": _slices(g, rr, base),
                               "delta_vs_bm25": _deltas(rr, base), "per_query": pq_(rr)},
                "hybrid_convex": {"alpha": alpha, "metrics": _summary(cv), "slices": _slices(g, cv, base, rr),
                                  "delta_vs_bm25": _deltas(cv, base), "delta_vs_rrf": _deltas(cv, rr),
                                  "per_query": pq_(cv)},
                "seconds": round(time.time() - t0, 1),
            }
            (out_dir / f"E5-{slug(model)}-{variant}.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
            c = res["hybrid_convex"]
            log.info("%s/%s: recall@10 bm25 %.4f rrf %.4f convex(%.2f) %.4f; Δ bm25 %s", slug(model), variant,
                     _mean(base, "recall@10"), _mean(rr, "recall@10"), alpha, _mean(cv, "recall@10"),
                     c["delta_vs_bm25"]["recall@10"])
        del corpus, m
        torch.cuda.empty_cache()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("encode")
    e.add_argument("--model", action="append", required=True, choices=sorted(PREFIX))
    b = sub.add_parser("bench")
    b.add_argument("--model", action="append", default=[], choices=sorted(PREFIX))
    b.add_argument("--n", type=int, default=500)
    b.add_argument("--seed", type=int, default=20261001)
    b.add_argument("--out-dir", type=Path, default=ROOT / "eval" / "results")
    b.add_argument("--short", action="store_true", help="query = the first position name (user-like)")
    b.add_argument("--no-latency", action="store_true",
                   help="skip CPU query latency (parallel runs make it noisy; measure on an idle machine)")
    c = sub.add_parser("convex", help="#313 (E5): convex combination vs RRF, alpha tuned on dev_full minus dev")
    c.add_argument("--model", action="append", required=True, choices=sorted(PREFIX))
    c.add_argument("--variant", action="append", choices=VARIANTS, help="default: all")
    c.add_argument("--n", type=int, default=2000)
    c.add_argument("--seed", type=int, default=20261001)
    c.add_argument("--tune-n", type=int, default=1000)
    c.add_argument("--out-dir", type=Path, default=ROOT / "eval" / "results")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.cmd == "encode":
        for model in args.model:
            print({"model": model, "out": str(encode_corpus(model))}, flush=True)
        return 0
    if args.cmd == "convex":
        convex_bench(args.model, args.variant or list(VARIANTS), args.n, args.seed, args.tune_n, args.out_dir)
        return 0
    res = bench(args.model, args.n, args.seed, args.out_dir, args.short, latency=not args.no_latency)
    print(json.dumps({k: (v.get("metrics") or {kk: v[kk]["metrics"] for kk in ("vector", "hybrid_rrf")})
                      for k, v in res.items()}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
