"""Where search() and /api/lot read organizers' data: Postgres (D-054) or ingest parquet. Owner: MATCH.

Two backends with one interface and the same SQL, row for row (tests/unit/test_store.py):
  PgStore      — schema `data` loaded by `make db-load` (src/db/load.py); the main path (`make up`).
  ParquetStore — data/processed/*.parquet through DuckDB; the fallback of `make demo` without Docker (Q-26).

DATA_STORE picks one: `postgres` (an error when the schema is not loaded), `parquet`, or `auto`
(default) — Postgres when DATABASE_URL is postgres and `data.manifest` exists, else parquet with a
warning. `/api/health` and eval results name the store that answered.

Ordering is part of the result (ties, the BM25 doc numbering): every ORDER BY on text is bytewise
(DuckDB's default, `COLLATE "C"` in Postgres), so both backends give identical rows.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

import duckdb
import numpy as np
import pyarrow as pa

from src.match.reliability import History
from src.settings import get_settings

log = logging.getLogger(__name__)
REQUIRED = ("manifest.json", "notices.parquet", "participants.parquet", "items.parquet", "suppliers.parquet")


class StoreUnavailable(FileNotFoundError):
    """Data is not loaded (FileNotFoundError: the API answers 503 with the command that builds it)."""


@dataclass
class LotHits:
    """Lots matched by a query: best BM25 score per lot and the document (doc_id) that gave it."""

    lot_id: np.ndarray  # int64
    score: np.ndarray  # float64
    doc_id: np.ndarray  # int32

    def __len__(self) -> int:
        return len(self.lot_id)

    def top(self, n: int) -> list[tuple[int, float]]:
        """n best lots by (score desc, lot_id asc)."""
        order = np.lexsort((self.lot_id, -self.score))[:n]
        return [(int(self.lot_id[i]), float(self.score[i])) for i in order]

    def without(self, lot_id: int) -> LotHits:
        keep = self.lot_id != lot_id
        return LotHits(self.lot_id[keep], self.score[keep], self.doc_id[keep])


class Store(Protocol):
    name: str

    def manifest(self) -> dict: ...
    def built_at(self) -> datetime: ...
    def index_rows(self, as_of: date | None) -> tuple[list[tuple[str, list[int]]], date | None]: ...
    def candidates(self, lots: LotHits, as_of: date | None, limit: int) -> list[tuple]: ...
    def lot_codes(self, lot_ids: list[int]) -> list[tuple[int, str]]: ...
    def text_codes(self, as_of: date | None) -> list[tuple[str, str, int]]: ...
    def class_history(self, inns: list[str], codes: list[str], as_of: date | None) -> dict[str, tuple]: ...
    def customer_lots(self, inns: list[str], customer_inn: str, as_of: date | None) -> dict[str, int]: ...
    def supplier_history(self, inns: list[str], as_of: date | None) -> dict[str, History]: ...
    def supplier_rows(self, inns: list[str], as_of: date | None) -> list[dict]: ...
    def okpd2_suppliers(self, level: int, codes: list[str], as_of: date | None, limit: int,
                        exclude_lot: int = -1) -> list[tuple]: ...
    def customer_suppliers(self, customer_inn: str, groups: list[str], as_of: date | None, limit: int,
                           exclude_lot: int = -1) -> list[tuple]: ...
    def lot(self, ident: str) -> tuple | None: ...
    def lot_okpd2(self, lot_id: int) -> list[str]: ...
    def lot_names(self, lot_id: int) -> list[str]: ...
    def top_suppliers(self, limit: int) -> list[str]: ...
    def participations(self, as_of: date | None) -> pa.Table: ...
    def lot_classes(self) -> pa.Table: ...


def levels(code: str) -> tuple[str, str | None, str | None, str | None]:
    """(code, level 6, level 4, level 2) as in src/ingest/build.py (items.okpd2_l*)."""
    return (code, code[:8] if len(code) >= 8 else None, code[:5] if len(code) >= 5 else None,
            code[:2] if len(code) >= 2 else None)


def class_params(codes: list[str]) -> dict[str, list[str]]:
    lv = [levels(c) for c in codes]
    return {"q9": [x[0] for x in lv], "q6": [x[1] for x in lv if x[1]],
            "q4": [x[2] for x in lv if x[2]], "q2": [x[3] for x in lv if x[3]]}


# --- SQL shared by both backends. {t[...]} — table, {c} — bytewise collation, {lots} — matched lots,
# parameters in psycopg style (%(name)s); DuckDB gets them rewritten to $name.

_INDEX_ROWS = """
WITH lots AS (SELECT DISTINCT lot_id FROM {t[participants]} WHERE {cond}),
texts AS (
    SELECT i.lot_id, i.product_name AS t FROM {t[items]} i JOIN lots USING (lot_id) WHERE i.product_name IS NOT NULL
    UNION
    SELECT n.lot_id, n.subject AS t FROM {t[notices]} n JOIN lots USING (lot_id) WHERE n.subject IS NOT NULL
)
SELECT t, array_agg(DISTINCT lot_id ORDER BY lot_id) FROM texts GROUP BY t ORDER BY t{c}
"""

# Per supplier: best BM25 of its lots, matched lots and wins; the best lot = max (score, date, lot_id);
# the date of its latest matched lot (#105, options.activity_by: similar).
_CANDIDATES = """
WITH j AS (
    SELECT p.supplier_inn, p.lot_id, p.is_winner, p.publish_date, l.score, l.doc_id
    FROM {t[participants]} p JOIN {lots} USING (lot_id) WHERE {cond}
),
agg AS (
    SELECT supplier_inn, max(score) AS bm25, count(DISTINCT lot_id) AS n_lots,
           count(DISTINCT lot_id) FILTER (WHERE is_winner) AS n_wins, max(publish_date) AS last_similar
    FROM j GROUP BY supplier_inn
    ORDER BY bm25 DESC, n_lots DESC, supplier_inn{c} LIMIT %(limit)s
),
best AS (
    SELECT DISTINCT ON (j.supplier_inn) j.supplier_inn, j.lot_id, j.doc_id, j.publish_date
    FROM j JOIN agg USING (supplier_inn)
    ORDER BY j.supplier_inn, j.score DESC, j.publish_date DESC NULLS LAST, j.lot_id DESC
)
SELECT agg.supplier_inn, agg.bm25, agg.n_lots, agg.n_wins, best.lot_id, best.doc_id, best.publish_date,
       agg.last_similar
