"""Role of a counterparty — manufacturer / distributor / dealer — by its main OKVED2. Owner: SOURCES.

Rule v1 (D-057, research/METHODOLOGY.md «Роль контрагента»), no LLM, by the first digits of the code:
10–33 (section C) → manufacturer; 46.1 → dealer (agents); other 46 → distributor; 45, 47 → dealer;
anything else → unknown; no code → None («роль не определена»). The main OKVED is what the company
declared to the registry, not a proof of production. The role is NOT part of the score.

The basis is an Evidence of its own: `field="okved"`, `value` = the page line `<key>: <code>` that holds
the code (`vyp.КодОКВЭД: 33.12`, `<inn>.okved1: 17.12`). src/enrich/evidence.py keeps `kind` only when
such a line passes the page check and the rule applied to its code gives the same kind.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from src.contracts import Kind

LABEL: dict[Kind, str] = {"manufacturer": "производитель", "distributor": "дистрибьютор", "dealer": "дилер",
                          "unknown": "не определена"}
_CODE = re.compile(r"\d{2}(?:\.\d{1,2}){0,3}", re.ASCII)
_LINE = re.compile(r"[^\s:]+: (\d{2}(?:\.\d{1,2}){0,3})", re.ASCII)  # `<key>: <code>`, the whole value


def classify(okved: str | None) -> tuple[Kind, str] | None:
    """Main OKVED2 code → (kind, why); None when there is no code to judge by."""
    code = (okved or "").strip()
    if not _CODE.fullmatch(code):
        return None
    group = int(code[:2])
    if 10 <= group <= 33:
        return "manufacturer", "обрабатывающее производство (раздел C, коды 10–33)"
    if code.startswith("46.1"):
        return "dealer", "торговля оптовая за вознаграждение или на договорной основе — агент, посредник"
    if group == 46:
        return "distributor", "оптовая торговля"
    if group == 45:
        return "dealer", "торговля автотранспортными средствами — перепродавец"
    if group == 47:
        return "dealer", "розничная торговля — перепродавец"
    return "unknown", "не производство и не торговля"


def line_kind(value: str | None) -> Kind | None:
    """Kind backed by an Evidence value that is a `<key>: <code>` page line; None for anything else."""
    m = _LINE.fullmatch(value.strip()) if value else None
    role = classify(m.group(1)) if m else None
    return role[0] if role else None


def assign(code: str | None, name: str | None, line: str, add: Callable[[str, str, str], bool]) -> Kind | None:
    """Kind by the main OKVED and its basis added as an Evidence (`add(field, value, fact)` of the
    caller, `line` — the page line holding the code). None: no code, or the fact was not added."""
    role = classify(code)
    if role is None:
        return None
    kind, why = role
    head = f"Роль — {LABEL[kind]}" if kind != "unknown" else "Роль не определена"
    named = f" «{name}»" if name else ""
    return kind if add("okved", line, f"{head}: заявленный основной ОКВЭД {code}{named} — {why}") else None
