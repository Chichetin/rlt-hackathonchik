"""search(query) -> ranked counterparties from organizers' data. Owner: MATCH.

v1 (T-02, research/METHODOLOGY.md, D-017, D-029):
1. BM25 (`bm25s`, src/match/index.py) over lot positions published strictly before `as_of`;
   matched lots -> their participants (Поставщики) = candidates, `origin="history"`.
2. OKPD2 class of the query: `Query.okpd2`, or — when empty — a vote of the positions that
   matched the query themselves (D-045): group XX.XX first, then the code inside it.
3. Components in [0, 1], one explanation line each:
   product_fit — BM25 similarity (normalized by the best candidate), optionally mixed with how well the name of
                 the best matched position covers the query (options.name_share, #576) + depth of the OKPD2 match;
   experience  — lots and wins in the query's OKPD2 group (level 4) before as_of;
   activity    — days from the last participation to as_of (or to the end of the data);
   geography   — region of the KPP (78 / 47) against Query.region;
   reliability — quality of the supplier from its history before as_of (src/match/reliability.py, #72):
                 `reliability.mode` off (as D-029: "нет данных"), shadow (weight 0, default: filled only for
                 the shown rows by shadow_reliability, never here),
                 on (weight from the config; confirmation of a human №3);
   price       — no data: value 0, weight 0, "нет данных" (D-029).
   score = Σ weight · value with weights from configs/search.yaml (D-004).
Candidates found only in open sources (`origin="discovered"`) are #16: the block is empty here.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
import threading
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import get_args

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from src.contracts import ComponentName, Counterparty, CounterpartyMatch, Evidence, Query, ScoreComponent
from src.ingest.load import from_rows
from src.ingest.okpd2 import label
from src.match import candidates, fallback, ranker
from src.match.index import LotIndex, get_index
from src.match.profiles import profile_overrides, profile_weights
from src.match.reliability import SHADOW_NOTE, History, Prior, history_reliability, reliability_mode
from src.match.signals import customer_lots, mix_incumbent, signal_shares
from src.match.store import LotHits, Store, get_store, levels
from src.match.text import tokenize
from src.regions import NEIGHBOUR, NEIGHBOUR_VALUE, REGION_CODE, REGION_IN, REGION_NAME, REGION_PREP
from src.settings import get_settings

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
SEARCH_CONFIG = ROOT / "configs" / "search.yaml"

IS_STUB = False
RETRIEVERS = ("bm25", "pg_hybrid")  # pg_hybrid: + pgvector, RRF (src/match/pg_retriever.py, #39)
# eval sets True: a configured pg_hybrid that is unavailable is an error, not a silent BM25 measurement.
STRICT_RETRIEVER = False
# hybrid_short (#485): a short query goes to pg_hybrid with its own embedding model, a long one stays on BM25.
# scores: rrf — the fused list keeps RRF scores (as pg_hybrid); bm25 — RRF order, scores rescaled to the query's best
# BM25 (the ranker D-103 was trained on BM25-scale scores).
HYBRID_SHORT_KEYS = ("model", "max_words", "scores")
HYBRID_SCORES = ("rrf", "bm25")

TOP_DOCS = 500  # BM25 documents (distinct position texts) taken per query
CANDIDATES = 200  # suppliers scored per query (by best BM25 of their lots)
VOTE_DOCS = 100  # best matched position texts that vote for the query's OKPD2 when it has none (D-045)
EXACT_DOCS = 20  # best texts checked for the very name of the query before the vote (P-02, #235)
DOC_CODES = "doc_codes.parquet"  # sidecar of the index: OKPD2 codes of the positions carrying each text
VOTE_LOTS = 20  # fallback: only lot subjects matched — top lots vote, a lot's vote split over its positions
FIT_BM25_SHARE = 0.6  # product_fit = 0.6 · BM25 + 0.4 · OKPD2 depth (BM25 only without a class)
OKPD_DEPTH = {4: 1.0, 3: 0.75, 2: 0.5, 1: 0.25, 0: 0.0}
OKPD_LEVEL_TEXT = {4: "совпадает код", 3: "совпадает до подкатегории", 2: "совпадает до группы",
                   1: "совпадает только класс"}
SAT_LOTS, SAT_WINS = 20, 10  # experience saturates at 20 lots / 10 wins in the group
HALF_LIFE_DAYS = 90  # activity halves every 90 days without participation
# Components without data (D-029): shown with value 0 and weight 0, not invented.
UNAVAILABLE = {
    "reliability": "Нет данных: статус, РНП и расторжения ещё не подключены — в score не входит",
    "price": "Нет данных: в выгрузке нет цены контракта, только НМЦК — в score не входит",
}
# options.price scale, but nothing to compare (#585): the weight goes to the other criteria
NO_PRICE = "Нет данных: НМЦК закупки не указана или у поставщика нет лотов с НМЦК — вес отдан другим критериям"


def _without_price(w: dict[str, float]) -> dict[str, float]:
    """Weights with the price share spread over the other criteria in proportion, so the score keeps its scale."""
    total, wp = sum(w.values()), w.get("price", 0.0)
    if wp <= 0 or total <= wp:
        return w
    return {k: 0.0 if k == "price" else round(v * total / (total - wp), 4) for k, v in w.items()}


# Scoring options of experiments (#22, `options:` in configs/search.yaml or configs/experiments.yaml).
# The defaults are v1: without the block nothing changes.
OPTIONS: dict[str, tuple] = {
    "fit_bm25_share": (FIT_BM25_SHARE, None),  # share of BM25 in product_fit, [0, 1]
    # share of the name match inside the text part of product_fit (#576): 0 — BM25 relative to the best candidate
    # only (v1), so the best one always gets 1.0; > 0 — the words of the query found in the best position's name
    "name_share": (0.0, None),
    # experience saturation (#576): fixed — at sat_lots / sat_wins; pool — at least at the most experienced candidate
    # of this query, so in a popular category not everyone with 20+ lots gets 1.0
    "exp_scale": ("fixed", ("fixed", "pool")),
    "experience_by": ("class", ("class", "similar")),  # lots in the OKPD2 group | lots with similar positions
    "tie_break": ("inn", ("inn", "bm25")),  # order of equal scores
    # lots / wins at which experience reaches 1.0 (#196): low caps make active suppliers indistinguishable
    "sat_lots": (SAT_LOTS, None),
    "sat_wins": (SAT_WINS, None),
    # price (#105): none — «нет данных» (v1); scale — НМЦК запроса против медианы НМЦК лотов поставщика
    "price": ("none", ("none", "scale")),
    "price_decades": (2.0, None),  # price reaches 0 at a 10^price_decades difference of scale
    # activity (#105): all — the last participation of any kind; similar — the last lot with similar positions
    "activity_by": ("all", ("all", "similar")),
    # experience (#105): count — 0.5·sat(lots) + 0.5·sat(wins); rate — the wins half is the smoothed win rate
    # (wins + 1) / (lots + 2), so 1 win of 20 lots is not «experienced winner»
    "experience_wins": ("count", ("count", "rate", "rate_aisgz")),  # rate_aisgz: rate only for channel АИС ГЗ
    # order of the formula path (#612): {component: weight} over the values the ranker reads (product_fit without
    # the name match, experience with fixed saturation, geography without the INN region) instead of the card's
    # score; {} — by the score. The score and the card stay the formula of `weights:`; custom weights of a query
    # (the UI sliders) order by the score as before.
    "order_weights": ({}, None),
}
PRICE_DECADES = 2.0  # price = 1 − |log10(budget / median)| / 2: 0 at a 100× difference


def scoring_options(cfg: dict) -> dict:
    """`options:` block of the config over the v1 defaults; unknown names and values are an error."""
    raw = cfg.get("options") or {}
    if unknown := set(raw) - set(OPTIONS):
        raise ValueError(f"неизвестные опции скоринга: {', '.join(sorted(unknown))}")
    out = {name: raw.get(name, default) for name, (default, _) in OPTIONS.items()}
    for name, (_, allowed) in OPTIONS.items():
        if allowed is not None and out[name] not in allowed:
            raise ValueError(f"options.{name} = {out[name]!r}: ожидается одно из {allowed}")
    out["fit_bm25_share"] = float(out["fit_bm25_share"])
    out["price_decades"] = float(out["price_decades"])
    if out["price_decades"] <= 0:
        raise ValueError(f"options.price_decades = {out['price_decades']}: ожидается число > 0")
    out["name_share"] = float(out["name_share"])
    if not 0.0 <= out["name_share"] <= 1.0:
        raise ValueError(f"options.name_share = {out['name_share']}: доля должна быть в [0, 1]")
    if not 0.0 <= out["fit_bm25_share"] <= 1.0:
        raise ValueError(f"options.fit_bm25_share = {out['fit_bm25_share']}: доля должна быть в [0, 1]")
    for name in ("sat_lots", "sat_wins"):
        if isinstance(out[name], bool) or not isinstance(out[name], int) or out[name] < 1:
            raise ValueError(f"options.{name} = {out[name]!r}: ожидается целое число ≥ 1")
    ow = out["order_weights"]
    if not isinstance(ow, dict) or set(ow) - set(get_args(ComponentName)) or any(
            isinstance(v, bool) or not isinstance(v, int | float) or v < 0 for v in ow.values()):
        raise ValueError(f"options.order_weights = {ow!r}: ожидается {{критерий: вес ≥ 0}}")
    out["order_weights"] = {name: float(v) for name, v in ow.items()}
    return out


def hybrid_short(cfg: dict) -> dict | None:
    """`hybrid_short:` of the config checked (#485): {model, max_words, scores}; None when the block is absent."""
    raw = cfg.get("hybrid_short")
    if not raw:
        return None
    if not isinstance(raw, dict) or set(raw) - set(HYBRID_SHORT_KEYS):
        raise ValueError(f"hybrid_short: ожидается {{{', '.join(HYBRID_SHORT_KEYS)}}}, получено {raw!r}")
    model, n, scores = raw.get("model"), raw.get("max_words"), raw.get("scores", "rrf")
    if not isinstance(model, str) or not model:
        raise ValueError("hybrid_short.model: нужна модель эмбеддингов (src/db/embed.py SPECS)")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"hybrid_short.max_words = {n!r}: ожидается целое число ≥ 1")
    if scores not in HYBRID_SCORES:
        raise ValueError(f"hybrid_short.scores = {scores!r}: ожидается одно из {HYBRID_SCORES}")
    if cfg.get("retriever") != "bm25":
        raise ValueError("hybrid_short задаётся только при retriever: bm25")
    return {"model": model, "max_words": n, "scores": scores}


