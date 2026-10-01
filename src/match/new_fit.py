"""product_fit_new of the block «Новые»: what a company really sells, not only its OKVED class (#77).

Increment 1 — signals without new external sources (research/METHODOLOGY.md «Релевантность новых
поставщиков», configs/discover.yaml):

- okved_match (signals 5, 8): the relation of the main OKVED to the query (class → 1.0, wholesale →
  0.6, as product_fit v0, D-036) times the depth check: a main OKVED deeper than the class is compared
  with the FULL OKPD2 code of the query (`21.20.2` agrees with `21.20.23.110`, `21.20.1` differs).
  The registry search goes by the MAIN OKVED; additional OKVEDs are not in any cached rmsp / pb.nalog
  answer, so «the code is lost among additional ones» is not measured here (needs the МСП dump).
- text_match (signals 6, 8): word stems of the query and of the full-OKPD2 profile (frequent words of
  organizers' positions with this code) found in the company name and in the name of its main OKVED.
- minus_words (signal 7): a hand-made table of stems for broad classes; a hit lowers the value.
- llm_check (signal 9) is not computed in this increment: its weight is split between the other two
  and the explanation says so.

HAND-MADE HEURISTIC, NOT VALIDATED: the depth values (agree 1.0 / class 0.85 / differs 0.6), the
word-hit contributions, text_saturation, profile_hit, the minus-word table and the minus_penalty
(configs/discover.yaml) were chosen by hand from a look at 11 cached queries (#77) — they are not fitted
and not checked against labels (precision@10 needs #21). That is why the default mode is shadow.

Only texts that are Evidence of the company AND literally occur in the cached registry page are read
(src/enrich/evidence.py::value_in_page); every part names the Evidence it stands on. Nothing is generated.

Mode (`mode()`): off | shadow | on — configs/discover.yaml `new_fit.mode`, env DISCOVERY_NEW_FIT.
shadow (default) computes and logs, ranking and score stay v0; on is for experiments only (switching
it on by default changes the scoring methodology — human confirmation №3).
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import duckdb
import yaml

from src.contracts import Counterparty, Evidence
from src.enrich.evidence import value_in_page

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "discover.yaml"
MODES = ("off", "shadow", "on")
PREFIX = 5  # a word is compared by its first PREFIX letters («мебели» → «мебел»)

Mode = Literal["off", "shadow", "on"]
Depth = Literal["agree", "class", "differs", "wholesale"]

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_DIGITS = re.compile(r"\D")


@lru_cache(maxsize=4)
def _load(path: str, mtime: int) -> dict[str, Any]:
    return dict(yaml.safe_load(Path(path).read_text(encoding="utf-8"))["new_fit"])


def config(path: Path = CONFIG) -> dict[str, Any]:
    return _load(str(path), path.stat().st_mtime_ns)


def mode(cfg: dict[str, Any] | None = None) -> Mode:
    """DISCOVERY_NEW_FIT overrides `new_fit.mode`; an unknown value falls back to shadow with a warning."""
    raw = os.environ.get("DISCOVERY_NEW_FIT") or str((cfg or config()).get("mode", "shadow"))
    if raw not in MODES:
        log.warning("new_fit: неизвестный режим %r — используется shadow", raw)
        return "shadow"
    if raw == "on":
        log.warning("new_fit: режим on — ранжирование блока «Новые» по product_fit_new; константы подобраны "
                    "вручную и не проверены на разметке (precision@10, #21); включать только для эксперимента")
    return raw  # type: ignore[return-value]


def stems(text: str, stop: frozenset[str] = frozenset()) -> list[str]:
    """Lower-case letter words of 3+ letters cut to PREFIX letters, stop stems dropped, order kept."""
    out: list[str] = []
    for w in _WORD.findall(text.lower().replace("ё", "е")):
        s = w[:PREFIX]
        if len(w) >= 3 and s not in stop and s not in out:
            out.append(s)
    return out


def _stop(cfg: dict[str, Any], *keys: str) -> frozenset[str]:
    """Stop stems of the config lists, cut to PREFIX letters as the stems they are compared with."""
    return frozenset(str(w).lower()[:PREFIX] for k in keys for w in cfg.get(k, ()))


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower().replace("ё", "е")) if len(w) >= 3]


# ---- signal 8: the full OKPD2 code of the query --------------------------------------------------

@dataclass(frozen=True)
class Profile:
    """Frequent words of organizers' positions with the query's OKPD2 code (not cut to the class)."""

    code: str  # the prefix actually used: the full code or a shorter level, never the class
    positions: int  # distinct position names with this prefix; at most `profile_max_names` (a LIMIT)
    terms: tuple[str, ...]
    capped: bool = False  # True: the LIMIT was reached, so `positions` is a floor («≥ N»), not a count


def _levels(code: str) -> list[str]:
    """`21.20.23.110` → [`21.20.23.110`, `21.20.23.1`, `21.20.23`, `21.20.2`]: deepest first, no class."""
    code = code.strip()
    out = [code[:n] for n in (len(code), 10, 8, 7) if 7 <= n <= len(code) and code[:n][-1] != "."]
    return list(dict.fromkeys(out))


@lru_cache(maxsize=256)
def _profile(code: str, items: str, notices: str, as_of: date | None, mtime: int,
             n_terms: int, min_positions: int, min_share: float, max_names: int,
             stop: frozenset[str]) -> Profile | None:
    del mtime  # part of the cache key only: a rebuilt items.parquet is read again
    con = duckdb.connect()
    try:
        for prefix in _levels(code):
            where = "starts_with(i.okpd2_code, ?)"
            args: list[Any] = [items, prefix]
            if as_of is not None and Path(notices).exists():
                where += " AND i.lot_id IN (SELECT lot_id FROM read_parquet(?) WHERE publish_date < ?)"
                args += [notices, as_of]
            elif as_of is not None:
                log.warning("new_fit: нет %s — профиль ОКПД2 %s строится по всем позициям без среза as_of=%s "
                            "(возможна утечка будущих лотов)", notices, prefix, as_of)
            rows = con.execute(
                f"SELECT i.product_name, count(*) AS n FROM read_parquet(?) i WHERE {where} "
                "AND i.product_name IS NOT NULL GROUP BY 1 ORDER BY n DESC, 1 LIMIT ?",
                [*args, max_names],
            ).fetchall()
            if len(rows) < min_positions:
                continue
            df: Counter[str] = Counter()
            for name, _ in rows:
                df.update(stems(name, stop))
            terms = tuple(t for t, c in sorted(df.items(), key=lambda x: (-x[1], x[0]))
                          if c / len(rows) >= min_share)[:n_terms]
            return Profile(code=prefix, positions=len(rows), terms=terms, capped=len(rows) >= max_names)
    except duckdb.Error as e:
        log.warning("new_fit: профиль ОКПД2 %s не построен: %s", code, e)
    finally:
        con.close()
    return None


def code_profile(codes: Sequence[str], processed: Path, as_of: date | None,
                 cfg: dict[str, Any] | None = None) -> Profile | None:
    """Profile of the deepest query code that has enough positions; None — no data or only the class."""
    cfg = cfg or config()
    items = processed / "items.parquet"
    if not items.exists():
        return None
    stop = _stop(cfg, "query_stopwords")
    for code in sorted((c.strip() for c in codes), key=len, reverse=True):
        prof = _profile(code, items.as_posix(), (processed / "notices.parquet").as_posix(), as_of,
                        items.stat().st_mtime_ns, int(cfg["profile_terms"]), int(cfg["profile_min_positions"]),
                        float(cfg["profile_min_share"]), int(cfg["profile_max_names"]), stop)
        if prof is not None:
            return prof
    return None


def query_overlap(query_text: str, profile: Profile, cfg: dict[str, Any] | None = None) -> list[str]:
    """Stems shared by the query text and the OKPD2 profile. Empty — the code and the text may disagree
    (the expert's «bad case», #113): shown in the comparison, not acted upon in this increment."""
    cfg = cfg or config()
    stop = _stop(cfg, "query_stopwords", "text_stopwords")
    return [s for s in stems(query_text, stop) if s in profile.terms]


# ---- scoring ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Part:
    name: Literal["okved_match", "text_match", "minus_words", "llm_check"]
    value: float
    weight: float  # share in product_fit_new; minus_words is a multiplier (weight 0)
    explanation: str
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class NewFit:
    value: float
    parts: tuple[Part, ...]
    depth: Depth
    hits: tuple[str, ...] = field(default=())
    minus: tuple[str, ...] = field(default=())

    def part(self, name: str) -> Part:
        return next(p for p in self.parts if p.name == name)

    def explanation(self) -> str:
        okved, text = self.part("okved_match"), self.part("text_match")
        minus, llm = self.part("minus_words"), self.part("llm_check")
        mult = f") × {1 - minus.value:.2f}" if minus.value else ")"
        return (f"product_fit_new {self.value:.2f} = ({okved.weight:.2f}·ОКВЭД {okved.value:.2f} + "
                f"{text.weight:.2f}·текст {text.value:.2f}{mult}. {okved.explanation}. {text.explanation}. "
                f"{minus.explanation}. {llm.explanation}")


def _verified(cp: Counterparty, fld: str, page_body: str) -> list[Evidence]:
    """Evidence of this field whose value literally occurs in the cached registry page."""
    return [e for e in cp.evidence
            if e.field == fld and e.value and e.url.startswith("http") and value_in_page(fld, e.value, page_body)]


def depth(okved: str, codes: Sequence[str], relation: str) -> Depth:
    """Main OKVED vs the full OKPD2 codes of the query below the class level (signal 8)."""
    if relation != "class":
        return "wholesale"
    ok = _DIGITS.sub("", okved)
    if len(ok) <= 4:
        return "class"
    full = [_DIGITS.sub("", c) for c in codes if _DIGITS.sub("", c)[:4] == ok[:4]]
    if not full or all(len(c) <= 4 for c in full):
        return "class"
    return "agree" if any(c.startswith(ok) for c in full) else "differs"


def _hit(stem: str, words: Sequence[str], compound: bool) -> bool:
    if any(w.startswith(stem) for w in words):
        return True
    # «бюромебель» ⊃ «мебел»: a full-length stem inside a longer company-name word
    return compound and len(stem) == PREFIX and any(stem in w[1:] for w in words if len(w) > PREFIX + 2)


def score(cp: Counterparty, *, okved: str, relation: str, base: float, codes: Sequence[str],
          okpd_class: str, query_text: str, page_body: str, profile: Profile | None,
          cfg: dict[str, Any] | None = None) -> NewFit:
    """product_fit_new of one registry row. `base` is product_fit v0 of its relation (1.0 / 0.6)."""
    cfg = cfg or config()
    w = cfg["weights"]
    w_ok, w_text = float(w["okved"]), float(w["text"])
    norm = w_ok + w_text  # LLM is not run: its share is split between the two in proportion
    w_ok, w_text = w_ok / norm, w_text / norm

    names = _verified(cp, "name", page_body)
    okveds = _verified(cp, "okved", page_body)
    okved_names = [e for e in okveds if not (e.value or "").startswith(f"{cp.inn}.")]  # the name, not the code line

    # okved_match
    d = depth(okved, codes, relation)
    factor = 1.0 if d == "wholesale" else float(cfg["depth"][d])
    full = ", ".join(c.strip() for c in codes) or okpd_class
    why_depth = {
        "agree": f"ОКВЭД {okved} совпадает с ОКПД2 {full} глубже класса",
        "class": f"ОКВЭД {okved} — только класс, с подгруппой ОКПД2 {full} не сравнить",
        "differs": f"ОКВЭД {okved} — другая подгруппа класса, чем ОКПД2 {full}",
        "wholesale": f"ОКВЭД {okved} — оптовая торговля, глубина не сравнивается",
    }[d]
    okved_part = Part("okved_match", round(base * factor, 4), round(w_ok, 4),
                      f"ОКВЭД: {base:.1f} (основной, {'класс' if relation == 'class' else 'опт'}) × {factor:.2f} — "
                      f"{why_depth}", tuple(okveds))

    # text_match
    qstop = _stop(cfg, "query_stopwords", "text_stopwords")
    q_stems = stems(query_text, qstop)
    p_stems = [t for t in (profile.terms if profile else ()) if t not in q_stems and t not in qstop]
    name_words = [x for e in names for x in _words(e.value or "")]
    okved_words = [x for e in okved_names for x in _words(e.value or "")]
    raw, hits, used = 0.0, [], set()
    for s, from_profile in [*((s, False) for s in q_stems), *((s, True) for s in p_stems)]:
        k = float(cfg["profile_hit"]) if from_profile else 1.0
        if _hit(s, name_words, compound=True):
            raw += k * float(cfg["name_hit"])
            hits.append(f"«{s}» в названии" + (" (профиль ОКПД2)" if from_profile else ""))
            used.add("name")
        elif _hit(s, okved_words, compound=False):
            raw += k * float(cfg["okved_hit"])
            hits.append(f"«{s}» в ОКВЭД" + (" (профиль ОКПД2)" if from_profile else ""))
            used.add("okved")
    text = min(1.0, raw / float(cfg["text_saturation"]))
    src = "слова запроса" + (f" и профиля ОКПД2 {profile.code} ({'≥ ' if profile.capped else ''}{profile.positions} наименований позиций "
                             "в данных АИС ГЗ)" if profile else "; профиля ОКПД2 нет")
    text_why = (f"Текст: {text:.2f} — совпали {', '.join(hits)}" if hits
                else f"Текст: 0 — {src} не встречаются в названии и ОКВЭД по реестру МСП")
    text_ev = tuple([*(names if "name" in used else ()), *(okved_names if "okved" in used else ())])
    text_part = Part("text_match", round(text, 4), round(w_text, 4), text_why, text_ev)

    # minus_words
    q_low = _words(query_text) + list(p_stems)
    keys = [k for k in cfg.get("minus_words", {}) if okpd_class.startswith(k) or okved.startswith(k)]
    minus: list[str] = []
    for key in keys:
        for m in cfg["minus_words"][key]:
            m = str(m).lower()
            if any(x.startswith(m) or x[:PREFIX] == m[:PREFIX] for x in q_low):
                continue  # the query itself asks for it
            if any(x.startswith(m) for x in name_words + okved_words) and m not in minus:
                minus.append(m)
    pen = float(cfg["minus_penalty"]) if minus else 0.0
    minus_why = (f"Минус-слова класса: {', '.join(minus)} — в запросе их нет, балл × {1 - pen:.2f}" if minus
                 else "Минус-слов класса нет")
    minus_part = Part("minus_words", pen, 0.0, minus_why, tuple(names + okved_names) if minus else ())

    llm_part = Part("llm_check", 0.0, 0.0, "LLM-проверка не выполнялась (её доля распределена на ОКВЭД и текст)")
    value = round((w_ok * okved_part.value + w_text * text) * (1.0 - pen), 4)
    return NewFit(value=max(0.0, min(1.0, value)), parts=(okved_part, text_part, minus_part, llm_part),
                  depth=d, hits=tuple(hits), minus=tuple(minus))