FROM agg JOIN best USING (supplier_inn)
ORDER BY agg.bm25 DESC, agg.n_lots DESC, agg.supplier_inn{c}
"""

_LOT_CODES = """
SELECT lot_id, okpd2_code FROM {t[items]}
WHERE lot_id IN (SELECT unnest(%(lots)s{bigint_arr})) AND okpd2_code IS NOT NULL
ORDER BY lot_id, okpd2_code{c}
"""

# OKPD2 codes of the positions carrying each text, lots with participants before as_of (D-045):
# (product_name, code, positions). Matched to the index documents by text in search.py.
_TEXT_CODES = """
WITH lots AS (SELECT DISTINCT lot_id FROM {t[participants]} WHERE {cond})
SELECT i.product_name, i.okpd2_code, count(*) FROM {t[items]} i JOIN lots USING (lot_id)
WHERE i.product_name IS NOT NULL AND i.okpd2_code IS NOT NULL
GROUP BY i.product_name, i.okpd2_code
"""

# Deepest OKPD2 match over each candidate's history (4 code … 1 class) and lots/wins in the group (level ≥ 2).
_CLASS_HISTORY = """
WITH p AS (
    SELECT lot_id, supplier_inn, is_winner FROM {t[participants]}
    WHERE supplier_inn IN (SELECT unnest(%(inns)s{text_arr})) AND {cond}
),
m AS (
    SELECT p.supplier_inn, p.lot_id, p.is_winner, i.okpd2_code,
           CASE WHEN i.okpd2_code IN (SELECT unnest(%(q9)s{text_arr})) THEN 4
                WHEN i.okpd2_l6 IN (SELECT unnest(%(q6)s{text_arr})) THEN 3
                WHEN i.okpd2_l4 IN (SELECT unnest(%(q4)s{text_arr})) THEN 2
                WHEN i.okpd2_l2 IN (SELECT unnest(%(q2)s{text_arr})) THEN 1 ELSE 0 END AS lvl
    FROM p JOIN {t[items]} i USING (lot_id)
),
agg AS (
    SELECT supplier_inn, max(lvl) AS lvl,
           count(DISTINCT lot_id) FILTER (WHERE lvl >= 2) AS class_lots,
           count(DISTINCT lot_id) FILTER (WHERE lvl >= 2 AND is_winner) AS class_wins
    FROM m GROUP BY supplier_inn
),
code AS (
    SELECT DISTINCT ON (supplier_inn) supplier_inn, okpd2_code FROM m
    ORDER BY supplier_inn, lvl DESC, okpd2_code{c} DESC NULLS LAST
)
SELECT agg.supplier_inn, agg.lvl, code.okpd2_code, agg.class_lots, agg.class_wins
FROM agg JOIN code USING (supplier_inn)
"""

# The same answer for Postgres from `supplier_items` (src/db/load.py, D-135): the candidates' own (lot, code) rows, no
# join of every lot of every candidate to `items`. Per lot: the deepest level, `<level><best code>` (bytewise max = the
# deepest level, then the greatest code; NULL codes are skipped as NULLS LAST did) and whether a participant won;
# then per supplier: the deepest level and its code, lots and wins in the group (level >= 2). Suppliers without
# positions get no row, as with the inner join above.
_CLASS_HISTORY_PG = """
WITH li AS (
    SELECT supplier_inn, lot_id, bool_or(is_winner) AS win, max(lvl) AS lvl, max((lvl::text || okpd2_code){c}) AS lc
    FROM (
        SELECT supplier_inn, lot_id, is_winner, okpd2_code,
               CASE WHEN okpd2_code = ANY(%(q9)s{text_arr}) THEN 4
                    WHEN okpd2_l6 = ANY(%(q6)s{text_arr}) THEN 3
                    WHEN okpd2_l4 = ANY(%(q4)s{text_arr}) THEN 2
                    WHEN okpd2_l2 = ANY(%(q2)s{text_arr}) THEN 1 ELSE 0 END AS lvl
        FROM {t[supplier_items]}
        WHERE supplier_inn = ANY(%(inns)s{text_arr}) AND {cond}
    ) m GROUP BY supplier_inn, lot_id
)
SELECT supplier_inn, max(lvl), substr(max(lc{c}), 2),
       count(*) FILTER (WHERE lvl >= 2), count(*) FILTER (WHERE lvl >= 2 AND win)
