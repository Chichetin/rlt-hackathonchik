"""Spoiled queries for the speller measurement (#282): `make eval EXP=<id> CORRUPT=typo|layout`.

Deterministic per query (the seed is the query id), so every experiment sees the same spoiled text:
- typo: each Cyrillic word of ≥ 5 letters is spoiled with probability TYPO_SHARE (at least one per query)
  by one edit a hand makes — a dropped letter, two letters swapped, a neighbouring key, a doubled letter;
- layout: the whole query typed with the Latin layout on (Cyrillic letters → the keys they sit on).
Only the text changes: OKPD2 codes, customer and dates stay as they were.
"""

from __future__ import annotations

import hashlib
import random
import re

from src.contracts import Query
from src.match.speller import RU_TO_EN

MODES = ("typo", "layout")
TYPO_SHARE = 0.3
_ROWS = ("йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю")
_NEIGHBOURS = {ch: row[max(i - 1, 0):i] + row[i + 1:i + 2] for row in _ROWS for i, ch in enumerate(row)}
_LONG_WORD = re.compile(r"[А-Яа-яЁё]{5,}")


def _rng(query_id: str) -> random.Random:
    return random.Random(int(hashlib.sha256(query_id.encode()).hexdigest()[:16], 16))


def typo(word: str, rng: random.Random) -> str:
    """One edit inside the word (the first letter kept: people rarely miss it)."""
    i = rng.randrange(1, len(word))
    kind = rng.choice(("drop", "swap", "neighbour", "double"))
    if kind == "drop":
        return word[:i] + word[i + 1:]
    if kind == "swap" and i < len(word) - 1:
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    near = _NEIGHBOURS.get(word[i].lower(), "")
    if kind == "neighbour" and near:
        ch = rng.choice(near)
        return word[:i] + (ch.upper() if word[i].isupper() else ch) + word[i + 1:]
    return word[:i] + word[i] + word[i:]  # double (or a swap / neighbour that had no room)


def spoil_text(text: str, mode: str, query_id: str) -> str:
    if mode not in MODES:
        raise ValueError(f"порча {mode!r}: ожидается одно из {MODES}")
    if mode == "layout":
        return "".join(RU_TO_EN.get(ch.lower(), ch) for ch in text)
    rng = _rng(query_id)
    words = list(_LONG_WORD.finditer(text))
    if not words:
        return text
    chosen = {m.start() for m in words if rng.random() < TYPO_SHARE} or {rng.choice(words).start()}
    return _LONG_WORD.sub(lambda m: typo(m.group(0), rng) if m.start() in chosen else m.group(0), text)


def spoil(query: Query, mode: str) -> Query:
    return query.model_copy(update={"text": spoil_text(query.text, mode, query.query_id)})
