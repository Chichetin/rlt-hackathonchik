"""E4 (#269): cross-encoder reranking of the retrieved positions before aggregation into suppliers.

    # quality on a dev subsample (GPU): BM25 and the hybrid, with and without the reranker
    python -m src.eval.ce_bench bench --model BAAI/bge-reranker-v2-m3 --n 500 [--short]
    # latency of the reranker alone on CPU: one query, N pairs (p50 / p95)
    python -m src.eval.ce_bench latency --model BAAI/bge-reranker-v2-m3
    # CE_HF_CACHE=<hub dir> — where the rerankers are cached, if not in the default HF cache

Stage 1 as `src/eval/emb_bench.py` (#115, #267, #313): the same dev sample (seed), the same retrievers and
the same candidates (documents -> lots -> participants before the cutoff -> the first CANDIDATES INNs, as
`emb_bench._cands`). One variable: only the order of the top-N documents of a retriever changes, the
document set does not. A document is a distinct position name or lot subject of the cutoff index; the
cross-encoder scores the pair (query text, document text as in the index).

Variants per retriever: `base` — the retriever as is (BM25 scores / RRF scores); `rank_control` — the same
order with rank scores 1/(1+r) (what a reranked list gets, so the reranker is compared without the score
scale change); `ce@N` — the top-N documents sorted by the cross-encoder score, the tail N+1.. after them in
the retriever order; `ce_rrf@N` — inside the top-N, RRF of the retriever rank and the cross-encoder rank;
`ce_tie@N` — the groups of equal retriever score inside the top-N ordered by their best cross-encoder score,
a group keeps one score (the candidates SQL breaks the tie by n_lots, as for BM25).
Retrievers: BM25 (`prefix5`) and the hybrid RRF BM25 + deepvk/USER-base (the best hybrid of #267 / #313 by
cand_recall@200). Result: eval/results/E4-<reranker>-<lot|short>-n<n>.json (+ -per_query.json).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import random
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from src.db.norm import normalize
from src.eval.emb_bench import (
    CANDIDATES,
    DELTA_METRICS,
    PREFIX,
    TOP_DOCS,
    Corpus,
    _cands,
    _commit,
    _deltas,
    _row,
    _slice_groups,
    _summary,
    load_model,
    short_text,
    slug,
    vec_files,
)
from src.eval.split import ROOT, load_cases, split_config
from src.match.index import get_index
from src.match.pg_retriever import KNN, RRF_K, rrf
from src.match.store import ParquetStore, processed_dir
from src.match.text import tokenize, tokenizer_version

log = logging.getLogger(__name__)
RERANK_N = (50, 100, 200)
CE_MAX_LEN = 256  # query + position name; a long lot text is truncated first (longest_first)
HYBRID_MODEL = "deepvk/USER-base"
STOP_MS = 10_000  # latency: a larger N is not measured once p50 at a smaller one exceeds this (only slower)
# Checked on the HF model cards (02.10): bge-reranker-v2-m3 — Apache-2.0, multilingual (bge-m3 base), score =
# raw logit, sigmoid maps it to [0, 1] (monotone: the order is the same), fp16 as the card's FlagReranker;
# ru-reranker-modernbert-small — MIT, Russian, RuModernBERT-small 34M (#254: the only one that may fit CPU).
RERANKERS = {"BAAI/bge-reranker-v2-m3": "float16", "ARGA100/ru-reranker-modernbert-small": "float32"}


def rank_scores(docs: Sequence[int]) -> list[tuple[int, float]]:
    """Documents in order -> (doc_id, 1/(1+rank)): only the order reaches the candidates."""
    return [(d, 1.0 / (1 + r)) for r, d in enumerate(docs)]


def rerank(hits: Sequence[tuple[int, float]], ce: Mapping[int, float], n: int,
           fuse: bool = False) -> list[tuple[int, float]]:
    """Top-n documents of `hits` reordered by the cross-encoder score, the tail after them in the retriever order.

    fuse=True: inside the top-n, RRF of the retriever rank and the cross-encoder rank (RRF_K as pg_retriever).
    Ties keep the retriever order (stable sort). Scores are rank scores 1/(1+r) of the new order.
    """
    docs = [d for d, _ in hits]
    head, tail = docs[:n], docs[n:]
    by_ce = sorted(head, key=lambda d: -ce[d])
    if fuse:
        ce_rank = {d: r for r, d in enumerate(by_ce)}
        fused = {d: 1.0 / (RRF_K + 1 + r) + 1.0 / (RRF_K + 1 + ce_rank[d]) for r, d in enumerate(head)}
        by_ce = sorted(head, key=lambda d: -fused[d])
    return rank_scores(by_ce + tail)


def rerank_ties(hits: Sequence[tuple[int, float]], ce: Mapping[int, float], n: int) -> list[tuple[int, float]]:
    """Groups of equal retriever score inside the top-n reordered by their best cross-encoder score.

    A group keeps one shared score, so the candidates SQL still breaks the tie by the number of lots
    (BM25 under prefix5 gives many names the same score; rank_control shows the tie itself carries signal).
    The tail n+1.. keeps the retriever order and its own ties. Group order ties keep the retriever order.
    """
    def groups(part: Sequence[tuple[int, float]]) -> list[list[int]]:
        out: dict[float, list[int]] = {}
        for d, sc in part:
            out.setdefault(sc, []).append(d)
        return list(out.values())

    head = sorted(groups(hits[:n]), key=lambda g: -max(ce[d] for d in g))
    return [(d, 1.0 / (1 + r)) for r, g in enumerate(head + groups(hits[n:])) for d in g]


def _revision(model: str, cache: str | None) -> str | None:
    """Snapshot commit of the cached model (the card version the numbers belong to)."""
    from huggingface_hub import try_to_load_from_cache  # pyright: ignore[reportMissingImports]

    path = try_to_load_from_cache(model, "config.json", cache_dir=cache)
    return Path(path).parent.name if isinstance(path, str) else None


class Scorer:
    """Cross-encoder (AutoModelForSequenceClassification, one logit) as on the bge-reranker-v2-m3 card."""

    def __init__(self, model: str, device: str | None = None, max_length: int = CE_MAX_LEN):
        import torch  # pyright: ignore[reportMissingImports]
        from transformers import (  # pyright: ignore[reportMissingImports]
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )

        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = getattr(torch, RERANKERS.get(model, "float32")) if self.device == "cuda" else torch.float32
        cache = None  # the default hub cache (HF_HOME): no environment reads outside src/settings.py
        self.tok = AutoTokenizer.from_pretrained(model, cache_dir=cache)
        self.model = AutoModelForSequenceClassification.from_pretrained(model, dtype=dtype, cache_dir=cache)
        self.model.to(self.device).eval()
        self.max_length = max_length
        self.revision = _revision(model, cache)
        self.dtype = str(dtype).removeprefix("torch.")

    def score(self, pairs: Sequence[tuple[str, str]], batch_size: int = 128) -> np.ndarray:
        """Raw logits (higher = more relevant), in the order of `pairs`; batches by length (less padding)."""
        import torch  # pyright: ignore[reportMissingImports]

        out = np.empty(len(pairs), np.float32)
        order = sorted(range(len(pairs)), key=lambda i: -(len(pairs[i][0]) + len(pairs[i][1])))
        with torch.inference_mode():
            for i in range(0, len(order), batch_size):
                ix = order[i:i + batch_size]
                enc = self.tok([pairs[j][0] for j in ix], [pairs[j][1] for j in ix], padding=True,
                               truncation=True, max_length=self.max_length, return_tensors="pt").to(self.device)
                out[ix] = self.model(**enc).logits.view(-1).float().cpu().numpy()
        return out


def _sample(n: int, seed: int) -> list:
    cases = load_cases("dev", split_config())
    return random.Random(seed).sample(cases, min(n, len(cases)))  # the same draw as emb_bench.bench


def _variant_rows(store, idx, as_of: date, cases, hits: list[list[tuple[int, float]]],
                  ce: list[dict[int, float]] | None, ns: Sequence[int]) -> dict[str, list[dict]]:
    """ce=None: base and rank_control; otherwise ce@N and ce_rrf@N."""
    def rows(hh: list[list[tuple[int, float]]]) -> list[dict]:
        return [_row(_cands(store, idx, h, as_of), c) for h, c in zip(hh, cases, strict=True)]

    if ce is None:
        return {"base": rows(hits), "rank_control": rows([rank_scores([d for d, _ in h]) for h in hits])}
    out = {}
    for n in ns:
        out[f"ce@{n}"] = rows([rerank(h, s, n) for h, s in zip(hits, ce, strict=True)])
        out[f"ce_rrf@{n}"] = rows([rerank(h, s, n, fuse=True) for h, s in zip(hits, ce, strict=True)])
        out[f"ce_tie@{n}"] = rows([rerank_ties(h, s, n) for h, s in zip(hits, ce, strict=True)])
    return out


def _report(variants: dict[str, list[dict]], bm25_base: list[dict] | None, groups: dict[str, list[int]]) -> dict:
    """Per variant: metrics, paired Δ to the same retriever without the reranker and (bm25_base given) to BM25,
    slices with the same Δ."""
    base = variants["base"]
    out = {}
    for name, rows in variants.items():
        r: dict = {"metrics": _summary(rows)}
        refs = {"delta_vs_base": base if name != "base" else None, "delta_vs_bm25": bm25_base}
        for key, ref in refs.items():
            if ref is not None:
                r[key] = _deltas(rows, ref)
        sl = {}
        for g, ix in groups.items():
            sl[g] = _summary([rows[i] for i in ix])
            for key, ref in refs.items():
                if ref is not None and ix:
                    sl[g][key] = _deltas([rows[i] for i in ix], [ref[i] for i in ix])
        r["slices"] = sl
        out[name] = r
    return out


def bench(models: list[str], n: int, seed: int, out_dir: Path, short: bool = False,
          ns: Sequence[int] = RERANK_N, hybrid_model: str = HYBRID_MODEL) -> None:
    import torch  # pyright: ignore[reportMissingImports]

    cfg = split_config()
    as_of = date.fromisoformat(str(cfg["cutoff"]))
    store = ParquetStore(processed_dir())
    idx = get_index(store, as_of)
    cases = _sample(n, seed)
    raw = [short_text(c.query.text) if short else c.query.text for c in cases]
    t0 = time.time()
    bm25 = [idx.retrieve(tokenize(t), TOP_DOCS) for t in raw]
    corpus = Corpus(hybrid_model, idx)
    enc = load_model(hybrid_model)
    qv = enc.encode([PREFIX[hybrid_model][0] + normalize(t) for t in raw], batch_size=64,
                    normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True)
    hyb = [rrf(h, [d for d, _ in v], TOP_DOCS) for h, v in zip(bm25, corpus.search(qv), strict=True)]
    del corpus, enc
    torch.cuda.empty_cache()
    log.info("retrieval: %.0f s", time.time() - t0)
    groups = _slice_groups(cases, bm25, idx, as_of)
    t0 = time.time()
    plain = {"bm25": _variant_rows(store, idx, as_of, cases, bm25, None, ns),
             "hybrid_rrf": _variant_rows(store, idx, as_of, cases, hyb, None, ns)}
    log.info("candidates without the reranker: %.0f s", time.time() - t0)
    top = max(ns)
    need = [sorted({d for d, _ in h[:top]} | {d for d, _ in y[:top]}) for h, y in zip(bm25, hyb, strict=True)]
    pairs = [(raw[i], idx.text(d)) for i, ds in enumerate(need) for d in ds]
    ids = [c.query.query_id for c in cases]
    kind = "short" if short else "lot"
    out_dir.mkdir(parents=True, exist_ok=True)
    for model in models:
        t0 = time.time()
        scorer = Scorer(model)
        flat = scorer.score(pairs)
        gpu_s = time.time() - t0
        rev, dtype = scorer.revision, scorer.dtype
        del scorer
        torch.cuda.empty_cache()
        ce: list[dict[int, float]] = []
        k = 0
        for ds in need:
            ce.append(dict(zip(ds, flat[k:k + len(ds)].tolist(), strict=True)))
            k += len(ds)
        log.info("%s: %d pairs in %.0f s", slug(model), len(pairs), gpu_s)
        variants = {r: {**plain[r], **_variant_rows(store, idx, as_of, cases, h, ce, ns)}
                    for r, h in (("bm25", bm25), ("hybrid_rrf", hyb))}
        bm25_base = variants["bm25"]["base"]
        name = f"E4-{slug(model)}-{kind}-n{len(cases)}"
        res = {
            "experiment": "E4", "issue": 269, "part": "dev", "query": kind, "n": len(cases), "seed": seed,
            "as_of": as_of.isoformat(), "commit": _commit(), "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "top_docs": TOP_DOCS, "knn": KNN, "candidates": CANDIDATES, "store": store.name,
            "index_docs": idx.n_docs, "tokenizer": tokenizer_version(),
            "reranker": {"model": model, "revision": rev, "dtype": dtype, "max_length": CE_MAX_LEN,
                         "score": "raw logit, higher = more relevant (model card); sigmoid is monotone",
                         "pairs": len(pairs), "gpu_seconds": round(gpu_s, 1),
                         "pair": "(query text, document text of the cutoff index)"},
            "hybrid": {"model": hybrid_model, "fusion": "rrf", "rrf_k": RRF_K,
                       "corpus_files": [p.name for p in vec_files(hybrid_model)]},
            "rerank_n": list(ns),
            "variant_def": {
                "base": "retriever as is (BM25 scores / RRF scores)",
                "rank_control": "same order, scores 1/(1+rank)",
                "ce@N": "top-N by cross-encoder score, tail N+1.. in retriever order, scores 1/(1+rank)",
                "ce_rrf@N": "top-N by RRF(retriever rank, cross-encoder rank), rrf_k as pg_retriever",
                "ce_tie@N": "groups of equal retriever score in the top-N ordered by max cross-encoder score, "
                            "a group keeps one score (n_lots still breaks the tie); added after rank_control "
                            "showed the BM25 ties carry signal (n=500 short, post hoc)",
            },
            "slice_def": {"new_name": "normalized first position not a name of the cutoff index",
                          "winner=new": "src.eval.run.winner_novelty", "bm25_empty": "BM25 found nothing"},
            "retrievers": {r: _report(v, None if r == "bm25" else bm25_base, groups) for r, v in variants.items()},
            "per_query_file": f"{name}-per_query.json",
        }
        (out_dir / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        per_query = {"query_ids": ids, "fields": list(DELTA_METRICS),
                     "rows": {f"{r}/{v}": [[round(x[k], 4) for k in DELTA_METRICS] for x in rows]
                              for r, vs in variants.items() for v, rows in vs.items()}}
        (out_dir / f"{name}-per_query.json").write_text(json.dumps(per_query, ensure_ascii=False), encoding="utf-8")
        for r, rep in res["retrievers"].items():
            for v, x in rep.items():
                d = x.get("delta_vs_base", {}).get("recall@10")
                log.info("%s %s/%s: R@10 %.4f MRR %.4f cand@200 %.4f Δbase R@10 %s", slug(model), r, v,
                         x["metrics"]["recall@10"]["mean"], x["metrics"]["mrr"]["mean"],
                         x["metrics"]["cand_recall@200"]["mean"], d)


def _cpu_name() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def cpu_latency(model: str, n_queries: int, seed: int, out_dir: Path, ns: Sequence[int] = RERANK_N,
                threads: int | None = None, batch_size: int = 32) -> dict:
    """Reranker alone on CPU: one query (as /api/search), its top-N BM25 documents scored in batches."""
    import torch  # pyright: ignore[reportMissingImports]

    if threads:
        torch.set_num_threads(threads)
    cfg = split_config()
    as_of = date.fromisoformat(str(cfg["cutoff"]))
    idx = get_index(ParquetStore(processed_dir()), as_of)
    cases = _sample(n_queries, seed)
    scorer = Scorer(model, device="cpu")
    res: dict = {"experiment": "E4", "issue": 269, "model": model, "revision": scorer.revision,
                 "device": "cpu", "cpu": _cpu_name(), "torch_threads": torch.get_num_threads(),
                 "max_length": CE_MAX_LEN, "batch_size": batch_size, "queries": len(cases), "seed": seed,
                 "commit": _commit(), "created": datetime.now(UTC).isoformat(timespec="seconds"),
                 "loadavg_before": [round(x, 2) for x in os.getloadavg()], "ms": {}}
    for kind in ("short", "lot"):
        raw = [short_text(c.query.text) if kind == "short" else c.query.text for c in cases]
        hits = [idx.retrieve(tokenize(t), max(ns)) for t in raw]
        scorer.score([(raw[0], idx.text(d)) for d, _ in hits[0][:8]], batch_size)  # warm-up
        for n in ns:
            if any(v["p50"] > STOP_MS for k, v in res["ms"].items() if k.startswith(kind)):
                res["ms"][f"{kind}@{n}"] = {"skipped": f"p50 at a smaller N > {STOP_MS} ms"}
                continue
            ms, sizes = [], []
            for q, h in zip(raw, hits, strict=True):
                pairs = [(q, idx.text(d)) for d, _ in h[:n]]
                t0 = time.perf_counter()
                scorer.score(pairs, batch_size)
                ms.append((time.perf_counter() - t0) * 1000)
                sizes.append(len(pairs))
            p50, p95 = np.percentile(ms, [50, 95])
            res["ms"][f"{kind}@{n}"] = {"p50": round(float(p50), 1), "p95": round(float(p95), 1),
                                       "mean": round(float(np.mean(ms)), 1),
                                       "pairs_mean": round(float(np.mean(sizes)), 1)}
            log.info("%s CPU %s@%d: p50 %.0f ms, p95 %.0f ms", slug(model), kind, n, p50, p95)
    res["loadavg_after"] = [round(x, 2) for x in os.getloadavg()]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"E4-latency-{slug(model)}.json").write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                                            encoding="utf-8")
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bench")
    b.add_argument("--model", action="append", required=True, choices=sorted(RERANKERS))
    b.add_argument("--n", type=int, default=500)
    b.add_argument("--seed", type=int, default=20261001)
    b.add_argument("--short", action="store_true", help="query = the first position name (user-like)")
    b.add_argument("--rerank-n", type=int, action="append", help=f"default: {RERANK_N}")
    b.add_argument("--out-dir", type=Path, default=ROOT / "eval" / "results")
    lat = sub.add_parser("latency")
    lat.add_argument("--model", action="append", required=True, choices=sorted(RERANKERS))
    lat.add_argument("--queries", type=int, default=20)
    lat.add_argument("--seed", type=int, default=20261001)
    lat.add_argument("--threads", type=int)
    lat.add_argument("--rerank-n", type=int, action="append", help=f"default: {RERANK_N}")
    lat.add_argument("--out-dir", type=Path, default=ROOT / "eval" / "results")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ns = tuple(sorted(args.rerank_n)) if args.rerank_n else RERANK_N
    if args.cmd == "bench":
        bench(args.model, args.n, args.seed, args.out_dir, args.short, ns)
        return 0
    for model in args.model:
        cpu_latency(model, args.queries, args.seed, args.out_dir, ns, args.threads)
    return 0


if __name__ == "__main__":
    sys.exit(main())
