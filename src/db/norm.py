"""Normalization of product names, the key of the vector table (src/db/embed.py).

Positions are normalized in Postgres (`norm_sql`), queries in Python (`normalize`); both must give
the same string (tests/unit/test_db_embed.py). Soft on purpose: the model needs natural text, so no
stemming as in BM25 (src/match/text.py) — only case, ё → е and whitespace.
"""

from __future__ import annotations

import re

from psycopg import sql

# The same explicit whitespace set on both sides: Python `\s` matches U+00A0, Postgres `\s` does not.
_WS = re.compile(r"[ \t\n\r\f\v\xa0]+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.lower().replace("ё", "е")).strip()


def norm_sql(col: sql.Composable) -> sql.Composed:
    return sql.SQL(r"btrim(regexp_replace(replace(lower({}), 'ё', 'е'), '[ \t\n\r\f\v\u00a0]+', ' ', 'g'))").format(col)
