"""Backtest of one past lot on the demo (#192, D-084): where the real participants and the winner of the
lot land in our ranking, with the history cut at the split cutoff.

Only lots of the dev window [cutoff, holdout_from) of configs/split.yaml are shown: the index holds
publications strictly before the cutoff, so neither the lot nor anything after it leaks into the ranking
(opening a past lot as an ordinary query would find its own participants — circular, #145 F-13). A lot before
the cutoff is in the index; a lot from holdout is sealed (read only by `make eval-final`, D-010).
Relevance follows research/EVAL.md: ЭМ — every offer with a valid INN, АИС ГЗ — the winner(s) only.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel

from src.api.lots import LotNotFound, find_lot
from src.match import pipeline
from src.match.store import get_store

SPLIT = Path(__file__).resolve().parents[2] / "configs" / "split.yaml"
TOP = 10

_PARTICIPANTS = """
SELECT supplier_inn, bool_or(is_winner), min(publish_date) FROM {t[participants]}
WHERE lot_id = %(lot_id)s AND inn_valid GROUP BY supplier_inn ORDER BY supplier_inn
"""


class NotBacktestable(ValueError):
    """The lot cannot be shown as a backtest without a leak (the text goes to the client, 422)."""


class ParticipantRank(BaseModel):
    inn: str
    is_winner: bool
    rank: int | None  # 1-based place in our history block; None — not in the shown candidates


class Backtest(BaseModel):
    lot_id: int
    lot_date: date
    as_of: date  # the history of the ranking: strictly before this date
    channel: str | None
    ranked: int  # suppliers in our ranking
    participants: list[ParticipantRank]  # winner first, then by place
    winner_rank: int | None
    in_top: int  # participants in the top TOP
    top: int


def _split_dates() -> tuple[date, date]:
    cfg = yaml.safe_load(SPLIT.read_text(encoding="utf-8"))
    return date.fromisoformat(str(cfg["cutoff"])), date.fromisoformat(str(cfg["holdout_from"]))


def lot_backtest(ident: str) -> Backtest:
    """FileNotFoundError — no data; LotNotFound — no such lot; NotBacktestable — see the module docstring."""
    cutoff, holdout_from = _split_dates()
    store = get_store()
    store.manifest()  # StoreUnavailable (a FileNotFoundError) when the data is not built, before any query
    row = store.lot(ident.strip())
    if row is None:
        raise LotNotFound(ident)
    lot_id = int(row[0])
    _, rows = cast(Any, store)._fetch(_PARTICIPANTS, {"lot_id": lot_id})  # same SQL on parquet and Postgres
    if not rows:
        raise NotBacktestable("у закупки нет участников с верным ИНН в данных — сравнивать не с чем")
    lot_date = rows[0][2]
    if lot_date < cutoff:
        raise NotBacktestable(
            f"закупка от {lot_date:%d.%m.%Y} входит в историю, на которой строится поиск (до {cutoff:%d.%m.%Y}): "
            "её участники нашлись бы в собственных данных. Для проверки откройте закупку из периода "
            f"{cutoff:%d.%m.%Y}–{holdout_from:%d.%m.%Y}")
    if lot_date >= holdout_from:
        raise NotBacktestable("закупка из отложенного периода (holdout): он запечатан и читается только "
                              "итоговой оценкой make eval-final")
    query = find_lot(str(lot_id)).model_copy(update={"as_of": cutoff})
    channel = query.channel
    relevant = [(inn, bool(win)) for inn, win, _ in rows if win or channel == "ЭМ"]
    if not relevant:
        raise NotBacktestable("у закупки нет победителя с верным ИНН в данных — сравнивать не с чем")
    # the eval pipeline in its backtest mode: nothing from today's cache, no «Новые» (#220, D-097)
    found = pipeline.verified(pipeline.pick(query, backtest=True).history)
    order = [m.counterparty.inn for m in found if m.counterparty.inn]
    place = {inn: i + 1 for i, inn in reversed(list(enumerate(order)))}
    people = sorted((ParticipantRank(inn=inn, is_winner=win, rank=place.get(inn)) for inn, win in relevant),
                    key=lambda p: (not p.is_winner, p.rank is None, p.rank or 0, p.inn))
    winners = [p.rank for p in people if p.is_winner and p.rank is not None]
    return Backtest(
        lot_id=lot_id, lot_date=lot_date, as_of=cutoff, channel=channel, ranked=len(order), participants=people,
        winner_rank=min(winners) if winners else None,
        in_top=sum(p.rank is not None and p.rank <= TOP for p in people), top=TOP,
    )
