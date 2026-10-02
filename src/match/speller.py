"""Query spelling (#282): the wrong keyboard layout and typos, fixed by a dictionary of our own corpus.

The dictionary is the word forms of the indexed position names (texts.parquet of the BM25 index) with the
number of distinct names each occurs in — so a backtest index (as_of) gives a dictionary of the past only.
Only a word that is NOT a trusted word of the dictionary is touched:
1. layout — keys typed in the other layout («,evfuf» → «бумага», «сфтщт» → «canon»; `,.;'[]` are Russian
   letters there): the other layout is taken when it is a known, much more frequent word;
2. typo — a Cyrillic word of ≥ 4 letters that the Russian morphological dictionary (pymorphy3) does NOT know
   («зонты», «зеленка», «дума» are words even when the corpus has none of them — never «corrected» into
   «зоны», «пеленка», «дома»): a known word with the same first letter within Damerau–Levenshtein distance 1
   (2 for words of ≥ DIST2_LEN letters, never for a capitalised word), found through single-delete keys
   (SymSpell-like: single deletes on both sides, so not every 2-edit word is found — it is then left as typed),
   much more frequent than the word; among several, the frequency discounted by DIST_DISCOUNT
   per edit decides («кортриджж» → «картридж», not the corpus typo «катридж»). A rare corpus word
   (< TRUSTED names) is still a typo when a word one edit away is RARE_RATIO times more frequent;
   a word in TRUSTED names or more is never changed.
Numbers, codes, short words, abbreviations in capitals («ДПЛС», «КПИ») and capitalised words inside a phrase
(names: «ул. Гаврина» is a phrase start, «Корняков» in the middle is not) are never changed; nor is a word whose
correction gives the search the same token (prefix5: «бокорезов» → «бокорезы» changes nothing but the text).
«ё» is «е» everywhere, as in the tokenizer. The result says which words were changed,
so the UI can show «показаны результаты по запросу …» and offer the query as typed (`filters.spell = "off"`).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from src.match.text import tokenize

log = logging.getLogger(__name__)

MIN_KNOWN = 3  # a word in fewer distinct names is not a word (the corpus has typos of its own)
TRUSTED = 20  # a word in this many names or more is never corrected
MIN_TARGET = 5  # a correction must be at least this frequent
RATIO = 20  # ... and this many times more frequent than an unknown word it replaces
RARE_RATIO = 100  # ... or than a rare known word (MIN_KNOWN ≤ f < TRUSTED), and one edit away only
DIST_DISCOUNT = 0.02  # candidate rank: frequency × DIST_DISCOUNT ** distance
MIN_TYPO_LEN = 4
DIST2_LEN = 8  # words this long may be 2 edits away (7: «аргенто» → «агент», «Гаврина» → «грина»)
VOCAB_FILE = "speller-vocab-v2.json"  # v2: «ё» → «е»

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,.~{}:\"<>"
_RU = "ёйцукенгшщзхъфывапролджэячсмитьбюёхъжэбю"
_TOKEN = re.compile(r"[A-Za-zА-Яа-яЁё\[\];',.`~{}:\"<>]+")  # a word with the keys that are letters in the other layout
_LAT_KEYS = re.compile(r"[a-z\[\];',.`~{}:\"<>]*[a-z][a-z\[\];',.`~{}:\"<>]*")
EN_TO_RU = dict(zip(_EN, _RU, strict=True))  # with the shifted keys: «<» and «,» are both «б»
RU_TO_EN = dict(zip(_RU[:33], _EN[:33], strict=True))  # the unshifted key of each letter
_CYR = re.compile(r"[а-яё]+")
_LAYOUT_PUNCT = re.compile(r"[\[\];',.`~{}:\"<>]")  # keys that are letters in the Russian layout


@functools.cache
def _morph() -> Any:
    import pymorphy3  # a dependency already (src/match/text.py, tokenizer «pymorphy»)

    return pymorphy3.MorphAnalyzer()


def is_russian_word(word: str) -> bool:
    """A word form the Russian morphological dictionary knows: never a typo, whatever the corpus says."""
    return bool(_morph().word_is_known(word))


@dataclass(frozen=True)
class Correction:
    text: str  # the query to search with
    changes: tuple[tuple[str, str], ...] = ()  # (as typed, as searched), in order

    @property
    def changed(self) -> bool:
        return bool(self.changes)


@dataclass
class Speller:
    freq: dict[str, int]
    _deletes: dict[str, list[str]] | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def known(self, word: str) -> bool:
        return self.freq.get(word, 0) >= MIN_KNOWN

    def trusted(self, word: str) -> bool:
        return self.freq.get(word, 0) >= TRUSTED

    def _better(self, cand: str, word: str) -> bool:
        f, own = self.freq.get(cand, 0), self.freq.get(word, 0)
        return f >= MIN_TARGET and f >= (RARE_RATIO if own >= MIN_KNOWN else RATIO) * max(own, 1)

    def _delete_index(self) -> dict[str, list[str]]:
        """single delete of a known word → the words (built once, on the first unknown word)."""
        with self._lock:
            if self._deletes is None:
                idx: dict[str, list[str]] = {}
                for w, f in self.freq.items():
                    if f < MIN_TARGET or len(w) < MIN_TYPO_LEN - 1 or not _CYR.fullmatch(w):
                        continue
                    for d in {w[:i] + w[i + 1:] for i in range(len(w))}:
                        idx.setdefault(d, []).append(w)
                self._deletes = idx
            return self._deletes

    def layout(self, token: str) -> str | None:
        """The same keys in the other layout, when that is a known and much more frequent word."""
        table = EN_TO_RU if _LAT_KEYS.fullmatch(token) else RU_TO_EN if _CYR.fullmatch(token) else None
        if table is None or len(token) < 3 or (table is RU_TO_EN and is_russian_word(token)):
            return None
        other = "".join(table.get(ch, ch) for ch in token)
        return other if other != token and self.known(other) and self._better(other, token) else None

    def typo(self, word: str, max_dist: int = 2) -> str | None:
        """The most frequent known word within distance 1 (2 for long words), much more frequent than `word`."""
        if len(word) < MIN_TYPO_LEN or not _CYR.fullmatch(word) or self.trusted(word) or is_russian_word(word):
            return None
        index = self._delete_index()
        keys = {word} | _deletes(word)
        limit = 1 if self.known(word) or len(word) < DIST2_LEN else min(2, max_dist)  # a rare known word: one edit
        if limit == 2:
            keys |= {d2 for d in list(keys) for d2 in _deletes(d)}
        cands = {w for k in keys for w in index.get(k, ())}
        cands |= {k for k in keys if self.freq.get(k, 0) >= MIN_TARGET}  # the word with extra letters
        best: tuple[float, str] | None = None
        for c in cands:
            if c == word or c[0] != word[0] or not self._better(c, word):  # people rarely miss the first letter
                continue
            d = damerau(word, c, limit)
            if d <= limit:
                key = (-self.freq[c] * DIST_DISCOUNT ** d, c)
                best = key if best is None or key < best else best
        return best[1] if best else None

    def correct(self, text: str) -> Correction:
        changes: list[tuple[str, str]] = []

        phrase_start = True  # the word being fixed starts a phrase (the text or after «. ; : ! ?»)

        def fix_word(m: re.Match[str]) -> str:
            raw = m.group(0)
            word = raw.lower().replace("ё", "е")
            if len(raw) > 1 and raw.isupper():  # an abbreviation or a code
                return raw
            if raw[0].isupper() and not (phrase_start and m.start() == 0):  # a name inside a phrase
                return raw
            if self.known(word) and (self.trusted(word) or not _CYR.fullmatch(word)):
                return raw
            new = None if self.known(word) else self.layout(word)
            if new is None:
                new = self.typo(word, max_dist=1 if raw[0].isupper() else 2)  # «Корняков»: a name, one edit at most
                if new is not None and tokenize(new) == tokenize(word):  # the search sees the same token
                    new = None
            if new is None:
                return raw
            changes.append((raw, new))
            return new

        def fix(m: re.Match[str]) -> str:
            nonlocal phrase_start
            raw = m.group(0)
            before = text[:m.start()].rstrip()
            phrase_start = not before or before[-1] in ".;:!?«\"(\n"
            token = raw.lower()
            letters = "".join(_WORD.findall(token))
            # «[kt,» is «хлеб»: «kt» is a corpus word, but the keys of «х» and «б» say the layout was wrong
            punct = bool(_LAYOUT_PUNCT.search(token.rstrip(",.;:")))
            if (not self.known(letters) or punct) and _LAT_KEYS.fullmatch(token):
                # the whole token typed in the Latin layout; a trailing «, . ; :» may be real punctuation
                for cut in range(0, min(2, len(token) - 3) + 1):
                    head, tail = token[:len(token) - cut], raw[len(raw) - cut:]
                    if tail.strip(",.;:") == "" and (new := self.layout(head)):
                        changes.append((raw[:len(raw) - cut], new))
                        return new + tail
            return _WORD.sub(fix_word, raw)  # punctuation around the words stays as typed

        out = _TOKEN.sub(fix, text)
        return Correction(out, tuple(changes))


def _deletes(word: str) -> set[str]:
    return {word[:i] + word[i + 1:] for i in range(len(word))}


def damerau(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance (adjacent transposition = 1); > limit as soon as it is certain."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def vocabulary(texts: list[str]) -> dict[str, int]:
    """word form → number of distinct texts it occurs in (lowercase letters only)."""
    c: Counter[str] = Counter()
    for t in texts:
        c.update({w.lower().replace("ё", "е") for w in _WORD.findall(t)})
    return dict(c)


_SPELLERS: dict[str, Speller] = {}
_build_lock = threading.Lock()


def for_index(index_dir: Path) -> Speller:
    """The speller of one BM25 index (its texts.parquet), the dictionary cached next to it on disk.

    The disk copy is a convenience: in `make up` the index directory is read-only for the API, so a failed write
    only logs a warning — the dictionary stays in memory and is never rebuilt per request. Prebuild it with the
    index (`python -m src.match.index`) so the first search does not pay the ≈ 6 s.
    """
    key = str(index_dir)
    with _build_lock:
        if key not in _SPELLERS:
            path = index_dir / VOCAB_FILE
            if path.exists():
                freq = json.loads(path.read_text(encoding="utf-8"))
            else:
                texts_path = index_dir / "texts.parquet"
                texts = pq.read_table(texts_path, columns=["text"]).column("text").to_pylist() \
                    if texts_path.exists() else []
                freq = vocabulary([t for t in texts if t])
                tmp = path.with_suffix(f".tmp{os.getpid()}")  # API, worker and parallel evals build it at once
                try:
                    tmp.write_text(json.dumps(freq, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(path)
                except OSError as e:  # read-only index (make up): keep it in memory, say how to prebuild
                    log.warning("speller: словарь не сохранён рядом с индексом (%s) — соберите заранее: "
                                "python -m src.match.index", e)
                log.info("speller: словарь %d словоформ из %s", len(freq), index_dir.name)
            if len(_SPELLERS) >= 4:
                _SPELLERS.pop(next(iter(_SPELLERS)))
            _SPELLERS[key] = Speller(freq)
        return _SPELLERS[key]