def routed(cfg: dict, text: str) -> dict:
    """The config this query is searched with (#485): a query of 1..hybrid_short.max_words words → retriever pg_hybrid
    with the model of hybrid_short; a longer one — the config as is. Without DATABASE_URL to Postgres the product
    stays on BM25 silently, as before the option; eval (STRICT_RETRIEVER) routes anyway, so missing vectors fail it."""
    hs = hybrid_short(cfg)
    if hs is None or not 0 < len(text.split()) <= hs["max_words"]:
        return cfg
    if not STRICT_RETRIEVER and not get_settings().database_url.startswith("postgres"):
        return cfg
    return {**cfg, "retriever": "pg_hybrid", "embedding_model": hs["model"], "hybrid_scores": hs["scores"]}


def no_customer_options(cfg: dict) -> dict:
    """Scoring options of the path without a customer and without the ranker (#511): `no_customer_options:` of the
    config over `options:`. The ranker reads the components as features (#198) and was trained on `options:`, so
    these apply only where the formula itself orders the answer: free text without a customer (#490)."""
    return scoring_options({"options": {**(cfg.get("options") or {}), **(cfg.get("no_customer_options") or {})}})


def check_config(cfg: dict) -> dict:
    if cfg.get("retriever") not in RETRIEVERS:
        raise ValueError(f"retriever {cfg.get('retriever')!r} не поддерживается (есть: {', '.join(RETRIEVERS)})")
    hybrid_short(cfg)  # short queries to pg_hybrid (#485), off by default
    scoring_options(cfg)
    no_customer_options(cfg)
    candidates.sizes(cfg)
    ranker.spec(cfg)  # learned ranker (#198): on by default since D-103
    profile_overrides(cfg)  # weights by procurement type (#104), off by default
    reliability_mode(cfg)  # supplier quality (#72): shadow by default
    return cfg


