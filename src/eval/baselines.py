"""Baselines of the backtest (research/EVAL.md, T-08) and the "winner new for the customer" slice.

B-pop  — the most frequent suppliers in the query's OKPD2 group (level 4, participations);
B-inc  — incumbents: suppliers that already took part at THIS customer in that group, then B-pop;
B-bm25 — the order of the BM25 retriever alone, without the component scoring of search().

Everything is counted over lots published strictly before `as_of` (the split cutoff in eval):
the same boundary as src/match/search.py, so a baseline never sees the evaluated period.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from functools import cached_property

import duckdb

from src.contracts import Query
from src.match.index import get_index
from src.match.store import Store, get_store
from src.match.text import tokenize

METHODS = ("pop", "inc", "bm25")
COUNTS = ("participations", "wins")
LEVEL_COLUMN = {4: "l4", 2: "l2"}


def okpd_prefix(code: str, level: int) -> str | None:
    """'17.12.14.110' -> '17.12' (level 4) or '17' (level 2); None when the code is shorter."""
    code = code.strip()
    n = {4: 5, 2: 2}[level]
    return code[:n] if len(code) >= n else None


class History:
    """Participations before `as_of`, one row per lot x supplier x OKPD2 group, in memory.

    Read through the store (#293): the same rows from parquet and from Postgres (tests/unit/test_store.py)."""

    def __init__(self, store: Store, as_of: date | None) -> None:
        store.manifest()  # StoreUnavailable with the command that builds the data
        self.store, self.as_of = store, as_of
        self.con = duckdb.connect()
        self.con.register("part_in", store.participations(as_of))
        self.con.register("classes_in", store.lot_classes())
        self.con.execute("CREATE TABLE part AS SELECT * FROM part_in")
        self.con.execute(
            """
            CREATE TABLE lp AS
            SELECT DISTINCT part.lot_id, part.inn, part.customer_inn, part.is_winner, c.l4, c.l2
            FROM part JOIN classes_in c USING (lot_id)
            """
        )
        self.con.unregister("part_in")
        self.con.unregister("classes_in")
        self._cache: dict[tuple, list[str]] = {}

    def top(self, k: int, *, level: int | None = None, groups: Iterable[str] = (),
            customer: str | None = None, count: str = "participations") -> list[str]:
        """Suppliers by the number of distinct lots (ties — by INN); level=None — over all classes."""
        if count not in COUNTS:
            raise ValueError(f"count: {count!r}, ожидается одно из {COUNTS}")
        groups = tuple(sorted(set(groups)))
        key = (k, level, groups, customer, count)
        if key in self._cache:
            return self._cache[key]
        conds, params, table = [], {}, "part"
        if level is not None:
            if not groups:
                return []
            table = "lp"
            conds.append(f"list_contains($groups, {LEVEL_COLUMN[level]})")
            params["groups"] = list(groups)
        if customer is not None:
            conds.append("customer_inn = $customer")
            params["customer"] = customer
        if count == "wins":
            conds.append("is_winner")
        where = " AND ".join(conds) or "TRUE"
        rows = self.con.execute(
            f"SELECT inn, count(DISTINCT lot_id) AS n FROM {table} WHERE {where} "
            f"GROUP BY inn ORDER BY n DESC, inn LIMIT {int(k)}",
            params,
        ).fetchall()
        self._cache[key] = [r[0] for r in rows]
        return self._cache[key]

    def known_pairs(self, customers: Iterable[str]) -> set[tuple[str, str]]:
        """(customer, supplier) pairs with any participation before as_of — any class, any outcome."""
        rows = self.con.execute(
            "SELECT DISTINCT customer_inn, inn FROM part WHERE list_contains($c, customer_inn)",
            {"c": sorted(set(customers))},
        ).fetchall()
        return {(c, s) for c, s in rows}

    @cached_property
    def valid_inns(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT DISTINCT inn FROM part").fetchall()}


_HISTORY: dict[tuple[int, date | None], History] = {}


def history(as_of: date | None, store: Store | None = None) -> History:
    """History for `as_of` from `store` (default: the store of the environment, as search() reads)."""
    store = store or get_store()
    key = (id(store), as_of)
    if key not in _HISTORY or _HISTORY[key].store is not store:
        _HISTORY[key] = History(store, as_of)
    return _HISTORY[key]


def clear_cache() -> None:
    for h in _HISTORY.values():
        h.con.close()
    _HISTORY.clear()


def _extend(out: list[str], more: Iterable[str], k: int) -> list[str]:
    seen = set(out)
    for inn in more:
        if len(out) >= k:
            break
        if inn not in seen:
            out.append(inn)
            seen.add(inn)
    return out


def rank_pop(query: Query, k: int, cfg: dict, h: History, start: list[str] | None = None) -> list[str]:
    """Most frequent in the query's group; short of k — the 2-digit class, then all classes (`fill`)."""
    level, count = int(cfg.get("okpd_level", 4)), cfg.get("count", "participations")
    out = list(start or [])
    tiers = [level, 2, None] if cfg.get("fill", True) else [level]
    for lv in dict.fromkeys(tiers):
        if len(out) >= k:
            break
        groups = [g for g in (okpd_prefix(c, lv) for c in query.okpd2) if g] if lv else []
        # k + len(out): enough rows to fill k after dropping the ones already ranked
        _extend(out, h.top(k + len(out), level=lv, groups=groups, count=count), k)
    return out[:k]


def rank_inc(query: Query, k: int, cfg: dict, h: History) -> list[str]:
    """Suppliers already seen at this customer in the query's group, by frequency; then B-pop."""
    level, count = int(cfg.get("okpd_level", 4)), cfg.get("count", "participations")
    own: list[str] = []
    if query.customer_inn:
        groups = [g for g in (okpd_prefix(c, level) for c in query.okpd2) if g]
        own = h.top(k, level=level, groups=groups, customer=query.customer_inn, count=count)
    if not cfg.get("fill", True):
        return own[:k]
    return rank_pop(query, k, cfg, h, start=own)


def rank_bm25(query: Query, k: int, h: History) -> list[str]:
    """Order of the retriever of search() (best BM25 of the supplier's lots), no component scoring."""
    from src.match import search as search_mod  # read-only use of the retriever (src/match is not ours)

    store = h.store
    idx = get_index(store, query.as_of)
    hits = idx.retrieve(tokenize(query.text), search_mod.TOP_DOCS)
    if not hits:
        return []
    cands = search_mod._retrieve(store, idx, idx.lot_hits(hits), query.as_of)
    return [c.inn for c in cands if c.inn in h.valid_inns][:k]


def rank(query: Query, cfg: dict) -> list[str]:
    """Top-k INNs of the baseline `cfg['method']` for the query; history strictly before query.as_of."""
    method, k = cfg["method"], int(cfg.get("k", 20))
    h = history(query.as_of)
    if method == "pop":
        return rank_pop(query, k, cfg, h)
    if method == "inc":
        return rank_inc(query, k, cfg, h)
    if method == "bm25":
        return rank_bm25(query, k, h)
    raise ValueError(f"baseline method: {method!r}, ожидается одно из {METHODS}")
