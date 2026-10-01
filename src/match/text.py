"""Text normalization for BM25 over lot positions (methodology step 1, research/METHODOLOGY.md).

lower case, ё -> е, words and numbers (`\\w+`), a few prepositions dropped, and a crude
"prefix stemmer": a Cyrillic/Latin word is cut to its first PREFIX characters ("бумага",
"бумаги", "бумагу" -> "бумаг"). No PyStemmer in the lockfile; a real stemmer or lemmatizer is an
experiment, not part of v1. Numbers stay whole ("80", "a4" -> "a4").
"""

from __future__ import annotations

import re

# Bump when tokenization changes: it is part of the cached index key (src/match/index.py).
TOKENIZER_VERSION = "prefix5-v1"
PREFIX = 5

_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset({"и", "в", "во", "на", "с", "со", "по", "для", "из", "к", "от", "до", "или", "а", "о", "об", "не"})
# Cyrillic "а" written instead of Latin "a" in "А4" — the same paper format, one token.
_CYR2LAT = str.maketrans({"а": "a", "в": "b", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x"})


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall(text.lower().replace("ё", "е")):
        if w in _STOP or w == "_":
            continue
        if any(ch.isdigit() for ch in w):
            out.append(w.translate(_CYR2LAT))  # "а4" == "a4"; numbers are kept whole
        elif len(w) >= 2:
            out.append(w[:PREFIX])
    return out