def load_config(path: Path = SEARCH_CONFIG) -> dict:
    return check_config(yaml.safe_load(path.read_text(encoding="utf-8")))


def _fmt(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _prefix(code: str, level: int) -> str:
    return {4: code, 3: code[:8], 2: code[:5], 1: code[:2]}[level]


@dataclass
class Candidate:
    inn: str
    bm25: float
    n_lots: int  # matched lots (similar positions) the supplier took part in
    n_wins: int
    best_lot: int
    best_text: str
    best_date: date
    okpd_level: int = 0
    okpd_code: str | None = None  # supplier's code at the deepest match
    class_lots: int = 0
    class_wins: int = 0
    found_by: str = ""  # fallback ladder (src/match/fallback.py): the first phrase of product_fit
    last_similar: date | None = None  # the latest matched lot (#105, options.activity_by: similar)
    counted: str = ""  # what n_lots / n_wins count when not lots with similar positions (#195 extra candidates)


def _retrieve(store: Store, idx: LotIndex, lots: LotHits, as_of: date | None) -> list[Candidate]:
    """Participants of the matched lots before as_of, best BM25 first (src/match/store.py: _CANDIDATES)."""
    rows = store.candidates(lots, as_of, CANDIDATES)
    return [Candidate(inn, bm25, n_lots, n_wins, best_lot, idx.text(doc), best_date, last_similar=last)
            for inn, bm25, n_lots, n_wins, best_lot, doc, best_date, last in rows]


_codes_lock = threading.Lock()


def _doc_codes(store: Store, idx: LotIndex) -> Path:
    """Sidecar (doc_id, code, n): codes of the positions whose name is the document's text (D-045).

    Built from the store (positions of lots before the index's as_of — no leak) and the index's own
    texts, once per index next to it (the index files and its key are untouched); when the index
    directory is read-only (docker mount) — in the temp directory.
    """
    places = (idx.path, Path(tempfile.gettempdir()) / f"rlt-doc-codes-{idx.key}")
    with _codes_lock:
        for base in places:
            if (base / DOC_CODES).exists():
                return base / DOC_CODES
        rows = store.text_codes(idx.as_of)
        codes = pa.table({"text": pa.array([r[0] for r in rows], pa.string()),
                          "code": pa.array([r[1] for r in rows], pa.string()),
                          "n": pa.array([int(r[2]) for r in rows], pa.int32())})
        texts = pq.read_table(idx.path / "texts.parquet", columns=["doc_id", "text"])
        table = (texts.join(codes, "text", join_type="inner").select(["doc_id", "code", "n"])
                 .sort_by([("doc_id", "ascending"), ("code", "ascending")]))
        for n, base in enumerate(places):
            tmp = base / f"{DOC_CODES}.tmp{os.getpid()}"
            try:
                base.mkdir(parents=True, exist_ok=True)
                pq.write_table(table, tmp)
                os.replace(tmp, base / DOC_CODES)
                log.info("built %s for index %s", base / DOC_CODES, idx.key)
                return base / DOC_CODES
            except OSError as e:
                if n == len(places) - 1:
                    raise
                log.warning("индекс %s не доступен на запись (%s) — коды позиций строятся в %s", idx.path, e, places[1])
    raise AssertionError("unreachable")


def prebuild_doc_codes(store: Store, idx: LotIndex) -> Path:
    """Build the position-codes sidecar now (`python -m src.match.index`), not on the first query
    without a code: in docker-compose the index directory is read-only and the temp one dies with
    the container (#83)."""
    return _doc_codes(store, idx)


def _position_codes(store: Store, idx: LotIndex) -> tuple[np.ndarray, pa.Array, np.ndarray]:
    """(offsets by doc_id, codes, n) of the sidecar, read once per loaded index."""
    if "doc_codes" not in idx.sidecars:
        t = pq.read_table(_doc_codes(store, idx))
        doc = t.column("doc_id").to_numpy()
        idx.sidecars["doc_codes"] = (np.searchsorted(doc, np.arange(idx.n_docs + 1)),
                                     t.column("code").combine_chunks(), t.column("n").to_numpy())
    return idx.sidecars["doc_codes"]


def _same_name_code(idx: LotIndex, tokens: list[str], docs: list[tuple[int, float]], off: np.ndarray,
                    codes: pa.Array, n: np.ndarray) -> str | None:
    """The most frequent code of the positions named exactly like the query; None when no such text."""
    counts: dict[str, int] = defaultdict(int)
    for d, _ in docs:
        if tokens and tokenize(idx.text(d)) == tokens:
            for i in range(off[d], off[d + 1]):
                counts[codes[i].as_py()] += int(n[i])
    return min(counts, key=lambda c: (-counts[c], c.encode())) if counts else None


def _predict_okpd2(store: Store, idx: LotIndex, hits: list[tuple[int, float]], lots: LotHits,
                   query_tokens: list[str] | None = None) -> str | None:
    """OKPD2 of a query without a code, from the documents that matched it; D-045.

    A matched text votes with the codes of the positions that carry this very text — not with the
    other positions of their lots. One vote per text, its score split over its codes; the
    VOTE_DOCS best texts vote. Texts that are only lot subjects have no code of their own: they
    vote only when no position matched at all — the VOTE_LOTS best lots, a lot's score split over
    its positions. The heaviest group XX.XX wins, then the heaviest code inside it (9 digits
    scatter the votes). Ties: bytewise order of the group / code.

    Before the vote (P-02, #235): when one of the EXACT_DOCS best texts is the query itself (the same
    BM25 tokens), the code most often given to positions with this name decides — the vote of a hundred
    neighbours used to outweigh it (P-01: class 0.718 → 0.922 on names seen before).
    """
    votes: dict[str, float] = defaultdict(float)
    try:
        off, codes, n = _position_codes(store, idx)
    except (OSError, duckdb.Error) as e:
        log.warning("коды позиций не построены (%s) — класс определяют лучшие лоты", e)
    else:
        coded = sorted(((d, s) for d, s in hits if off[d + 1] > off[d]), key=lambda h: (-h[1], h[0]))
        if exact := _same_name_code(idx, query_tokens or [], coded[:EXACT_DOCS], off, codes, n):
            return exact
        for d, s in coded[:VOTE_DOCS]:
            total = int(n[off[d]:off[d + 1]].sum())
            for i in range(off[d], off[d + 1]):
                votes[codes[i].as_py()] += s * int(n[i]) / total
    if not votes:
        score = dict(lots.top(VOTE_LOTS))
        per_lot: dict[tuple[int, str], int] = defaultdict(int)
        for lot_id, code in store.lot_codes(list(score)):  # one row per position
            per_lot[(lot_id, code)] += 1
        positions: dict[int, int] = defaultdict(int)
        for (lot_id, _), cnt in per_lot.items():
            positions[lot_id] += cnt
        for (lot_id, code), cnt in per_lot.items():
            votes[code] += score[lot_id] * cnt / positions[lot_id]
    if not votes:
        return None
    groups: dict[str, float] = defaultdict(float)
    for code, w in votes.items():
        groups[code[:5]] += w
    grp = min(groups, key=lambda g: (-groups[g], g.encode()))
    return min((c for c in votes if c[:5] == grp), key=lambda c: (-votes[c], c.encode()))


def _class_history(store: Store, cands: list[Candidate], codes: list[str], as_of: date | None) -> None:
    """Deepest OKPD2 match over each candidate's history and lots/wins in the query's group."""
    by_inn = store.class_history([c.inn for c in cands], codes, as_of)
    for c in cands:
        if c.inn in by_inn:
            c.okpd_level, c.okpd_code, c.class_lots, c.class_wins = by_inn[c.inn]
            if c.okpd_level == 0:
                c.okpd_code = None


def _sat(n: int, cap: int) -> float:
    return min(1.0, math.log1p(n) / math.log1p(cap))


def _period(as_of: date | None) -> str:
    return f"до {_fmt(as_of)}" if as_of else "за весь период данных"


def _name_match(query_tokens: set[str], text: str) -> tuple[float, int, int]:
    """How well a position name matches the query (#576): 0.7 · share of the query words found in the name +
    0.3 · share of the name's words that are in the query (a long name with one common word is a weak match).
    Returns (value, query words found, query words)."""
    name = set(tokenize(text))
    if not query_tokens or not name:
        return 0.0, 0, len(query_tokens)
    hit = len(query_tokens & name)
    return 0.7 * hit / len(query_tokens) + 0.3 * hit / len(name), hit, len(query_tokens)


def _product_fit(c: Candidate, top_bm25: float, codes: list[str], predicted: bool,
                 how: str = "", share: float = FIT_BM25_SHARE, name_share: float = 0.0,
                 query_tokens: set[str] | None = None) -> tuple[float, str]:
    sim = c.bm25 / top_bm25 if top_bm25 > 0 else 0.0
    best = c.best_text if len(c.best_text) <= 60 else c.best_text[:57] + "…"
    head = c.found_by or f"Похожие{how} позиции в {c.n_lots} лот(ах), лучшее совпадение «{best}» (лот {c.best_lot})"
    if name_share > 0 and query_tokens:
        name, hit, total = _name_match(query_tokens, c.best_text)
        sim = name_share * name + (1 - name_share) * sim
        head = f"{head}; в наименовании совпало слов запроса: {hit} из {total}"
    if not codes:
        return sim, f"{head}; ОКПД2 запроса не определён"
    src = "ОКПД2 определён по похожим позициям" if predicted else "ОКПД2 запроса"
    value = share * sim + (1 - share) * OKPD_DEPTH[c.okpd_level]
    if c.okpd_level == 0 or c.okpd_code is None:
        return value, f"{head}; {src} {', '.join(codes)} в его истории не встречается"
    return value, (f"{head}; {src}: {OKPD_LEVEL_TEXT[c.okpd_level]} "
                   f"{label(_prefix(c.okpd_code, c.okpd_level))}")


def _experience(c: Candidate, codes: list[str], as_of: date | None, by: str = "class",
                sat_lots: int = SAT_LOTS, sat_wins: int = SAT_WINS, wins: str = "count") -> tuple[float, str]:
    if codes and by == "class":
        groups = ", ".join(label(g) for g in sorted({x[2] or x[0] for x in map(levels, codes)}))
        lots, wins_n, what = c.class_lots, c.class_wins, f"по ОКПД2 {groups}"
    else:
        lots, wins_n, what = c.n_lots, c.n_wins, c.counted or "с похожими позициями"
    n_wins = wins_n
    if wins == "rate":
        rate = (n_wins + 1) / (lots + 2)
        value = 0.5 * _sat(lots, sat_lots) + 0.5 * rate
        return value, (f"{lots} лот(ов) {what} {_period(as_of)}, из них побед: {n_wins} "
                       f"(доля побед со сглаживанием {rate:.0%})")
    value = 0.5 * _sat(lots, sat_lots) + 0.5 * _sat(n_wins, sat_wins)
    return value, f"{lots} лот(ов) {what} {_period(as_of)}, из них побед: {n_wins}"


# Assortment summary thresholds (D-064, research/DATA_DICTIONARY.md): shown only with enough history.
PROFILE_MIN_LOTS = 5
NARROW_TOP_SHARE = 0.8
WIDE_CLASSES, WIDE_TOP_SHARE = 5, 0.5


def _profile_note(cp: Counterparty) -> str:
    """One-line assortment summary from the supplier profile; empty between the thresholds. Not scored."""
    st = cp.stats
    share, classes, lots = st.get("top_class_share"), st.get("okpd2_classes"), st.get("lots_participated")
    if not isinstance(share, (int, float)) or not isinstance(lots, (int, float)) or lots < PROFILE_MIN_LOTS:
        return ""
    top = str(st.get("top_class") or "")
    if share >= NARROW_TOP_SHARE:
        return f"; узкий ассортимент: {share:.0%} лотов — класс {top}" if top.isdigit() else ""
    if isinstance(classes, (int, float)) and classes >= WIDE_CLASSES and share < WIDE_TOP_SHARE:
        return f"; широкий ассортимент: {int(classes)} классов ОКПД2"
    return ""


def _activity(cp: Counterparty, ref: date | None, as_of: date | None) -> tuple[float, str]:
    last = cp.stats.get("last_seen")
    if not isinstance(last, str) or ref is None:
        return 0.0, "Нет данных о последнем участии"
    return _recency(date.fromisoformat(last), ref, as_of, "Последнее участие")


def _recency(last: date, ref: date | None, as_of: date | None, what: str) -> tuple[float, str]:
    if ref is None:
        return 0.0, "Нет данных о последнем участии"
    days = max(0, (ref - last).days)
    anchor = f"до {_fmt(ref)}" if as_of else f"до конца данных ({_fmt(ref)})"
    return 0.5 ** (days / HALF_LIFE_DAYS), f"{what} {_fmt(last)} — {days} дн. {anchor}"


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def _price(cp: Counterparty, budget: float | None, as_of: date | None,
           decades: float = PRICE_DECADES) -> tuple[float, str] | None:
    """Scale fit (#105): the query's НМЦК against the median НМЦК of the supplier's lots before as_of —
    a supplier of 5 000 ₽ lots is a poor fit for a 5 000 000 ₽ lot and vice versa. 1.0 at the same scale,
    0 at a PRICE_DECADES-decade difference. None — not computed (no budget or no history of prices)."""
    median = cp.stats.get("median_start_price")
    if budget is None or budget <= 0 or not isinstance(median, int | float) or median <= 0:
        return None
    ratio = budget / float(median)
    value = max(0.0, 1.0 - abs(math.log10(ratio)) / decades)
    if 0.5 <= ratio <= 2:
        how = "масштаб сопоставим"
    elif ratio > 1:
        how = f"закупка в {ratio:.0f} раз крупнее его типичного лота" if ratio >= 10 else \
            f"закупка в {ratio:.1f} раза крупнее его типичного лота"
    else:
        how = f"закупка в {1 / ratio:.0f} раз мельче его типичного лота" if ratio <= 0.1 else \
            f"закупка в {1 / ratio:.1f} раза мельче его типичного лота"
    return value, (f"НМЦК запроса {_rub(budget)}; медиана НМЦК лотов поставщика {_period(as_of)} — "
                   f"{_rub(float(median))}: {how}")


def _inn_geography(inn: str | None, want: str | None, share: float, why: str) -> tuple[float, str]:
    """No usable KPP: 0 in v1; with signals.inn_region > 0 — region by the INN's first two digits (H-15c)."""
    if share <= 0.0 or not inn or not inn[:2].isdigit():
        return 0.0, f"{why} — регион не определён"
    code = inn[:2]
    where = REGION_IN.get(code, f"в регионе с кодом {code}")
    note = f"{why}; ИНН выдан {where} — регион выдачи ИНН, не обязательно место работы"
    if want is not None and code == want:
        return share, note
    if want is not None and NEIGHBOUR.get(want) == code:
        return share * NEIGHBOUR_VALUE, f"{note}; соседний регион"
    return 0.0, note


def customer_region(query: Query) -> tuple[str, str | None]:
    """The region «География» is measured against (#218): the customer's, else the query region.

    The first two INN digits are the region where the INN was issued, not necessarily where the customer is
    (the customer's KPP would be better, but Query has none). АИС ГЗ customers work in St Petersburg or the
    Leningrad region, so only INN 78 / 47 is taken; another prefix (a federal body registered in Moscow, a
    branch) says nothing about where the goods go — then the query region stays. The second value is the
    note for the explanation, or None.
    """
    inn = (query.customer_inn or "").strip()
    code = inn[:2] if len(inn) in (10, 12) and inn.isdigit() else ""
    if code in REGION_NAME:
        return REGION_NAME[code], f"ИНН заказчика выдан в {REGION_PREP[code]} — сравниваем с этим регионом"
    return query.region, None


def _geography(cp: Counterparty, region: str, inn_share: float = 0.0) -> tuple[float, str]:
    want = REGION_CODE.get(region)
    if not cp.kpp or len(cp.kpp) < 2 or not cp.kpp.strip("0"):  # 000000000 — a stub, not region 00
        why = "КПП нет (ИП или не указан)" if not cp.kpp or len(cp.kpp) < 2 else "КПП не указан (заглушка из нулей)"
        return _inn_geography(cp.inn, want, inn_share, why)
    code = cp.kpp[:2]
    where = REGION_IN.get(code, f"в регионе с кодом {code}")
    if want is None:
        return 0.0, f"Регион запроса «{region}» не сопоставлен с кодом КПП; КПП {cp.kpp} — на учёте {where}"
    if code == want:
        return 1.0, f"КПП {cp.kpp}: на налоговом учёте {where}"
    if NEIGHBOUR.get(want) == code:
        return NEIGHBOUR_VALUE, f"КПП {cp.kpp}: соседний регион — {REGION_NAME[code]}"
    return 0.0, f"КПП {cp.kpp}: на учёте {where}, регион запроса — {region}"


def _reliability(mode: str, h: History | None, prior: Prior, ref: date | None,
                 weights: dict[str, float]) -> ScoreComponent:
    if mode == "off" or ref is None:
        return ScoreComponent(name="reliability", value=0.0, weight=0.0,  # type: ignore[arg-type]
                              explanation=UNAVAILABLE["reliability"])
    value, why = history_reliability(h, prior, ref)
    weight = float(weights.get("reliability", 0.0)) if mode == "on" else 0.0
    return ScoreComponent(name="reliability", value=round(min(1.0, max(0.0, value)), 4),  # type: ignore[arg-type]
                          weight=weight, explanation=why if mode == "on" else why + SHADOW_NOTE)


def _component(name: str, value: float, weights: dict[str, float], explanation: str) -> ScoreComponent:
    return ScoreComponent(name=name, value=round(min(1.0, max(0.0, value)), 4),  # type: ignore[arg-type]
                          weight=float(weights.get(name, 0.0)), explanation=explanation)


# A query built from a notice (`/api/lot` → query_id `lot-<N>`) must not find that very lot: its own positions are an
# exact text match, so its participants would top the list on the answer itself (#249, review #145 F-13).
_OWN_LOT = re.compile(r"lot-(\d+)")
_excluded: ContextVar[int | None] = ContextVar("excluded_lot", default=None)
_predicted: ContextVar[str | None] = ContextVar("predicted_code", default=None)
# What the last search() in this context found, in absolute terms (not normalized by the best candidate):
# the raw material of src/match/confidence.py (#281).
_fit_signals: ContextVar[dict | None] = ContextVar("fit_signals", default=None)


def fit_signals() -> dict | None:
    return _fit_signals.get()


def own_lot(query: Query) -> int | None:
    m = _OWN_LOT.fullmatch(query.query_id)
    return int(m.group(1)) if m else None


def predicted_code() -> str | None:
    """The OKPD2 code the last search() in this context predicted for a query without a code (D-045, P-02);
    None — the query had a code, or nothing matched to predict it from. The block «Новые» searches by its
    class (src/match/pipeline.py passes it to discover(), #394): one classifier for both blocks."""
    return _predicted.get()


def excluded_lot() -> int | None:
    """The lot the last search() in this context removed from its own results (None — nothing removed)."""
    return _excluded.get()


def _drop_own(lots: LotHits, own: int | None) -> LotHits:
    if own is None or not (lots.lot_id == own).any():
        return lots
    _excluded.set(own)
    return lots.without(own)

def _hybrid_hits(idx: LotIndex, text: str, hits: list[tuple[int, float]],
                 cfg: dict) -> tuple[list[tuple[int, float]], str]:
    """pg_hybrid (#39): BM25 hits fused with the nearest names by meaning; BM25 as is when the vectors are unavailable
    (no psycopg, Postgres, table or model) — an error in eval (STRICT_RETRIEVER)."""
    try:
        from src.match import pg_retriever
    except ImportError as e:  # no extra pg (psycopg): the same as unavailable vectors
        if STRICT_RETRIEVER:
            raise
        log.warning("pg_hybrid недоступен, поиск по BM25: %s", e)
        return hits, ""
    try:
        fused, by_meaning = pg_retriever.hybrid(idx, text, hits, TOP_DOCS, model=cfg.get("embedding_model"))
    except pg_retriever.VectorsUnavailable as e:
        if STRICT_RETRIEVER:
            raise
        log.warning("pg_hybrid недоступен, поиск по BM25: %s", e)
        return hits, ""
    if cfg.get("hybrid_scores") == "bm25" and fused:  # #485: RRF order on the BM25 scale the ranker was trained on
        top = hits[0][1] if hits else 1.0
        fused = [(d, s / fused[0][1] * top) for d, s in fused]
    return fused, " по словам и смыслу" if by_meaning else ""


def search(query: Query, k: int = 20, weights: dict[str, float] | None = None,
           config: dict | None = None, features_out: list | None = None) -> list[CounterpartyMatch]:
    """Top-k suppliers from organizers' data for the query; FileNotFoundError if data is not built.

    `config`: the whole configuration of an experiment (eval); by default configs/search.yaml.
    Why the answer is empty (or found by the fallback ladder): fallback.search_note() after the call.
    `features_out`: gets (inn, ranker features) of every scored candidate — training of the ranker (#198).
    """
    fallback.reset_note()
    ranker.forget()  # rank reasons of a previous search must not reach this one (#278)
    _excluded.set(None)
    _predicted.set(None)
    _fit_signals.set(None)
    own = own_lot(query)
    cfg = check_config(config) if config is not None else load_config()  # the product reads the file every call
    cfg = routed(cfg, query.text)  # #485: a short query may go to pg_hybrid
    w = weights or cfg["weights"]
    shares = signal_shares(cfg)  # experiment signals (#22), all 0 by default
    rel_mode = reliability_mode(cfg)  # #72: off | shadow | on
    opts = scoring_options(cfg)  # experiment options (#22), v1 by default
    store = get_store()
    as_of = query.as_of
    idx = get_index(store, as_of)
    lot_date = as_of  # activity is counted up to the lot's date (#589)
    if as_of is not None and idx.as_of is None:
        # get_index gave the full index: the lot is after the data (defense export), its history is all of it.
        # An index built for as_of ends before as_of by construction — its data_end says nothing (#605: leak).
        as_of = None
    if idx.n_docs == 0:
        fallback.set_note(fallback.no_history(as_of))
        return []
    words = tokenize(query.text)
    tokens = fallback.cap_tokens(idx, words)
    hits = idx.retrieve(tokens, TOP_DOCS)
    how = ""
    if cfg["retriever"] == "pg_hybrid":
        hits, how = _hybrid_hits(idx, query.text, hits, cfg)
    codes, bad_codes = fallback.okpd2_codes(query.okpd2)
    lots = _drop_own(idx.lot_hits(hits), own)
    cands = _retrieve(store, idx, lots, as_of) if len(lots) else []
    if not cands:  # nobody by text: the fallback ladder (src/match/fallback.py)
        hits, cands, note = fallback.ladder(store, idx, query, tokens, codes, cfg, bad_codes, own)
        fallback.set_note(note)
        if not cands:
            return []
        lots = _drop_own(idx.lot_hits(hits), own)
    elif bad_codes:
        fallback.set_note(f"ОКПД2 не распознан и не учтён: {', '.join(bad_codes)}")
    predicted = False
    if not codes:
        guess = _predict_okpd2(store, idx, hits, lots, words)
        codes, predicted = ([guess], True) if guess else ([], False)
        _predicted.set(guess)
    if codes:
        cands += candidates.extra(store, {c.inn for c in cands}, codes, query.customer_inn, as_of, cfg, own)  # #195
        _class_history(store, cands, codes, as_of)
    rk = ranker.spec(cfg)
    if rk is not None and rk.get("customer_only") and not query.customer_inn:  # #490: learned only on queries
        rk = None  # with a customer; free text without one goes in the v2 order
    ranked = rk is not None or features_out is not None  # ranker features wanted (#198)
    if not query.customer_inn and not ranked:  # #511: the formula orders free text without a customer
        opts = no_customer_options(cfg)
    # #612: the order of the formula path by options.order_weights; weights other than the config's order by the score
    order_w = opts["order_weights"] if w == cfg["weights"] and not ranked else {}
    w, profile_note = profile_weights(w, cfg, codes, predicted)  # #104: (w, "") when off
    incumbent: dict[str, int] = {}
    if (shares["incumbent"] > 0 or ranked) and query.customer_inn:
        incumbent = customer_lots(store, [c.inn for c in cands], query.customer_inn, as_of)
    fetched = store.built_at()
    # H-105-winrate: the win rate helps АИС ГЗ (one executor per lot) and hurts ЭМ (many offers, one winner)
    wins_mode = opts["experience_wins"]
    if wins_mode == "rate_aisgz":
        wins_mode = "rate" if query.channel == "АИС ГЗ" else "count"
    parties = from_rows(store.supplier_rows([c.inn for c in cands], as_of), fetched)
    top_bm25 = max(c.bm25 for c in cands)
    query_tokens = set(tokens)
    by_class = bool(codes) and opts["experience_by"] == "class"
    pool_lots = max((c.class_lots if by_class else c.n_lots) for c in cands)
    pool_wins = max((c.class_wins if by_class else c.n_wins) for c in cands)
    _fit_signals.set({"top_bm25": top_bm25, "tokens": len(set(tokens)), "retriever": cfg["retriever"],
                      "ladder": any(c.found_by for c in cands)})
    ref = lot_date or idx.data_end
    last_day = as_of - timedelta(days=1) if as_of else idx.data_end  # history is strictly before as_of
    # on: every candidate needs its history (the score depends on it); shadow: nothing here — the weight is 0, the
    # order does not depend on it; shadow_reliability() fills only what the user sees (the page, the export rows)
    history = store.supplier_history([c.inn for c in cands], as_of) if rel_mode == "on" else {}
    prior = reliability_prior(store, as_of) if rel_mode == "on" else Prior(0.0, 0.0)
    out: list[CounterpartyMatch] = []
    bm25_of: list[float] = []  # BM25 of each match, for options.tie_break
    order_of: list[float] = []  # order key of each match under options.order_weights (#612)
    rows: list[list[float]] = []  # ranker features of each match (#198)
    target, target_note = customer_region(query)  # «География» against the customer's region (#218)
    for c in cands:
        cp = parties.get(c.inn)
        if cp is None:
            continue
        sat_lots, sat_wins = opts["sat_lots"], opts["sat_wins"]
        if opts["exp_scale"] == "pool":
            sat_lots, sat_wins = max(sat_lots, pool_lots), max(sat_wins, pool_wins)
        fit, fit_why = _product_fit(c, top_bm25, codes, predicted, how, opts["fit_bm25_share"], opts["name_share"],
                                    query_tokens)
        exp, exp_why = _experience(c, codes, as_of, opts["experience_by"], sat_lots, sat_wins, wins_mode)
        exp_rank = exp if opts["exp_scale"] == "fixed" else _experience(
            c, codes, as_of, opts["experience_by"], opts["sat_lots"], opts["sat_wins"], wins_mode)[0]
        if query.customer_inn:
            exp, exp_why = mix_incumbent(exp, exp_why, incumbent.get(c.inn, 0), shares["incumbent"])
            exp_rank = mix_incumbent(exp_rank, "", incumbent.get(c.inn, 0), shares["incumbent"])[0]
        exp_why += _profile_note(cp)
        if opts["activity_by"] == "similar" and c.last_similar is not None:
            act, act_why = _recency(c.last_similar, ref, lot_date, "Последнее участие в закупке с похожими позициями")
        else:
            act, act_why = _activity(cp, ref, lot_date)
        price = _price(cp, query.budget, as_of, opts["price_decades"]) if opts["price"] == "scale" else None
        geo, geo_why = _geography(cp, target, shares["inn_region"])
        if target_note:  # the comparison is with the customer's region, say so
            geo_why = f"{geo_why.replace('регион запроса', 'регион заказчика')}; {target_note}"
        if profile_note:
            geo_why = f"{geo_why}; {profile_note}"
        wc = _without_price(w) if price is None and opts["price"] == "scale" else w
        no_price = NO_PRICE if opts["price"] == "scale" else UNAVAILABLE["price"]
        comps = [
            _component("product_fit", fit, wc, fit_why),
            _component("experience", exp, wc, exp_why),
            _reliability("off" if rel_mode == "shadow" else rel_mode, history.get(c.inn), prior, last_day, wc),
            _component("price", price[0], wc, price[1]) if price is not None else ScoreComponent(
                name="price", value=0.0, weight=0.0, explanation=no_price),
            _component("activity", act, wc, act_why),
            _component("geography", geo, wc, geo_why),
        ]
        comps.sort(key=lambda x: list(w).index(x.name) if x.name in w else len(w))
        cp = cp.model_copy(update={"evidence": [*cp.evidence, Evidence(
            source="data:items",
            url=f"data:items#{c.best_lot}",
            fetched_at=fetched,
            fact=f"Участвовал в лоте {c.best_lot} от {_fmt(c.best_date)}: «{c.best_text}» (данные организаторов)",
        )]})
        out.append(CounterpartyMatch(counterparty=cp, score=round(sum(x.value * x.weight for x in comps), 4),
                                     components=comps, origin="history"))
        bm25_of.append(c.bm25)
        if ranked or order_w:
            # the ranker was trained on product_fit without the name match, experience with fixed saturation and
            # geography without the INN region:
            # it gets those values, so its order does not move with the display formula (#576);
            # options.order_weights (#612) orders by the same values — the formula before #576
            feats = {x.name: x.value for x in comps}
            feats["experience"] = round(min(1.0, max(0.0, exp_rank)), 4)
            if opts["name_share"] > 0:
                feats["product_fit"] = round(min(1.0, _product_fit(c, top_bm25, codes, predicted, how,
                                                                   opts["fit_bm25_share"])[0]), 4)
            if shares["inn_region"] > 0:
                feats["geography"] = round(_geography(cp, target)[0], 4)
            if order_w:
                order_of.append(round(sum(feats[name] * v for name, v in order_w.items()), 4))
            if ranked:
                rows.append(ranker.feature_row(c, top_bm25, feats,
                                               incumbent.get(c.inn, 0) if query.customer_inn else None,
                                               cp, query, ref, codes, predicted))
    if features_out is not None:
        features_out.extend(zip((m.counterparty.inn for m in out), rows, strict=True))
    if rk is not None:
        try:
            model = ranker.load(rk)
        except (OSError, ImportError, ValueError) as e:  # no model file, no lightgbm, stale features: v2 order
            log.warning("ранжировщик недоступен, порядок по формуле v2: %s", e)
            prev = fallback.search_note()
            why = "ранжировщик недоступен — порядок по формуле v2"
            fallback.set_note(f"{prev}; {why}" if prev else why)
        else:
            return ranker.rerank(model, out, rows, k)
    keys = {id(m): o for m, o in zip(out, order_of, strict=True)} if order_w else {id(m): m.score for m in out}
    if order_w:
        ranker.set_order(out, order_of)
    if opts["tie_break"] == "bm25":
        sims = {id(m): s for m, s in zip(out, bm25_of, strict=True)}
        out.sort(key=lambda m: (-keys[id(m)], -sims[id(m)], m.counterparty.inn or "~"))
    else:
        out.sort(key=lambda m: (-keys[id(m)], m.counterparty.inn or "~"))
    return out[:k]


_priors: dict[tuple, Prior] = {}
_priors_lock = threading.Lock()


def reliability_prior(store: Store, as_of: date | None) -> Prior:
    """The pooled ЭМ win rate and retention of every supplier before as_of (Store.reliability_prior): one query per
    data snapshot and as_of in this process — the same prior for every query, page and export."""
    key = (store.name, repr(store), str(store.built_at()), as_of)
    with _priors_lock:
        if key in _priors:
            return _priors[key]
    prior = store.reliability_prior(as_of)
    with _priors_lock:
        _priors[key] = prior
    return prior


_UNCONFIRMED = re.compile(r"ИНН не подтверждён \((\d+)\)")  # the name src/ingest/load.py gives a bad INN


def _raw_inn(cp: Counterparty) -> str | None:
    """The data's INN of a history match, also when it fails the checksum (counterparty.inn None): then the name
    from src/ingest/load.py carries it («ИНН не подтверждён (<raw>)»)."""
    if cp.inn:
        return cp.inn
    found = _UNCONFIRMED.fullmatch(cp.name)
    return found.group(1) if found else None


def shadow_reliability(matches: list[CounterpartyMatch], as_of: date | None,
                       config: dict | None = None) -> list[CounterpartyMatch]:
    """reliability.mode shadow (#72): the reliability component (weight 0) of the matches the user sees — the page
    of /api/search, the rows of an export — one SQL over their INNs, never over all ≤ CANDIDATES candidates of
    the query. Score and order do not change. Other modes, «Новые» and a stub pass unchanged."""
    if IS_STUB or not matches:
        return matches
    cfg = config if config is not None else load_config()
    if reliability_mode(cfg) != "shadow":
        return matches
    at = [(j, inn) for j, m in enumerate(matches) if m.origin == "history" and (inn := _raw_inn(m.counterparty))]
    if not at:
        return matches
    try:
        store = get_store()
        history = store.supplier_history(sorted({inn for _, inn in at}), as_of)
        prior = reliability_prior(store, as_of)
        last_day = as_of - timedelta(days=1) if as_of else get_index(store, None).data_end
    except Exception:  # noqa: BLE001 — a reference-only component never breaks the page (it stays «Нет данных»)
        log.exception("reliability (shadow): не посчитана для %d строк", len(at))
        return matches
    out = list(matches)
    for j, inn in at:
        rel = _reliability("shadow", history.get(inn), prior, last_day, {})
        out[j] = matches[j].model_copy(
            update={"components": [rel if c.name == "reliability" else c for c in matches[j].components]})
    return out
