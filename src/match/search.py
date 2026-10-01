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
import tempfile
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import yaml

from src.contracts import Counterparty, CounterpartyMatch, Evidence, Query, ScoreComponent
from src.ingest.load import built_at, counterparties
from src.match.filters import REGION_CODE
from src.match.index import LotIndex, get_index, pq_path, processed_dir
from src.match.signals import customer_lots, mix_incumbent, signal_shares
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
VOTE_DOCS = 100  # best matched position texts that vote for the query's OKPD2 when it has none (D-045)
DOC_CODES = "doc_codes.parquet"  # sidecar of the index: OKPD2 codes of the positions carrying each text
VOTE_LOTS = 20  # fallback: only lot subjects matched — top lots vote, a lot's vote split over its positions
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


def _levels(code: str) -> tuple[str, str | None, str | None, str | None]:
    """(code, level 6, level 4, level 2) as in src/ingest/build.py (items.okpd2_l*)."""
    return (code, code[:8] if len(code) >= 8 else None, code[:5] if len(code) >= 5 else None,
            code[:2] if len(code) >= 2 else None)


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


def _retrieve(con: duckdb.DuckDBPyConnection, idx: LotIndex, hits: list[tuple[int, float]],
              processed: Path, as_of: date | None) -> list[Candidate]:
    con.execute("CREATE OR REPLACE TEMP TABLE h AS SELECT unnest($d) AS doc_id, unnest($s) AS score",
                {"d": [d for d, _ in hits], "s": [s for _, s in hits]})
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE lot AS
        SELECT d.lot_id, max(h.score) AS score, arg_max(h.doc_id, h.score) AS doc_id
        FROM read_parquet({idx.docs_parquet}) d JOIN h USING (doc_id) GROUP BY d.lot_id
        """
    )
    cond = "p.publish_date < $as_of" if as_of else "TRUE"
    rows = con.execute(
        f"""
        SELECT p.supplier_inn, max(lot.score) AS bm25, count(DISTINCT p.lot_id) AS n_lots,
               count(DISTINCT p.lot_id) FILTER (WHERE p.is_winner) AS n_wins,
               arg_max(p.lot_id, (lot.score, p.publish_date, p.lot_id)) AS best_lot,
               arg_max(t.text, (lot.score, p.publish_date, p.lot_id)) AS best_text,
               arg_max(p.publish_date, (lot.score, p.publish_date, p.lot_id)) AS best_date
        FROM read_parquet({pq_path(processed / "participants.parquet")}) p
        JOIN lot USING (lot_id)
        JOIN read_parquet({pq_path(idx.path / "texts.parquet")}) t ON t.doc_id = lot.doc_id
        WHERE {cond}
        GROUP BY p.supplier_inn
        ORDER BY bm25 DESC, n_lots DESC, p.supplier_inn
        LIMIT {CANDIDATES}
        """,
        {"as_of": as_of} if as_of else {},
    ).fetchall()
    return [Candidate(*r) for r in rows]


_codes_lock = threading.Lock()


def _doc_codes(con: duckdb.DuckDBPyConnection, idx: LotIndex, processed: Path) -> str:
    """SQL path of the sidecar (doc_id, code, n): codes of the positions whose name is the document's text.

    Derived from the index's own docs/texts (lots before its as_of) and items of those lots — no
    leak. Built once per index next to it (the index files and its key are untouched); when the
    index directory is read-only (docker mount) — in the temp directory.
    """
    places = (idx.path, Path(tempfile.gettempdir()) / f"rlt-doc-codes-{idx.key}")
    with _codes_lock:
        for base in places:
            if (base / DOC_CODES).exists():
                return pq_path(base / DOC_CODES)
        for n, base in enumerate(places):
            tmp = base / f"{DOC_CODES}.tmp{os.getpid()}"
            try:
                base.mkdir(parents=True, exist_ok=True)
                con.execute(
                    f"""
                    COPY (
                        SELECT d.doc_id, i.okpd2_code AS code, count(*)::INTEGER AS n
                        FROM read_parquet({idx.docs_parquet}) d
                        JOIN read_parquet({pq_path(idx.path / "texts.parquet")}) t USING (doc_id)
                        JOIN read_parquet({pq_path(processed / "items.parquet")}) i
                          ON i.lot_id = d.lot_id AND i.product_name = t.text
                        WHERE i.okpd2_code IS NOT NULL
                        GROUP BY d.doc_id, i.okpd2_code ORDER BY d.doc_id, code
                    ) TO {pq_path(tmp)} (FORMAT parquet)
                    """
                )
                os.replace(tmp, base / DOC_CODES)
                log.info("built %s for index %s", base / DOC_CODES, idx.key)
                return pq_path(base / DOC_CODES)
            except (OSError, duckdb.IOException) as e:
                if n == len(places) - 1:
                    raise
                log.warning("индекс %s не доступен на запись (%s) — коды позиций строятся в %s", idx.path, e, places[1])
    raise AssertionError("unreachable")


def _predict_okpd2(con: duckdb.DuckDBPyConnection, idx: LotIndex, processed: Path) -> str | None:
    """OKPD2 of a query without a code, from the documents that matched it (temp tables h, lot); D-045.

    A matched text votes with the codes of the positions that carry this very text — not with the
    other positions of their lots. One vote per text, its score split over its codes; the
    VOTE_DOCS best texts vote. Texts that are only lot subjects have no code of their own: they
    vote only when no position matched at all — the VOTE_LOTS best lots, a lot's score split over
    its positions. The heaviest group XX.XX wins, then the heaviest code inside it (9 digits
    scatter the votes).
    """
    codes = _doc_codes(con, idx, processed)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE vote AS
        WITH pos AS (SELECT h.doc_id, h.score, c.code, c.n FROM h JOIN read_parquet({codes}) c USING (doc_id)),
        best AS (SELECT DISTINCT doc_id, score FROM pos ORDER BY score DESC, doc_id LIMIT {VOTE_DOCS})
        SELECT pos.code, pos.score * pos.n / sum(pos.n) OVER (PARTITION BY pos.doc_id) AS w
        FROM pos JOIN best USING (doc_id)
        """
    )
    if not con.execute("SELECT 1 FROM vote LIMIT 1").fetchone():
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE vote AS
            WITH top AS (SELECT lot_id, score FROM lot ORDER BY score DESC, lot_id LIMIT {VOTE_LOTS}),
            pos AS (
                SELECT top.lot_id, top.score, i.okpd2_code AS code, count(*) AS n
                FROM read_parquet({pq_path(processed / "items.parquet")}) i JOIN top USING (lot_id)
                WHERE i.okpd2_code IS NOT NULL
                GROUP BY top.lot_id, top.score, i.okpd2_code
            )
            SELECT code, score * n / sum(n) OVER (PARTITION BY lot_id) AS w FROM pos
            """
        )
    row = con.execute(
        """
        WITH v AS (SELECT code, left(code, 5) AS grp, sum(w) AS w FROM vote GROUP BY code)
        SELECT code FROM v
        WHERE grp = (SELECT grp FROM v GROUP BY grp ORDER BY sum(w) DESC, grp LIMIT 1)
        ORDER BY w DESC, code LIMIT 1
        """
    ).fetchone()
    return row[0] if row else None


def _class_history(con: duckdb.DuckDBPyConnection, cands: list[Candidate], codes: list[str],
                   processed: Path, as_of: date | None) -> None:
    """Deepest OKPD2 match over each candidate's history and lots/wins in the query's group."""
    lv = [_levels(c) for c in codes]
    params: dict = {
        "inns": [c.inn for c in cands],
        "q9": [x[0] for x in lv], "q6": [x[1] for x in lv if x[1]],
        "q4": [x[2] for x in lv if x[2]], "q2": [x[3] for x in lv if x[3]],
    }
    cond = "publish_date < $as_of" if as_of else "TRUE"
    if as_of:
        params["as_of"] = as_of
    rows = con.execute(
        f"""
        WITH p AS (
            SELECT lot_id, supplier_inn, is_winner FROM read_parquet({pq_path(processed / "participants.parquet")})
            WHERE supplier_inn IN (SELECT unnest($inns)) AND {cond}
        ),
        m AS (
            SELECT p.supplier_inn, p.lot_id, p.is_winner, i.okpd2_code,
                   CASE WHEN list_contains($q9, i.okpd2_code) THEN 4
                        WHEN list_contains($q6, i.okpd2_l6) THEN 3
                        WHEN list_contains($q4, i.okpd2_l4) THEN 2
                        WHEN list_contains($q2, i.okpd2_l2) THEN 1 ELSE 0 END AS lvl
            FROM p JOIN read_parquet({pq_path(processed / "items.parquet")}) i USING (lot_id)
        )
        SELECT supplier_inn, max(lvl), arg_max(okpd2_code, (lvl, okpd2_code)),
               count(DISTINCT lot_id) FILTER (WHERE lvl >= 2),
               count(DISTINCT lot_id) FILTER (WHERE lvl >= 2 AND is_winner)
        FROM m GROUP BY supplier_inn
        """,
        params,
    ).fetchall()
    by_inn = {r[0]: r[1:] for r in rows}
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
        groups = ", ".join(sorted({x[2] or x[0] for x in map(_levels, codes)}))
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
    processed = processed_dir()
    as_of = query.as_of
    idx = get_index(processed, as_of)
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
    con = duckdb.connect()
    try:
        cands = _retrieve(con, idx, hits, processed, as_of)
        if not cands:
            return []
        codes = list(dict.fromkeys(c.strip() for c in query.okpd2 if c.strip()))
        predicted = False
        if not codes:
            guess = _predict_okpd2(con, idx, processed)
            codes, predicted = ([guess], True) if guess else ([], False)
        if codes:
            _class_history(con, cands, codes, processed, as_of)
        incumbent: dict[str, int] = {}
        if shares["incumbent"] > 0 and query.customer_inn:
            incumbent = customer_lots(con, [c.inn for c in cands], query.customer_inn, processed, as_of)
        parties = counterparties([c.inn for c in cands], as_of, processed, con)
    finally:
        con.close()
    fetched = built_at(processed)
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
