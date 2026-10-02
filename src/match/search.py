"""search(query) -> ranked counterparties from organizers' data. Owner: MATCH.

v1 (T-02, research/METHODOLOGY.md, D-017, D-029):
1. BM25 (`bm25s`, src/match/index.py) over lot positions published strictly before `as_of`;
   matched lots -> their participants (Поставщики) = candidates, `origin="history"`.
2. OKPD2 class of the query: `Query.okpd2`, or — when empty — a vote of the positions that
   matched the query themselves (D-045): group XX.XX first, then the code inside it.
3. Components in [0, 1], one explanation line each:
   product_fit — BM25 similarity (normalized by the best candidate) + depth of the OKPD2 match;
   experience  — lots and wins in the query's OKPD2 group (level 4) before as_of;
   activity    — days from the last participation to as_of (or to the end of the data);
   geography   — region of the KPP (78 / 47) against Query.region;
   reliability, price — no data yet: value 0, weight 0, "нет данных" (D-029).
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
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query, ScoreComponent
from src.ingest.load import from_rows
from src.ingest.okpd2 import label
from src.match import candidates, fallback, ranker
from src.match.index import LotIndex, get_index
from src.match.profiles import profile_overrides, profile_weights
from src.match.signals import customer_lots, mix_incumbent, signal_shares
from src.match.store import LotHits, Store, get_store, levels
from src.match.text import tokenize
from src.regions import NEIGHBOUR, NEIGHBOUR_VALUE, REGION_CODE, REGION_IN, REGION_NAME, REGION_PREP

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
SEARCH_CONFIG = ROOT / "configs" / "search.yaml"

IS_STUB = False
RETRIEVERS = ("bm25", "pg_hybrid")  # pg_hybrid: + pgvector, RRF (src/match/pg_retriever.py, #39)
# eval sets True: a configured pg_hybrid that is unavailable is an error, not a silent BM25 measurement.
STRICT_RETRIEVER = False

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


# Scoring options of experiments (#22, `options:` in configs/search.yaml or configs/experiments.yaml).
# The defaults are v1: without the block nothing changes.
OPTIONS: dict[str, tuple] = {
    "fit_bm25_share": (FIT_BM25_SHARE, None),  # share of BM25 in product_fit, [0, 1]
    "experience_by": ("class", ("class", "similar")),  # lots in the OKPD2 group | lots with similar positions
    "tie_break": ("inn", ("inn", "bm25")),  # order of equal scores
    # lots / wins at which experience reaches 1.0 (#196): low caps make active suppliers indistinguishable
    "sat_lots": (SAT_LOTS, None),
    "sat_wins": (SAT_WINS, None),
}


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
    if not 0.0 <= out["fit_bm25_share"] <= 1.0:
        raise ValueError(f"options.fit_bm25_share = {out['fit_bm25_share']}: доля должна быть в [0, 1]")
    for name in ("sat_lots", "sat_wins"):
        if isinstance(out[name], bool) or not isinstance(out[name], int) or out[name] < 1:
            raise ValueError(f"options.{name} = {out[name]!r}: ожидается целое число ≥ 1")
    return out


def check_config(cfg: dict) -> dict:
    if cfg.get("retriever") not in RETRIEVERS:
        raise ValueError(f"retriever {cfg.get('retriever')!r} не поддерживается (есть: {', '.join(RETRIEVERS)})")
    scoring_options(cfg)
    candidates.sizes(cfg)
    ranker.spec(cfg)  # learned ranker (#198): on by default since D-103
    profile_overrides(cfg)  # weights by procurement type (#104), off by default
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
    counted: str = ""  # what n_lots / n_wins count when not lots with similar positions (#195 extra candidates)


def _retrieve(store: Store, idx: LotIndex, lots: LotHits, as_of: date | None) -> list[Candidate]:
    """Participants of the matched lots before as_of, best BM25 first (src/match/store.py: _CANDIDATES)."""
    rows = store.candidates(lots, as_of, CANDIDATES)
    return [Candidate(inn, bm25, n_lots, n_wins, best_lot, idx.text(doc), best_date)
            for inn, bm25, n_lots, n_wins, best_lot, doc, best_date in rows]


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


def _product_fit(c: Candidate, top_bm25: float, codes: list[str], predicted: bool,
                 how: str = "", share: float = FIT_BM25_SHARE) -> tuple[float, str]:
    sim = c.bm25 / top_bm25 if top_bm25 > 0 else 0.0
    best = c.best_text if len(c.best_text) <= 60 else c.best_text[:57] + "…"
    head = c.found_by or f"Похожие{how} позиции в {c.n_lots} лот(ах), лучшее совпадение «{best}» (лот {c.best_lot})"
    if not codes:
        return sim, f"{head}; ОКПД2 запроса не определён"
    src = "ОКПД2 определён по похожим позициям" if predicted else "ОКПД2 запроса"
    value = share * sim + (1 - share) * OKPD_DEPTH[c.okpd_level]
    if c.okpd_level == 0 or c.okpd_code is None:
        return value, f"{head}; {src} {', '.join(codes)} в его истории не встречается"
    return value, (f"{head}; {src}: {OKPD_LEVEL_TEXT[c.okpd_level]} "
                   f"{label(_prefix(c.okpd_code, c.okpd_level))}")


def _experience(c: Candidate, codes: list[str], as_of: date | None, by: str = "class",
                sat_lots: int = SAT_LOTS, sat_wins: int = SAT_WINS) -> tuple[float, str]:
    if codes and by == "class":
        groups = ", ".join(label(g) for g in sorted({x[2] or x[0] for x in map(levels, codes)}))
        lots, wins, what = c.class_lots, c.class_wins, f"по ОКПД2 {groups}"
    else:
        lots, wins, what = c.n_lots, c.n_wins, c.counted or "с похожими позициями"
    value = 0.5 * _sat(lots, sat_lots) + 0.5 * _sat(wins, sat_wins)
    return value, f"{lots} лот(ов) {what} {_period(as_of)}, из них побед: {wins}"


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
    days = max(0, (ref - date.fromisoformat(last)).days)
    anchor = f"до {_fmt(ref)}" if as_of else f"до конца данных ({_fmt(ref)})"
    return 0.5 ** (days / HALF_LIFE_DAYS), f"Последнее участие {_fmt(date.fromisoformat(last))} — {days} дн. {anchor}"


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


def _component(name: str, value: float, weights: dict[str, float], explanation: str) -> ScoreComponent:
    return ScoreComponent(name=name, value=round(min(1.0, max(0.0, value)), 4),  # type: ignore[arg-type]
                          weight=float(weights.get(name, 0.0)), explanation=explanation)


# A query built from a notice (`/api/lot` → query_id `lot-<N>`) must not find that very lot: its own positions are an
# exact text match, so its participants would top the list on the answer itself (#249, review #145 F-13).
_OWN_LOT = re.compile(r"lot-(\d+)")
_excluded: ContextVar[int | None] = ContextVar("excluded_lot", default=None)
# What the last search() in this context found, in absolute terms (not normalized by the best candidate):
# the raw material of src/match/confidence.py (#281).
_fit_signals: ContextVar[dict | None] = ContextVar("fit_signals", default=None)


def fit_signals() -> dict | None:
    return _fit_signals.get()


def own_lot(query: Query) -> int | None:
    m = _OWN_LOT.fullmatch(query.query_id)
    return int(m.group(1)) if m else None


def excluded_lot() -> int | None:
    """The lot the last search() in this context removed from its own results (None — nothing removed)."""
    return _excluded.get()


def _drop_own(lots: LotHits, own: int | None) -> LotHits:
    if own is None or not (lots.lot_id == own).any():
        return lots
    _excluded.set(own)
    return lots.without(own)

def search(query: Query, k: int = 20, weights: dict[str, float] | None = None,
           config: dict | None = None, features_out: list | None = None) -> list[CounterpartyMatch]:
    """Top-k suppliers from organizers' data for the query; FileNotFoundError if data is not built.

    `config`: the whole configuration of an experiment (eval); by default configs/search.yaml.
    Why the answer is empty (or found by the fallback ladder): fallback.search_note() after the call.
    `features_out`: gets (inn, ranker features) of every scored candidate — training of the ranker (#198).
    """
    fallback.reset_note()
    _excluded.set(None)
    _fit_signals.set(None)
    own = own_lot(query)
    cfg = check_config(config) if config is not None else load_config()  # the product reads the file every call
    w = weights or cfg["weights"]
    shares = signal_shares(cfg)  # experiment signals (#22), all 0 by default
    opts = scoring_options(cfg)  # experiment options (#22), v1 by default
    store = get_store()
    as_of = query.as_of
    idx = get_index(store, as_of)
    if idx.n_docs == 0:
        fallback.set_note(fallback.no_history(as_of))
        return []
    words = tokenize(query.text)
    tokens = fallback.cap_tokens(idx, words)
    hits = idx.retrieve(tokens, TOP_DOCS)
    how = ""
    if cfg["retriever"] == "pg_hybrid":
        from src.match import pg_retriever

        try:
            hits, by_meaning = pg_retriever.hybrid(idx, query.text, hits, TOP_DOCS, model=cfg.get("embedding_model"))
            how = " по словам и смыслу" if by_meaning else ""
        except pg_retriever.VectorsUnavailable as e:
            if STRICT_RETRIEVER:
                raise
            log.warning("pg_hybrid недоступен, поиск по BM25: %s", e)
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
    if codes:
        cands += candidates.extra(store, {c.inn for c in cands}, codes, query.customer_inn, as_of, cfg, own)  # #195
        _class_history(store, cands, codes, as_of)
    rk = ranker.spec(cfg)
    ranked = rk is not None or features_out is not None  # ranker features wanted (#198)
    w, profile_note = profile_weights(w, cfg, codes, predicted)  # #104: (w, "") when off
    incumbent: dict[str, int] = {}
    if (shares["incumbent"] > 0 or ranked) and query.customer_inn:
        incumbent = customer_lots(store, [c.inn for c in cands], query.customer_inn, as_of)
    fetched = store.built_at()
    parties = from_rows(store.supplier_rows([c.inn for c in cands], as_of), fetched)
    top_bm25 = max(c.bm25 for c in cands)
    _fit_signals.set({"top_bm25": top_bm25, "tokens": len(set(tokens)), "retriever": cfg["retriever"],
                      "ladder": any(c.found_by for c in cands)})
    ref = as_of or idx.data_end
    out: list[CounterpartyMatch] = []
    bm25_of: list[float] = []  # BM25 of each match, for options.tie_break
    rows: list[list[float]] = []  # ranker features of each match (#198)
    target, target_note = customer_region(query)  # «География» against the customer's region (#218)
    for c in cands:
        cp = parties.get(c.inn)
        if cp is None:
            continue
        fit, fit_why = _product_fit(c, top_bm25, codes, predicted, how, opts["fit_bm25_share"])
        exp, exp_why = _experience(c, codes, as_of, opts["experience_by"], opts["sat_lots"], opts["sat_wins"])
        if query.customer_inn:
            exp, exp_why = mix_incumbent(exp, exp_why, incumbent.get(c.inn, 0), shares["incumbent"])
        exp_why += _profile_note(cp)
        act, act_why = _activity(cp, ref, as_of)
        geo, geo_why = _geography(cp, target, shares["inn_region"])
        if target_note:  # the comparison is with the customer's region, say so
            geo_why = f"{geo_why.replace('регион запроса', 'регион заказчика')}; {target_note}"
        if profile_note:
            geo_why = f"{geo_why}; {profile_note}"
        comps = [
            _component("product_fit", fit, w, fit_why),
            _component("experience", exp, w, exp_why),
            *(ScoreComponent(name=n, value=0.0, weight=0.0, explanation=why)  # type: ignore[arg-type]
              for n, why in UNAVAILABLE.items()),
            _component("activity", act, w, act_why),
            _component("geography", geo, w, geo_why),
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
        if ranked:
            rows.append(ranker.feature_row(c, top_bm25, {x.name: x.value for x in comps},
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
    if opts["tie_break"] == "bm25":
        sims = {id(m): s for m, s in zip(out, bm25_of, strict=True)}
        out.sort(key=lambda m: (-m.score, -sims[id(m)], m.counterparty.inn or "~"))
    else:
        out.sort(key=lambda m: (-m.score, m.counterparty.inn or "~"))
    return out[:k]