FROM li GROUP BY supplier_inn
"""

# Lots of one customer per supplier before as_of: the incumbent signal (src/match/signals.py, #22).
_CUSTOMER_LOTS = """
SELECT supplier_inn, count(DISTINCT lot_id) FROM {t[participants]}
WHERE supplier_inn IN (SELECT unnest(%(inns)s{text_arr})) AND customer_inn = %(cust)s AND {cond}
GROUP BY supplier_inn
"""

# Reliability aggregates per supplier before as_of (src/match/reliability.py, #72): lots, wins, distinct
# customers, customers with >= 1 / >= 2 winning lots, active months, first lot.
_SUPPLIER_HISTORY = """
WITH p AS (
    SELECT lot_id, supplier_inn, is_winner, customer_inn, publish_date FROM {t[participants]}
    WHERE supplier_inn IN (SELECT unnest(%(inns)s{text_arr})) AND {cond}
),
pc AS (
    SELECT supplier_inn, count(DISTINCT lot_id) FILTER (WHERE is_winner) AS w
    FROM p WHERE customer_inn IS NOT NULL GROUP BY supplier_inn, customer_inn
),
c AS (
    SELECT supplier_inn, count(*) FILTER (WHERE w >= 1) AS won_customers,
           count(*) FILTER (WHERE w >= 2) AS repeat_customers
    FROM pc GROUP BY supplier_inn
)
SELECT p.supplier_inn, count(DISTINCT p.lot_id), count(DISTINCT p.lot_id) FILTER (WHERE p.is_winner),
       count(DISTINCT p.customer_inn), coalesce(max(c.won_customers), 0), coalesce(max(c.repeat_customers), 0),
       count(DISTINCT date_trunc('month', p.publish_date)), min(p.publish_date)
