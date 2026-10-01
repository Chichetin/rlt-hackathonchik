"""Edge cases of search() and the fallback ladder when the text finds nobody (#113, research/METHODOLOGY.md).

The ladder runs ONLY when the query text gave zero candidates:
  1. vectors — nearest product names by meaning (src/match/pg_retriever.py) when Postgres + pgvector
     answer; otherwise skipped with a log line. Not tried when the configured retriever already used
     the vectors (pg_hybrid), when the query has no words, or in eval with retriever bm25
     (STRICT_RETRIEVER: a bm25 measurement must not depend on a running database).
  2. OKPD2 — suppliers of the lots with the query's code before as_of, most lots first (Store.okpd2_suppliers);
  3. the code is raised a level at a time: code → subcategory XX.XX.XX → group XX.XX → class XX, stop.
Candidates of the ladder have the BM25 part of product_fit = 0; how they were found is the first phrase
of the product_fit explanation. Why the answer is empty is the "note" (log + X-Search-Note in the API).
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from datetime import date

import numpy as np

from src.contracts import Query
from src.match.index import LotIndex
from src.match.store import Store, levels

log = logging.getLogger(__name__)

MAX_TOKENS = 64  # a very long query keeps its rarest (highest IDF) unique words
NOT_BY_NAME = "По названию не найдено"
LEVEL_WORD = {4: "кода", 3: "подкатегории", 2: "группы", 1: "класса"}
# OKPD2: XX, XX.X, XX.XX, XX.XX.X, XX.XX.XX, XX.XX.XX.X{1,3}
_OKPD2 = re.compile(r"\d{2}(?:\.\d|\.\d{2}(?:\.\d|\.\d{2}(?:\.\d{1,3})?)?)?")
_note: ContextVar[str | None] = ContextVar("search_note", default=None)


def reset_note() -> None:
    _note.set(None)


def set_note(why: str) -> None:
    log.info("search: %s", why)
    _note.set(why)


def search_note() -> str | None:
    """Why the last search() in this context is empty or how it was found (None — nothing to say)."""
    return _note.get()


def _fmt(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def okpd2_codes(raw: list[str]) -> tuple[list[str], list[str]]:
    """(valid codes in order, unrecognized inputs): «17.12.» → 17.12, «1712» → 17.12, «abc» → rejected."""
    valid: list[str] = []
    bad: list[str] = []
    for r in raw:
        s = re.sub(r"\s+", "", r).replace(",", ".").strip(".")
        if not s:
            continue
        if s.isdigit() and 2 <= len(s) <= 9:
            s = ".".join(p for p in (s[:2], s[2:4], s[4:6], s[6:]) if p)
        if _OKPD2.fullmatch(s):
            valid.append(s)
        else:
            bad.append(r.strip())
    if bad:
        log.info("ОКПД2 не распознан и отброшен: %s", ", ".join(bad))
    return list(dict.fromkeys(valid)), bad


def cap_tokens(idx: LotIndex, tokens: list[str], cap: int = MAX_TOKENS) -> list[str]:
    """More than `cap` unique words: the `cap` rarest by document frequency (bytewise on ties)."""
    uniq = list(dict.fromkeys(tokens))
    if len(uniq) <= cap:
        return tokens
    indptr = np.asarray(idx.retriever.scores["indptr"]) if idx.n_docs else np.zeros(1, np.int64)
    vocab = idx.retriever.vocab_dict if idx.n_docs else {}

    def df(t: str) -> int:
        i = vocab[t]
        return int(indptr[i + 1] - indptr[i]) if i + 1 < len(indptr) else 0

    keep = sorted((t for t in uniq if t in idx.vocab), key=lambda t: (df(t), t.encode()))[:cap]
    log.info("длинный запрос: %d уникальных слов, оставлено %d самых редких, отброшено %d",
             len(uniq), len(keep), len(uniq) - len(keep))
    return keep


def _short(text: str) -> str:
    return text if len(text) <= 60 else text[:57] + "…"


def no_history(as_of: date | None) -> str:
    if as_of:
        return f"нет истории до {_fmt(as_of)}: в данных нет лотов с участниками раньше этой даты"
    return "в данных нет лотов с участниками"


def _vector_hits(idx: LotIndex, text: str) -> tuple[list[tuple[int, float]], str]:
    """Ladder step 1: (hits, why skipped / empty)."""
    from src.match import search as s

    try:
        from src.match import pg_retriever

        docs = pg_retriever.vector_docs(idx, text)
    except (ImportError, RuntimeError) as e:  # VectorsUnavailable is a RuntimeError
        log.info("лестница: векторная ступень пропущена — %s", e)
        return [], "поиск по смыслу недоступен"
    if not docs:
        return [], "по смыслу похожих названий нет"
    return pg_retriever.rrf([], docs, s.TOP_DOCS), ""


def _okpd2_rungs(codes: list[str]) -> list[tuple[int, list[str]]]:
    """(level, prefixes) from the codes themselves up to the class: 4 code, 3 XX.XX.XX, 2 XX.XX, 1 XX.

    A code that is itself a level (17.12 — a group) starts at that level: the exact-code step would
    find only the positions coded «17.12», not the group."""
    out = []
    for level in (4, 3, 2, 1):
        prefixes = sorted({p for c in codes if (p := levels(c)[4 - level]) and (level < 4 or len(c) not in (2, 5, 8))},
                          key=str.encode)
        if prefixes:
            out.append((level, prefixes))
    return out


def ladder(store: Store, idx: LotIndex, query: Query, tokens: list[str], codes: list[str], cfg: dict,
           bad_codes: list[str], own: int | None = None) -> tuple[list[tuple[int, float]], list, str]:
    """Zero candidates by text → (hits, candidates, note); no candidates → note says why.
    `own` — the notice the query was built from: no rung may find it again (#249, F-13).

    Vector hits come back as `hits` (search() then predicts the class and scores them as usual, BM25
    part 0); OKPD2 candidates come with `hits = []`.
    """
    from src.match import search as s

    as_of = query.as_of
    why = ["в запросе нет слов для поиска" if not tokens else "по названию ничего не найдено"]
    if bad_codes:
        why.append(f"ОКПД2 не распознан: {', '.join(bad_codes)}")
    if not tokens:
        log.info("лестница: в запросе нет слов — векторная ступень не нужна")
    elif cfg.get("retriever") == "pg_hybrid":
        log.info("лестница: векторы уже использованы ретривером pg_hybrid")
    elif s.STRICT_RETRIEVER:
        log.info("лестница: векторная ступень не используется в eval с retriever=bm25")
    else:
        hits, skipped = _vector_hits(idx, query.text)
        lots = s._drop_own(idx.lot_hits(hits), own)
        cands = s._retrieve(store, idx, lots, as_of) if len(lots) else []
        for c in cands:
            c.found_by = (f"{NOT_BY_NAME}; найден по смыслу названия (векторный поиск): похожие позиции в "
                          f"{c.n_lots} лот(ах), лучшее совпадение «{_short(c.best_text)}» (лот {c.best_lot})")
            c.bm25 = 0.0
        if cands:
            return hits, cands, f"{NOT_BY_NAME.lower()}; показаны поставщики, найденные по смыслу названия"
        why.append(skipped or "по смыслу ничего не найдено")
    if not codes:
        why.append("ОКПД2 не задан — уточните название или укажите код")
        return [], [], "; ".join(why)
    for level, prefixes in _okpd2_rungs(codes):
        rows = store.okpd2_suppliers(level, prefixes, as_of, s.CANDIDATES, -1 if own is None else own)
        if not rows:
            continue
        what = f"{LEVEL_WORD[level]} ОКПД2 {', '.join(prefixes)}"
        cands = []
        for inn, n_lots, n_wins, lot_id, text, day in rows:
            c = s.Candidate(inn, 0.0, int(n_lots), int(n_wins), int(lot_id), text or "", day)
            c.found_by = (f"{NOT_BY_NAME}; поставщик {what} по частоте участия: {c.n_lots} лот(ов), "
                          f"последний «{_short(c.best_text)}» (лот {c.best_lot})")
            cands.append(c)
        return [], cands, f"{NOT_BY_NAME.lower()}; показаны поставщики {what} по частоте участия"
    period = f" до {_fmt(as_of)}" if as_of else ""
    why.append(f"по ОКПД2 {', '.join(codes)} нет поставщиков{period} даже на уровне класса")
    return [], [], "; ".join(why)
