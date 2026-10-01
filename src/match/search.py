"""search(query) -> ranked counterparties from organizers' data. Owner: MATCH.

v1 (T-02, research/METHODOLOGY.md, D-017, D-029):
1. BM25 (`bm25s`, src/match/index.py) over lot positions published strictly before `as_of`;
   matched lots -> their participants (Поставщики) = candidates, `origin="history"`.
2. OKPD2 class of the query: `Query.okpd2`, or — when empty — the code with the largest BM25
   weight among the positions of the top matched lots.
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
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query, ScoreComponent
from src.ingest.load import from_rows
from src.match.filters import REGION_CODE
from src.match.index import LotIndex, get_index
from src.match.signals import customer_lots, mix_incumbent, signal_shares
from src.match.store import LotHits, Store, get_store, levels
from src.match.text import tokenize

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
SEARCH_CONFIG = ROOT / "configs" / "search.yaml"

IS_STUB = False
RETRIEVERS = ("bm25", "pg_hybrid")  # pg_hybrid: + pgvector, RRF (src/match/pg_retriever.py, #39)
# eval sets True: a configured pg_hybrid that is unavailable is an error, not a silent BM25 measurement.
STRICT_RETRIEVER = False

TOP_DOCS = 500  # BM25 documents (distinct position texts) taken per query
CANDIDATES = 200  # suppliers scored per query (by best BM25 of their lots)
VOTE_LOTS = 20  # top lots that vote for the query's OKPD2 when the query has none
FIT_BM25_SHARE = 0.6  # product_fit = 0.6 · BM25 + 0.4 · OKPD2 depth (BM25 only without a class)
OKPD_DEPTH = {4: 1.0, 3: 0.75, 2: 0.5, 1: 0.25, 0: 0.0}
OKPD_LEVEL_TEXT = {4: "совпадает код", 3: "совпадает до подкатегории", 2: "совпадает до группы",
                   1: "совпадает только класс"}
SAT_LOTS, SAT_WINS = 20, 10  # experience saturates at 20 lots / 10 wins in the group
HALF_LIFE_DAYS = 90  # activity halves every 90 days without participation
REGION_NAME = {v: k for k, v in REGION_CODE.items()}
REGION_IN = {"78": "в Санкт-Петербурге", "47": "в Ленинградской области"}
NEIGHBOUR = {"78": "47", "47": "78"}
NEIGHBOUR_VALUE = 0.8
# Components without data (D-029): shown with value 0 and weight 0, not invented.
UNAVAILABLE = {
    "reliability": "Нет данных: статус, РНП и расторжения ещё не подключены — в score не входит",
    "price": "Нет данных: в выгрузке нет цены контракта, только НМЦК — в score не входит",
}


def load_config(path: Path = SEARCH_CONFIG) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if cfg.get("retriever") not in RETRIEVERS:
        raise ValueError(f"retriever {cfg.get('retriever')!r} не поддерживается (есть: {', '.join(RETRIEVERS)})")
    return cfg


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


def _retrieve(store: Store, idx: LotIndex, lots: LotHits, as_of: date | None) -> list[Candidate]:
    """Participants of the matched lots before as_of, best BM25 first (src/match/store.py: _CANDIDATES)."""
    rows = store.candidates(lots, as_of, CANDIDATES)
    return [Candidate(inn, bm25, n_lots, n_wins, best_lot, idx.text(doc), best_date)
            for inn, bm25, n_lots, n_wins, best_lot, doc, best_date in rows]


def _predict_okpd2(store: Store, lots: LotHits) -> str | None:
    """The OKPD2 code with the largest summed BM25 score over the positions of the top lots."""
    score = dict(lots.top(VOTE_LOTS))
    votes: dict[str, float] = defaultdict(float)
    for lot_id, code in store.lot_codes(list(score)):  # one row per position: a code votes per position
        votes[code] += score[lot_id]
    return min(votes, key=lambda c: (-votes[c], c.encode())) if votes else None


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
                 how: str = "") -> tuple[float, str]:
    sim = c.bm25 / top_bm25 if top_bm25 > 0 else 0.0
    best = c.best_text if len(c.best_text) <= 60 else c.best_text[:57] + "…"
    head = f"Похожие{how} позиции в {c.n_lots} лот(ах), лучшее совпадение «{best}» (лот {c.best_lot})"
    if not codes:
        return sim, f"{head}; ОКПД2 запроса не определён"
    src = "ОКПД2 определён по похожим позициям" if predicted else "ОКПД2 запроса"
    value = FIT_BM25_SHARE * sim + (1 - FIT_BM25_SHARE) * OKPD_DEPTH[c.okpd_level]
    if c.okpd_level == 0 or c.okpd_code is None:
        return value, f"{head}; {src} {', '.join(codes)} в его истории не встречается"
    return value, (f"{head}; {src}: {OKPD_LEVEL_TEXT[c.okpd_level]} "
                   f"{_prefix(c.okpd_code, c.okpd_level)}")


def _experience(c: Candidate, codes: list[str], as_of: date | None) -> tuple[float, str]:
    if codes:
        groups = ", ".join(sorted({x[2] or x[0] for x in map(levels, codes)}))
        lots, wins, what = c.class_lots, c.class_wins, f"по ОКПД2 {groups}"
    else:
        lots, wins, what = c.n_lots, c.n_wins, "с похожими позициями"
    value = 0.5 * _sat(lots, SAT_LOTS) + 0.5 * _sat(wins, SAT_WINS)
    return value, f"{lots} лот(ов) {what} {_period(as_of)}, из них побед: {wins}"


def _activity(cp: Counterparty, ref: date | None, as_of: date | None) -> tuple[float, str]:
    last = cp.stats.get("last_seen")
    if not isinstance(last, str) or ref is None:
        return 0.0, "Нет данных о последнем участии"
    days = max(0, (ref - date.fromisoformat(last)).days)
    anchor = f"до {_fmt(ref)}" if as_of else f"до конца данных ({_fmt(ref)})"
    return 0.5 ** (days / HALF_LIFE_DAYS), f"Последнее участие {_fmt(date.fromisoformat(last))} — {days} дн. {anchor}"


def _geography(cp: Counterparty, region: str) -> tuple[float, str]:
    want = REGION_CODE.get(region)
    if not cp.kpp or len(cp.kpp) < 2:
        return 0.0, "КПП нет (ИП или не указан) — регион не определён"
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


def search(query: Query, k: int = 20, weights: dict[str, float] | None = None) -> list[CounterpartyMatch]:
    """Top-k suppliers from organizers' data for the query; FileNotFoundError if data is not built."""
    cfg = load_config()  # every call: eval passes weights, the retriever still comes from the config
    w = weights or cfg["weights"]
    shares = signal_shares(cfg)  # experiment signals (#22), all 0 by default
    store = get_store()
    as_of = query.as_of
    idx = get_index(store, as_of)
    hits = idx.retrieve(tokenize(query.text), TOP_DOCS)
    how = ""
    if cfg["retriever"] == "pg_hybrid":
        from src.match import pg_retriever

        try:
            hits, by_meaning = pg_retriever.hybrid(idx, query.text, hits, TOP_DOCS)
            how = " по словам и смыслу" if by_meaning else ""
        except pg_retriever.VectorsUnavailable as e:
            if STRICT_RETRIEVER:
                raise
            log.warning("pg_hybrid недоступен, поиск по BM25: %s", e)
    if not hits:
        return []
    lots = idx.lot_hits(hits)
    cands = _retrieve(store, idx, lots, as_of)
    if not cands:
        return []
    codes = list(dict.fromkeys(c.strip() for c in query.okpd2 if c.strip()))
    predicted = False
    if not codes:
        guess = _predict_okpd2(store, lots)
        codes, predicted = ([guess], True) if guess else ([], False)
    if codes:
        _class_history(store, cands, codes, as_of)
    incumbent: dict[str, int] = {}
    if shares["incumbent"] > 0 and query.customer_inn:
        incumbent = customer_lots(store, [c.inn for c in cands], query.customer_inn, as_of)
    fetched = store.built_at()
    parties = from_rows(store.supplier_rows([c.inn for c in cands], as_of), fetched)
    top_bm25 = max(c.bm25 for c in cands)
    ref = as_of or idx.data_end
    out: list[CounterpartyMatch] = []
    for c in cands:
        cp = parties.get(c.inn)
        if cp is None:
            continue
        fit, fit_why = _product_fit(c, top_bm25, codes, predicted, how)
        exp, exp_why = _experience(c, codes, as_of)
        if query.customer_inn:
            exp, exp_why = mix_incumbent(exp, exp_why, incumbent.get(c.inn, 0), shares["incumbent"])
        act, act_why = _activity(cp, ref, as_of)
        geo, geo_why = _geography(cp, query.region)
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
    out.sort(key=lambda m: (-m.score, m.counterparty.inn or "~"))
    return out[:k]