FROM p LEFT JOIN c USING (supplier_inn)
GROUP BY p.supplier_inn
ORDER BY p.supplier_inn{c}
"""


def _histories(rows: list[tuple]) -> dict[str, History]:
    return {r[0]: History(*(int(x) for x in r[1:7]), first_seen=r[7]) for r in rows}


# Supplier aggregates (as of the cutoff for eval) + the latest lot: the INN Evidence of src/ingest/load.py.
_SUPPLIERS = """
WITH last AS (
    SELECT DISTINCT ON (supplier_inn) supplier_inn, lot_id, channel FROM {t[participants]}
    WHERE supplier_inn IN (SELECT unnest(%(inns)s{text_arr})) AND {cond}
    ORDER BY supplier_inn, publish_date DESC NULLS LAST, lot_id DESC
)
SELECT s.*, last.lot_id AS last_lot, last.channel AS last_channel
FROM {t[suppliers]} s LEFT JOIN last ON last.supplier_inn = s.inn
WHERE s.inn IN (SELECT unnest(%(inns)s{text_arr}))
ORDER BY s.inn{c}
"""

# Fallback ladder (src/match/fallback.py): suppliers of the lots with an OKPD2 prefix at one level
# ({col}: okpd2_code | okpd2_l6 | okpd2_l4 | okpd2_l2), most lots first, before as_of; the latest such lot
# and one of its matching position names (bytewise min) are the evidence.
_OKPD2_SUPPLIERS = """
WITH m AS (
    SELECT DISTINCT lot_id, product_name FROM {t[items]} WHERE {col} IN (SELECT unnest(%(codes)s{text_arr}))
      AND lot_id <> %(exclude)s  -- the notice the query was built from (#249, F-13); -1 — none
),
j AS (
    SELECT supplier_inn, lot_id, is_winner, publish_date FROM {t[participants]}
    WHERE lot_id IN (SELECT lot_id FROM m) AND {cond}
),
agg AS (
    SELECT supplier_inn, count(DISTINCT lot_id) AS n_lots, count(DISTINCT lot_id) FILTER (WHERE is_winner) AS n_wins
    FROM j GROUP BY supplier_inn
    ORDER BY n_lots DESC, n_wins DESC, supplier_inn{c} LIMIT %(limit)s
),
best AS (
    SELECT DISTINCT ON (j.supplier_inn) j.supplier_inn, j.lot_id, j.publish_date
    FROM j JOIN agg USING (supplier_inn)
    ORDER BY j.supplier_inn, j.publish_date DESC NULLS LAST, j.lot_id DESC
),
name AS (SELECT lot_id, min(product_name{c}) AS text FROM m WHERE lot_id IN (SELECT lot_id FROM best) GROUP BY lot_id)
SELECT agg.supplier_inn, agg.n_lots, agg.n_wins, best.lot_id, name.text, best.publish_date
FROM agg JOIN best USING (supplier_inn) LEFT JOIN name USING (lot_id)
ORDER BY agg.n_lots DESC, agg.n_wins DESC, agg.supplier_inn{c}
"""
OKPD2_COLUMN = {4: "okpd2_code", 3: "okpd2_l6", 2: "okpd2_l4", 1: "okpd2_l2"}

# Suppliers of one customer's lots in the query's OKPD2 groups before as_of, most lots first (#195):
# (inn, lots, wins, latest lot, its position name, its date) — the same shape as _OKPD2_SUPPLIERS.
_CUSTOMER_SUPPLIERS = """
WITH j AS (
    SELECT supplier_inn, lot_id, is_winner, publish_date FROM {t[participants]}
    WHERE customer_inn = %(cust)s AND {cond}
      AND lot_id <> %(exclude)s  -- the notice the query was built from (#249, F-13); -1 — none
      AND lot_id IN (SELECT lot_id FROM {t[items]} WHERE okpd2_l4 IN (SELECT unnest(%(groups)s{text_arr})))
),
agg AS (
    SELECT supplier_inn, count(DISTINCT lot_id) AS n_lots, count(DISTINCT lot_id) FILTER (WHERE is_winner) AS n_wins
    FROM j GROUP BY supplier_inn
    ORDER BY n_lots DESC, n_wins DESC, supplier_inn{c} LIMIT %(limit)s
),
best AS (
    SELECT DISTINCT ON (j.supplier_inn) j.supplier_inn, j.lot_id, j.publish_date
    FROM j JOIN agg USING (supplier_inn)
    ORDER BY j.supplier_inn, j.publish_date DESC NULLS LAST, j.lot_id DESC
),
name AS (
    SELECT lot_id, min(product_name{c}) AS text FROM {t[items]}
    WHERE lot_id IN (SELECT lot_id FROM best) AND okpd2_l4 IN (SELECT unnest(%(groups)s{text_arr}))
    GROUP BY lot_id
)
SELECT agg.supplier_inn, agg.n_lots, agg.n_wins, best.lot_id, name.text, best.publish_date
FROM agg JOIN best USING (supplier_inn) LEFT JOIN name USING (lot_id)
ORDER BY agg.n_lots DESC, agg.n_wins DESC, agg.supplier_inn{c}
"""

_LOT = """
SELECT lot_id, subject, procedure_name, start_price, customer_inn, channel FROM {t[notices]}
WHERE lot_id = %(lot_id)s OR reqnum = %(ident)s ORDER BY (lot_id = %(lot_id)s) DESC, lot_id LIMIT 1
"""

_LOT_OKPD2 = """
SELECT okpd2_code FROM {t[items]}
WHERE lot_id = %(lot_id)s AND okpd2_code IS NOT NULL AND okpd2_code <> '' GROUP BY okpd2_code ORDER BY okpd2_code{c}
"""

# Position names of a lot: the query text when both subject and procedure_name are templates (src/api/lots.py).
_LOT_NAMES = """
SELECT product_name FROM {t[items]}
WHERE lot_id = %(lot_id)s AND product_name IS NOT NULL AND trim(product_name) <> ''
GROUP BY product_name ORDER BY product_name{c} LIMIT 5
"""


# The most active suppliers first (src/worker/prefetch.py warms the source cache in this order, D-056).
_TOP_SUPPLIERS = """
SELECT inn FROM {t[suppliers]} WHERE inn_valid
ORDER BY lots_won DESC NULLS LAST, lots_participated DESC NULLS LAST, last_seen DESC NULLS LAST, inn{c}
LIMIT %(limit)s
"""

# The baselines of the backtest (src/eval/baselines.py): one row per lot x supplier x customer before as_of and
# the OKPD2 groups of every lot. inn_valid: an INN with a wrong checksum is never relevant (D-032).
_PARTICIPATIONS = """
SELECT lot_id, supplier_inn AS inn, customer_inn, bool_or(is_winner) AS is_winner
FROM {t[participants]} WHERE inn_valid AND {cond}
GROUP BY lot_id, supplier_inn, customer_inn
"""
PARTICIPATIONS = pa.schema([("lot_id", pa.int64()), ("inn", pa.string()), ("customer_inn", pa.string()),
                            ("is_winner", pa.bool_())])

_LOT_CLASSES = """
SELECT DISTINCT lot_id, okpd2_l4 AS l4, okpd2_l2 AS l2 FROM {t[items]} WHERE okpd2_l2 IS NOT NULL
"""
LOT_CLASSES = pa.schema([("lot_id", pa.int64()), ("l4", pa.string()), ("l2", pa.string())])


def _table(cols: list[str], rows: list[tuple], schema: pa.Schema) -> pa.Table:
    """Rows of a query as an Arrow table of `schema` — the same types from both backends."""
    assert cols == schema.names, (cols, schema.names)
    columns = list(zip(*rows, strict=True)) if rows else [[] for _ in cols]
    return pa.table([pa.array(list(c), f.type) for c, f in zip(columns, schema, strict=True)], schema=schema)


def _okpd2_sql(level: int) -> str:
    return _OKPD2_SUPPLIERS.replace("{col}", OKPD2_COLUMN[level])  # a fixed column name, not user input


def _as_lot_id(ident: str) -> int:
    n = int(ident)
    return n if n < 2**63 else -1  # reqnum is longer than bigint: compare it as text only


def _cond(as_of: date | None, col: str = "publish_date") -> str:
    return f"{col} < %(as_of)s" if as_of else "TRUE"


def _pq(path: Path) -> str:
    return "read_parquet('" + path.as_posix().replace("'", "''") + "')"


class ParquetStore:
    """data/processed through DuckDB (D-026): `make demo` without Docker and the eval of a laptop."""

    name = "parquet"

    def __init__(self, processed: Path) -> None:
        self.processed = processed
        self._sql_kw = {"c": "", "bigint_arr": "", "text_arr": ""}

    def __repr__(self) -> str:
        return f"ParquetStore({self.processed})"

    def manifest(self) -> dict:
        missing = [f for f in REQUIRED if not (self.processed / f).exists()]
        if missing:
            raise StoreUnavailable(
                f"нет данных для поиска в {self.processed}: {', '.join(missing)} — соберите "
                "`uv run python -m src.ingest.build` (DATA_RAW_DIR → DATA_PROCESSED_DIR)"
            )
        return json.loads((self.processed / "manifest.json").read_text(encoding="utf-8"))

    def built_at(self) -> datetime:
        try:
            return datetime.fromisoformat(self.manifest()["built_at"])
        except (KeyError, ValueError) as e:
            raise StoreUnavailable(f"{self.processed}/manifest.json: нет built_at — пересоберите ingest") from e

    def _suppliers(self, as_of: date | None) -> str:
        path = self.processed / (f"suppliers_{as_of.isoformat()}.parquet" if as_of else "suppliers.parquet")
        if not path.exists():
            raise StoreUnavailable(f"{path} не собран: `uv run python -m src.ingest.build` (as_of={as_of}; "
                                   "заранее собирается только cutoff)")
        return _pq(path)

    def _fetch(self, template: str, params: dict, *, as_of: date | None = None, suppliers: str = "",
               lots: pa.Table | None = None) -> tuple[list[str], list[tuple]]:
        names = ("notices", "items", "participants")
        missing = [f"{n}.parquet" for n in names if f"{{t[{n}]}}" in template
                   and not (self.processed / f"{n}.parquet").exists()]
        if missing:
            raise StoreUnavailable(f"данные организаторов не собраны: нет {', '.join(missing)} в {self.processed} — "
                                   "`uv run python -m src.ingest.build` (DATA_RAW_DIR → DATA_PROCESSED_DIR)")
        t = {n: _pq(self.processed / f"{n}.parquet") for n in names}
        sql = template.format(t=t | {"suppliers": suppliers}, cond=_cond(as_of), lots="lots_in l", **self._sql_kw)
        for k in params:
            sql = sql.replace(f"%({k})s", f"${k}")
        con = duckdb.connect()
        try:
            if lots is not None:
                con.register("lots_in", lots)
            rel = con.execute(sql, {k: v for k, v in params.items() if f"${k}" in sql})
            return [d[0] for d in rel.description or []], rel.fetchall()
        finally:
            con.close()

    def index_rows(self, as_of: date | None) -> tuple[list[tuple[str, list[int]]], date | None]:
        self.manifest()
        p = {"as_of": as_of}
        _, rows = self._fetch(_INDEX_ROWS, p, as_of=as_of)
        _, end = self._fetch("SELECT max(publish_date) FROM {t[participants]} WHERE {cond}", p, as_of=as_of)
        return [(t, list(ids)) for t, ids in rows], (end[0][0] if end else None)

    def candidates(self, lots: LotHits, as_of: date | None, limit: int) -> list[tuple]:
        tbl = pa.table({"lot_id": pa.array(lots.lot_id, pa.int64()), "score": pa.array(lots.score, pa.float64()),
                        "doc_id": pa.array(lots.doc_id, pa.int32())})
        return self._fetch(_CANDIDATES, {"as_of": as_of, "limit": limit}, as_of=as_of, lots=tbl)[1]

    def lot_codes(self, lot_ids: list[int]) -> list[tuple[int, str]]:
        return self._fetch(_LOT_CODES, {"lots": lot_ids})[1]

    def text_codes(self, as_of: date | None) -> list[tuple[str, str, int]]:
        return self._fetch(_TEXT_CODES, {"as_of": as_of}, as_of=as_of)[1]

    def class_history(self, inns: list[str], codes: list[str], as_of: date | None) -> dict[str, tuple]:
        _, rows = self._fetch(_CLASS_HISTORY, {"inns": inns, "as_of": as_of, **class_params(codes)}, as_of=as_of)
        return {r[0]: tuple(r[1:]) for r in rows}

    def customer_lots(self, inns: list[str], customer_inn: str, as_of: date | None) -> dict[str, int]:
        _, rows = self._fetch(_CUSTOMER_LOTS, {"inns": inns, "cust": customer_inn, "as_of": as_of}, as_of=as_of)
        return {r[0]: int(r[1]) for r in rows}

    def supplier_history(self, inns: list[str], as_of: date | None) -> dict[str, History]:
        return _histories(self._fetch(_SUPPLIER_HISTORY, {"inns": inns, "as_of": as_of}, as_of=as_of)[1])

    def supplier_rows(self, inns: list[str], as_of: date | None) -> list[dict]:
        cols, rows = self._fetch(_SUPPLIERS, {"inns": inns, "as_of": as_of}, as_of=as_of,
                                 suppliers=self._suppliers(as_of))
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def okpd2_suppliers(self, level: int, codes: list[str], as_of: date | None, limit: int,
                        exclude_lot: int = -1) -> list[tuple]:
        params = {"codes": codes, "as_of": as_of, "limit": limit, "exclude": exclude_lot}
        return self._fetch(_okpd2_sql(level), params, as_of=as_of)[1]

    def customer_suppliers(self, customer_inn: str, groups: list[str], as_of: date | None, limit: int,
                           exclude_lot: int = -1) -> list[tuple]:
        params = {"cust": customer_inn, "groups": groups, "as_of": as_of, "limit": limit, "exclude": exclude_lot}
        return self._fetch(_CUSTOMER_SUPPLIERS, params, as_of=as_of)[1]

    def lot(self, ident: str) -> tuple | None:
        rows = self._fetch(_LOT, {"lot_id": _as_lot_id(ident), "ident": ident})[1]
        return rows[0] if rows else None

    def lot_okpd2(self, lot_id: int) -> list[str]:
        return [r[0] for r in self._fetch(_LOT_OKPD2, {"lot_id": lot_id})[1]]

    def lot_names(self, lot_id: int) -> list[str]:
        return [r[0] for r in self._fetch(_LOT_NAMES, {"lot_id": lot_id})[1]]

    def top_suppliers(self, limit: int) -> list[str]:
        return [r[0] for r in self._fetch(_TOP_SUPPLIERS, {"limit": limit}, suppliers=self._suppliers(None))[1]]

    def participations(self, as_of: date | None) -> pa.Table:
        return _table(*self._fetch(_PARTICIPATIONS, {"as_of": as_of}, as_of=as_of), PARTICIPATIONS)

    def lot_classes(self) -> pa.Table:
        return _table(*self._fetch(_LOT_CLASSES, {}), LOT_CLASSES)


class PgStore:
    """Schema `data` in Postgres (src/db/load.py). One connection per thread: FastAPI runs sync handlers
    in a thread pool; a broken connection is reopened once."""

    name = "postgres"

    def __init__(self, url: str, schema: str = "data") -> None:
        import psycopg  # noqa: F401 — the pg extra (`make setup`); a clear ImportError otherwise

        from src.db.load import pg_dsn

        self.dsn, self.schema = pg_dsn(url), schema
        self._local = threading.local()
        self._manifest: tuple[dict, float] | None = None
        self._precomputed: tuple[bool, float] | None = None
        self._sql_kw = {"c": ' COLLATE "C"', "bigint_arr": "::bigint[]", "text_arr": "::text[]"}

    def __repr__(self) -> str:
        return f"PgStore(schema={self.schema})"

    def _conn(self):
        import psycopg

        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
            self._local.conn = conn
        return conn

    def _fetch(self, template: str, params: dict, *, as_of: date | None = None, suppliers: str = "suppliers",
               lots: LotHits | None = None) -> tuple[list[str], list[tuple]]:
        import psycopg

        s = self.schema
        t = {n: f"{s}.{n}" for n in ("notices", "items", "participants", "supplier_items")} | {
            "suppliers": f"{s}.{suppliers}"}
        lots_sql = "unnest(%(l_lot)s::bigint[], %(l_score)s::float8[], %(l_doc)s::int[]) AS l(lot_id, score, doc_id)"
        if lots is not None:
            params = {**params, "l_lot": [int(x) for x in lots.lot_id], "l_score": [float(x) for x in lots.score],
                      "l_doc": [int(x) for x in lots.doc_id]}
        sql = template.format(t=t, cond=_cond(as_of), lots=lots_sql, **self._sql_kw)
        for attempt in (0, 1):
            fresh = getattr(self._local, "conn", None) is None
            try:
                cur = self._conn().execute(sql.encode(), params)
                return [d.name for d in cur.description or []], cur.fetchall()
            except psycopg.OperationalError as e:
                self._local.conn = None
                if attempt or fresh:  # reconnect once only if an open connection broke (server restart)
                    _forget(self)  # the next request chooses the store again (auto: parquet fallback)
                    raise StoreUnavailable(f"Postgres не отвечает ({type(e).__name__}) — "
                                           "`docker compose up -d db`, затем `make db-load`") from e
        raise AssertionError("unreachable")

    def manifest(self) -> dict:
        """Cached for MANIFEST_TTL_S: every search reads it twice (index key, Evidence date)."""
        import psycopg

        if self._manifest and self._manifest[1] > time.monotonic():
            return self._manifest[0]
        try:
            _, rows = self._fetch(f"SELECT body FROM {self.schema}.manifest LIMIT 1", {})
        except psycopg.Error as e:
            raise StoreUnavailable(f"Postgres: схема {self.schema} не загружена ({type(e).__name__}) — "
                                   "`make db-load`") from e
        if not rows:
            raise StoreUnavailable(f"Postgres: {self.schema}.manifest пуст — `make db-load`")
        body = rows[0][0]
        body = body if isinstance(body, dict) else json.loads(body)
        self._manifest = (body, time.monotonic() + MANIFEST_TTL_S)
        return body

    def built_at(self) -> datetime:
        return datetime.fromisoformat(self.manifest()["built_at"])

    def index_rows(self, as_of: date | None) -> tuple[list[tuple[str, list[int]]], date | None]:
        p = {"as_of": as_of}
        _, rows = self._fetch(_INDEX_ROWS, p, as_of=as_of)
        _, end = self._fetch("SELECT max(publish_date) FROM {t[participants]} WHERE {cond}", p, as_of=as_of)
        return [(t, list(ids)) for t, ids in rows], (end[0][0] if end else None)

    def candidates(self, lots: LotHits, as_of: date | None, limit: int) -> list[tuple]:
        return self._fetch(_CANDIDATES, {"as_of": as_of, "limit": limit}, as_of=as_of, lots=lots)[1]

    def lot_codes(self, lot_ids: list[int]) -> list[tuple[int, str]]:
        return self._fetch(_LOT_CODES, {"lots": lot_ids})[1]

    def text_codes(self, as_of: date | None) -> list[tuple[str, str, int]]:
        return self._fetch(_TEXT_CODES, {"as_of": as_of}, as_of=as_of)[1]

    def _has_supplier_items(self) -> bool:
        """`supplier_items` exists (a schema loaded before D-135 has none); checked at most once per MANIFEST_TTL_S."""
        if self._precomputed and self._precomputed[1] > time.monotonic():
            return self._precomputed[0]
        ok = self._fetch("SELECT to_regclass(%(t)s) IS NOT NULL", {"t": f"{self.schema}.supplier_items"})[1][0][0]
        if not ok:
            log.warning("Postgres: нет %s.supplier_items — class_history читает items, в несколько раз медленнее; "
                        "`make db-load` (D-135)", self.schema)
        self._precomputed = (ok, time.monotonic() + MANIFEST_TTL_S)
        return ok

    def class_history(self, inns: list[str], codes: list[str], as_of: date | None) -> dict[str, tuple]:
        template = _CLASS_HISTORY_PG if self._has_supplier_items() else _CLASS_HISTORY
        _, rows = self._fetch(template, {"inns": inns, "as_of": as_of, **class_params(codes)}, as_of=as_of)
        return {r[0]: tuple(r[1:]) for r in rows}

    def customer_lots(self, inns: list[str], customer_inn: str, as_of: date | None) -> dict[str, int]:
        _, rows = self._fetch(_CUSTOMER_LOTS, {"inns": inns, "cust": customer_inn, "as_of": as_of}, as_of=as_of)
        return {r[0]: int(r[1]) for r in rows}

    def supplier_history(self, inns: list[str], as_of: date | None) -> dict[str, History]:
        return _histories(self._fetch(_SUPPLIER_HISTORY, {"inns": inns, "as_of": as_of}, as_of=as_of)[1])

    def supplier_rows(self, inns: list[str], as_of: date | None) -> list[dict]:
        table = f"suppliers_{as_of.isoformat().replace('-', '_')}" if as_of else "suppliers"
        _, ok = self._fetch("SELECT to_regclass(%(t)s) IS NOT NULL", {"t": f"{self.schema}.{table}"})
        if not ok[0][0]:
            raise StoreUnavailable(f"Postgres: нет {self.schema}.{table} (as_of={as_of}; заранее собирается только "
                                   "cutoff) — `make db-load`")
        cols, rows = self._fetch(_SUPPLIERS, {"inns": inns, "as_of": as_of}, as_of=as_of, suppliers=table)
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def okpd2_suppliers(self, level: int, codes: list[str], as_of: date | None, limit: int,
                        exclude_lot: int = -1) -> list[tuple]:
        params = {"codes": codes, "as_of": as_of, "limit": limit, "exclude": exclude_lot}
        return self._fetch(_okpd2_sql(level), params, as_of=as_of)[1]

    def customer_suppliers(self, customer_inn: str, groups: list[str], as_of: date | None, limit: int,
                           exclude_lot: int = -1) -> list[tuple]:
        params = {"cust": customer_inn, "groups": groups, "as_of": as_of, "limit": limit, "exclude": exclude_lot}
        return self._fetch(_CUSTOMER_SUPPLIERS, params, as_of=as_of)[1]

    def lot(self, ident: str) -> tuple | None:
        _, rows = self._fetch(_LOT, {"lot_id": _as_lot_id(ident), "ident": ident})
        return rows[0] if rows else None

    def lot_okpd2(self, lot_id: int) -> list[str]:
        return [r[0] for r in self._fetch(_LOT_OKPD2, {"lot_id": lot_id})[1]]

    def lot_names(self, lot_id: int) -> list[str]:
        return [r[0] for r in self._fetch(_LOT_NAMES, {"lot_id": lot_id})[1]]

    def top_suppliers(self, limit: int) -> list[str]:
        return [r[0] for r in self._fetch(_TOP_SUPPLIERS, {"limit": limit})[1]]

    def participations(self, as_of: date | None) -> pa.Table:
        return _table(*self._fetch(_PARTICIPATIONS, {"as_of": as_of}, as_of=as_of), PARTICIPATIONS)

    def lot_classes(self) -> pa.Table:
        return _table(*self._fetch(_LOT_CLASSES, {}), LOT_CLASSES)


def processed_dir() -> Path:
    """DATA_PROCESSED_DIR, default data/processed."""
    return get_settings().data_processed_dir


MANIFEST_TTL_S = 30.0  # a new `make db-load` is seen by search within this time
RETRY_PG_S = 30.0  # auto mode: how long the parquet fallback is kept before Postgres is tried again
_stores: dict[tuple, tuple[Store, float]] = {}  # key -> (store, expires at; inf for the chosen one)
_lock = threading.Lock()


def get_store() -> Store:
    """The store for the current environment (DATA_STORE, DATABASE_URL, DATA_SCHEMA, DATA_PROCESSED_DIR).

    auto without Postgres falls back to parquet for RETRY_PG_S: a refused connection costs seconds
    (≈ 10 s on Windows), it must not be paid on every request; a later `make db-load` is picked up."""
    cfg = get_settings()
    mode, url, schema = cfg.data_store, cfg.database_url, cfg.data_schema
    key = (mode, url, schema, str(processed_dir()))
    with _lock:
        if key in _stores and _stores[key][1] > time.monotonic():
            return _stores[key][0]
    if mode not in ("auto", "postgres", "parquet"):
        raise ValueError(f"DATA_STORE={mode!r}: нужно auto, postgres или parquet")
    store: Store = ParquetStore(processed_dir())
    expires = math.inf
    if mode == "postgres" or (mode == "auto" and url.startswith("postgres")):
        try:
            pg = PgStore(url, schema)
            pg.manifest()
            store = pg
        except (ImportError, StoreUnavailable) as e:
            if mode == "postgres":
                raise
            log.warning("DATA_STORE=auto: Postgres недоступен (%s) — поиск по parquet, повтор через %d с",
                        e, RETRY_PG_S)
            expires = time.monotonic() + RETRY_PG_S
    with _lock:
        _stores[key] = (store, expires)
    return store


def _forget(store: Store) -> None:
    with _lock:
        for key in [k for k, (s, _) in _stores.items() if s is store]:
            del _stores[key]


def clear_stores() -> None:
    with _lock:
        _stores.clear()
