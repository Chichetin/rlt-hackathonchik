"""Text normalization for BM25 over lot positions (methodology step 1, research/METHODOLOGY.md).

lower case, ё -> е, words and numbers (`\\w+`), a few prepositions dropped, and a crude
"prefix stemmer": a Cyrillic/Latin word is cut to its first PREFIX characters ("бумага",
"бумаги", "бумагу" -> "бумаг"). No PyStemmer in the lockfile; a real stemmer or lemmatizer is an
experiment, not part of v1. Numbers stay whole ("80", "a4" -> "a4").
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from src.settings import get_settings

# Bump when tokenization changes: it is part of the cached index key (src/match/index.py).
TOKENIZER_VERSION = "prefix5-v1"
PREFIX = 5

TOKENIZERS = ("prefix5", "snowball", "pymorphy")
DEFAULT_TOKENIZER = "prefix5"
_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "search.yaml"

_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset({"и", "в", "во", "на", "с", "со", "по", "для", "из", "к", "от", "до", "или", "а", "о", "об", "не"})
# Cyrillic "а" written instead of Latin "a" in "А4" — the same paper format, one token.
_CYR2LAT = str.maketrans({"а": "a", "в": "b", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x"})


@lru_cache(maxsize=1)
def _config_tokenizer() -> str:
    return str(yaml.safe_load(_CONFIG.read_text(encoding="utf-8")).get("tokenizer", DEFAULT_TOKENIZER))


def tokenizer_name() -> str:
    """BM25_TOKENIZER (benchmarks) overrides `tokenizer:` of configs/search.yaml."""
    name = get_settings().bm25_tokenizer or _config_tokenizer()
    if name not in TOKENIZERS:
        raise ValueError(f"tokenizer {name!r} не поддерживается (есть: {', '.join(TOKENIZERS)})")
    return name


def tokenizer_version() -> str:
    """Part of the index cache key; prefix5 keeps the old string so existing indexes stay valid."""
    name = tokenizer_name()
    return TOKENIZER_VERSION if name == DEFAULT_TOKENIZER else f"{name}-v1"


@lru_cache(maxsize=1)
def _stemmers():
    import Stemmer  # PyStemmer

    return Stemmer.Stemmer("russian"), Stemmer.Stemmer("english")


@lru_cache(maxsize=500_000)
def _snowball(w: str) -> str:
    ru, en = _stemmers()
    return (en if w.isascii() else ru).stemWord(w)


@lru_cache(maxsize=1)
def _morph():
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=500_000)
def _lemma(w: str) -> str:
    if w.isascii():
        return w
    return _morph().parse(w)[0].normal_form.replace("ё", "е")


def tokenize(text: str) -> list[str]:
    word = {"prefix5": lambda w: w[:PREFIX], "snowball": _snowball, "pymorphy": _lemma}[tokenizer_name()]
    out: list[str] = []
    for w in _WORD.findall(text.lower().replace("ё", "е")):
        if w in _STOP or w == "_":
            continue
        if any(ch.isdigit() for ch in w):
            out.append(w.translate(_CYR2LAT))  # "а4" == "a4"; numbers are kept whole
        elif len(w) >= 2:
            out.append(word(w))
    return out
