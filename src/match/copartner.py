"""Co-bidding of the suppliers on the shown page (#508, point 5): the partner that bids on the same ЭМ lots.

ЭМ data has every offer of a lot, so «with whom does this INN bid» is a fact of organizers' data
(`data:participants`), not a guess. Computed on the fly for the INNs of the shown page, never for all candidates;
a failure leaves the card without the fact and never breaks the search. The wording is neutral: a signal to check.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from src.contracts import CounterpartyMatch, Evidence
from src.match.reliability import plural
from src.match.store import get_store

log = logging.getLogger(__name__)

MIN_JOINT = 5  # shared ЭМ lots
MIN_SHARE = 0.5  # of the supplier's own ЭМ lots


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def _fact(partner: str, own: int, joint: int, wins: int) -> str:
    return (f"подаёт заявки вместе с ИНН {partner} в {_pct(joint / own)} своих лотов ЭМ ({joint} из {own}); "
            f"из совместных ИНН {partner} побеждает в {_pct(wins / joint)} "
            f"({wins} из {joint} {plural(joint, 'лота', 'лотов', 'лотов')}). "
            "Часто участвуют в одних лотах — сигнал для проверки, а не вывод о связи")


def with_cobid(matches: list[CounterpartyMatch], as_of: date | None) -> list[CounterpartyMatch]:
    """Add the co-bidding fact (Evidence + stats `cobid_*`) to the history matches above the threshold."""
    at = [(j, m.counterparty.inn) for j, m in enumerate(matches)
          if m.origin == "history" and m.counterparty.inn]
    if not at:
        return matches
    try:
        rows = get_store().cobid(sorted({inn for _, inn in at}), as_of)
    except Exception:  # noqa: BLE001 — a reference fact never breaks the page; data absent -> no fact
        log.exception("cobid: не посчитано для %d ИНН", len(at))
        return matches
    found = {r[0]: r for r in rows}
    now = datetime.now(UTC)
    out = list(matches)
    for j, inn in at:
        row = found.get(inn)
        if row is None:
            continue
        _, partner, own, joint, wins, partner_lots, last_lot = row
        own, joint, wins, partner_lots = int(own), int(joint), int(wins), int(partner_lots)
        if own <= 0 or joint < MIN_JOINT or joint / own < MIN_SHARE:
            continue
        cp = matches[j].counterparty
        ev = Evidence(source="data:participants", url=f"data:participants#{last_lot}", fetched_at=now,
                      fact=_fact(partner, own, joint, wins))
        stats = {**cp.stats, "cobid_partner_inn": partner, "cobid_lots": joint, "cobid_share": joint / own,
                 "cobid_jaccard": joint / (own + partner_lots - joint), "cobid_partner_win_rate": wins / joint}
        out[j] = matches[j].model_copy(
            update={"counterparty": cp.model_copy(update={"evidence": [*cp.evidence, ev], "stats": stats})})
    return out
