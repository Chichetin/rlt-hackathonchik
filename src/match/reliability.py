"""reliability — "is this a good supplier", in [0, 1] with one human line (#72, research/METHODOLOGY.md).

Organizers' data has no quality label (no execution, terminations, penalties). The component is
built from indirect signals, all strictly before `as_of` (no leak into eval):

Suppliers with history (`origin="history"`), from organizers' data (Store.supplier_history):
    win_rate  — wins / lots, Beta-smoothed to the pooled win rate of the query's candidates
                (similar suppliers): (wins + M·p0) / (lots + M), M = PRIOR_LOTS — 1 of 1 is not 1.0;
    retention — customers with ≥ 2 wins / customers with ≥ 1 win, smoothed the same way
                (prior strength PRIOR_CUSTOMERS): the customer came back;
    breadth   — distinct customers, log-saturated at SAT_CUSTOMERS: not a "pocket" supplier;
    tenure    — 0.5 · min(1, months since the first lot / TENURE_MONTHS)
              + 0.5 · active months / months since the first lot (regularity).
    reliability = Σ HISTORY_PARTS[i] · s_i.

Suppliers without history (open sources) — registry part only, from what enrich() put into the
Counterparty: status (active), not in РНП, age by the year in the ОГРН (digits 2–3 of the evidenced
ОГРН — the year of the record). Unknown signals are skipped; none known → None ("не проверено"),
never an invented value.

Whether the value enters the score is `reliability.mode` in configs/search.yaml (confirmation
of a human №3 — it changes the methodology):
    off    — as v1 (D-029): value 0, weight 0, "Нет данных";
    shadow — computed and shown with weight 0, the score and the ranking do not change (default);
    on     — weight `weights.reliability`.

`python -m src.match.reliability` — the sanity check of the issue: reliability before the cutoff
against wins and survival in the dev period [cutoff, holdout_from) — never the holdout.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from src.contracts import Counterparty

MODES = ("off", "shadow", "on")
DEFAULT_MODE = "off"  # when the config has no `reliability:` block
PRIOR_LOTS = 5  # Beta prior strength of the win rate, in lots
PRIOR_CUSTOMERS = 3  # Beta prior strength of the retention, in customers with a win
SAT_CUSTOMERS = 20  # breadth saturates at 20 distinct customers
TENURE_MONTHS = 12  # tenure saturates at a year in the data (the data starts 2024-01)
HISTORY_PARTS = {"win_rate": 0.35, "retention": 0.25, "breadth": 0.20, "tenure": 0.20}
REGISTRY_PARTS = {"status": 0.4, "rnp": 0.3, "age": 0.3}
AGE_YEARS = 5  # registry age saturates at 5 years
SHADOW_NOTE = " — в балл не входит (включение ждёт подтверждения методологии)"
NO_HISTORY = "Нет данных: истории участия до даты запроса нет"


@dataclass(frozen=True)
class History:
    """Supplier aggregates over lots published strictly before as_of (Store.supplier_history)."""

    lots: int
    wins: int
    customers: int
    won_customers: int  # customers with ≥ 1 winning lot
    repeat_customers: int  # customers with ≥ 2 winning lots
    active_months: int
    first_seen: date | None


@dataclass(frozen=True)
class Prior:
    win_rate: float
    retention: float


def reliability_mode(cfg: dict) -> str:
    raw = cfg.get("reliability") or {}
    value = raw.get("mode", DEFAULT_MODE)
    if isinstance(value, bool):  # YAML 1.1: bare on / off are booleans
        value = "on" if value else "off"
    mode = str(value).strip().lower()
    if mode not in MODES:
        raise ValueError(f"reliability.mode = {mode!r}: нужно {', '.join(MODES)}")
    return mode


def smooth(k: int, n: int, p0: float, m: float) -> float:
    """Posterior mean of a rate k/n under a Beta prior with mean p0 and strength m."""
    return (k + m * p0) / (n + m) if n + m > 0 else p0


def pool_prior(histories: Iterable[History]) -> Prior:
    """Pooled rates of the candidates of one query: the mean of "similar suppliers"."""
    hs = list(histories)
    lots, wins = sum(h.lots for h in hs), sum(h.wins for h in hs)
    won, rep = sum(h.won_customers for h in hs), sum(h.repeat_customers for h in hs)
    return Prior(win_rate=wins / lots if lots else 0.0, retention=rep / won if won else 0.0)


def _months(a: date, b: date) -> int:
    """Calendar months from a to b inclusive (a 2024-01-20, b 2024-03-01 → 3)."""
    return max(1, (b.year - a.year) * 12 + b.month - a.month + 1)


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def history_parts(h: History, prior: Prior, ref: date) -> dict[str, float]:
    """`ref` — the last day of the history: the day before as_of, or the end of the data."""
    s_win = smooth(h.wins, h.lots, prior.win_rate, PRIOR_LOTS)
    s_ret = smooth(h.repeat_customers, h.won_customers, prior.retention, PRIOR_CUSTOMERS)
    s_breadth = min(1.0, math.log1p(h.customers) / math.log1p(SAT_CUSTOMERS))
    if h.first_seen is None:
        s_tenure = 0.0
    else:
        span = _months(h.first_seen, ref) if h.first_seen <= ref else 1
        s_tenure = 0.5 * min(1.0, span / TENURE_MONTHS) + 0.5 * min(1.0, h.active_months / span)
    return {"win_rate": s_win, "retention": s_ret, "breadth": s_breadth, "tenure": s_tenure}


def history_reliability(h: History | None, prior: Prior, ref: date) -> tuple[float, str]:
    """(value, one line) for a supplier with history; no history → 0 and "Нет данных".

    `ref` — the last day of the history (the day before as_of, or the end of the data)."""
    if h is None or h.lots == 0:
        return 0.0, NO_HISTORY
    s = history_parts(h, prior, ref)
    value = sum(HISTORY_PARTS[k] * s[k] for k in HISTORY_PARTS)
    first = h.first_seen
    since = (f"в данных с {first.strftime('%m.%Y')}, активен {h.active_months} из {_months(first, ref)} мес."
             if first and first <= ref else "дата первого участия неизвестна")
    back = (f"повторные победы у {h.repeat_customers} из {h.won_customers} заказчик(ов) с победой"
            if h.won_customers else "побед у заказчиков ещё не было")
    why = (f"Побед {h.wins} из {h.lots} лот(ов) — {_pct(s['win_rate'])} с поправкой на малую выборку "
           f"(у похожих поставщиков {_pct(prior.win_rate)}); {back}; всего заказчиков {h.customers}; {since}")
    return value, why


def ogrn_year(ogrn: str, ref: date) -> int | None:
    """Year of the ОГРН/ОГРНИП record: digits 2–3 (ОГРН exists since 2002); None if not plausible."""
    if not (ogrn.isdigit() and len(ogrn) in (13, 15)):
        return None
    year = 2000 + int(ogrn[1:3])
    return year if 2002 <= year <= ref.year else None


def _is_active(status: str) -> bool:
    s = status.lower()
    return s.startswith("действ") or s == "active"


def registry_reliability(cp: Counterparty, ref: date) -> tuple[float | None, str]:
    """Registry part for a supplier without history: only facts enrich() put into the card.

    None — nothing is known ("не проверено"), never a guessed value.
    """
    parts: dict[str, float] = {}
    said: list[str] = []
    if cp.status:
        parts["status"] = 1.0 if _is_active(cp.status) else 0.0
        said.append(f"статус по реестру: {cp.status}")
    if cp.in_rnp is not None:
        parts["rnp"] = 0.0 if cp.in_rnp else 1.0
        said.append("в реестре недобросовестных поставщиков" if cp.in_rnp else "в РНП не найден")
    ogrn = next((e.value for e in cp.evidence if e.field == "ogrn" and e.value), None)
    year = ogrn_year(ogrn, ref) if ogrn else None
    if year is not None:
        age = ref.year - year
        parts["age"] = min(1.0, age / AGE_YEARS)
        said.append(f"запись в реестре с {year} г. (по ОГРН), {age} лет")
    if not parts:
        return None, "Не проверено: статус, РНП и возраст по реестрам ещё не получены"
    total = sum(REGISTRY_PARTS[k] for k in parts)
    value = sum(REGISTRY_PARTS[k] * v for k, v in parts.items()) / total
    missing = [n for k, n in (("status", "статус"), ("rnp", "РНП"), ("age", "возраст")) if k not in parts]
    tail = f"; не проверено: {', '.join(missing)}" if missing else ""
    return value, "Истории в данных нет; " + "; ".join(said) + tail


# --- sanity check (#72): reliability before the cutoff vs outcomes in the dev period -------------


def _ranks(x: list[float]):
    """Ranks with ties averaged (as scipy.stats.rankdata, without scipy)."""
    import numpy as np

    a = np.asarray(x, dtype=float)
    _, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    upper = np.cumsum(counts)
    return ((upper - counts + 1 + upper) / 2.0)[inv]


def spearman(x: list[float], y: list[float]) -> float:
    import numpy as np

    return float(np.corrcoef(_ranks(x), _ranks(y))[0, 1])


def check(min_after: int = 3, quantiles: int = 5) -> dict:
    """Quantiles of reliability as of the cutoff (global prior) vs win rate and survival after it.

    Outcome window: [cutoff, holdout_from) of configs/split.yaml — the holdout is never read.
    """
    from src.eval.split import split_config
    from src.match.store import ParquetStore, _pq, processed_dir

    cfg = split_config()
    cutoff, until = date.fromisoformat(str(cfg["cutoff"])), date.fromisoformat(str(cfg["holdout_from"]))
    store = ParquetStore(processed_dir())
    store.manifest()
    import duckdb

    part = _pq(store.processed / "participants.parquet")
    inns = [r[0] for r in duckdb.sql(
        f"SELECT DISTINCT supplier_inn FROM {part} WHERE publish_date < DATE '{cutoff}'").fetchall()]
    hist = store.supplier_history(inns, cutoff)
    after = {r[0]: (int(r[1]), int(r[2])) for r in duckdb.sql(
        f"""SELECT supplier_inn, count(DISTINCT lot_id), count(DISTINCT lot_id) FILTER (WHERE is_winner)
            FROM {part} WHERE publish_date >= DATE '{cutoff}' AND publish_date < DATE '{until}'
            GROUP BY supplier_inn""").fetchall()}
    prior = pool_prior(hist.values())
    last = cutoff - timedelta(days=1)
    rel = {inn: history_reliability(h, prior, last)[0] for inn, h in hist.items()}
    raw = {inn: h.wins / h.lots for inn, h in hist.items() if h.lots}

    def table(keys: list[str], score: dict[str, float], outcome) -> list[dict]:
        ordered = sorted(keys, key=lambda k: (score[k], k))
        rows = []
        for q in range(quantiles):
            chunk = ordered[q * len(ordered) // quantiles:(q + 1) * len(ordered) // quantiles]
            vals = [outcome(k) for k in chunk]
            rows.append({"q": q + 1, "n": len(chunk), "reliability_min": round(score[chunk[0]], 3),
                         "reliability_max": round(score[chunk[-1]], 3),
                         "outcome": round(sum(vals) / len(vals), 4)})
        return rows

    active = [k for k in hist if after.get(k, (0, 0))[0] >= min_after]

    def win_after(k: str) -> float:
        return after[k][1] / after[k][0]

    def survived(k: str) -> float:
        return 1.0 if k in after else 0.0

    everyone = list(hist)
    return {
        "cutoff": cutoff.isoformat(), "outcome_until": until.isoformat(),
        "prior": {"win_rate": round(prior.win_rate, 4), "retention": round(prior.retention, 4)},
        "suppliers_before": len(hist), "with_min_lots_after": len(active), "min_after": min_after,
        "win_rate_after_by_quantile": table(active, rel, win_after),
        "spearman_win_after": {"reliability": round(spearman([rel[k] for k in active],
                                                              [win_after(k) for k in active]), 4),
                               "raw_win_rate": round(spearman([raw[k] for k in active],
                                                               [win_after(k) for k in active]), 4)},
        "survival_by_quantile": table(everyone, rel, survived),
        "spearman_survival": {"reliability": round(spearman([rel[k] for k in everyone],
                                                             [survived(k) for k in everyone]), 4),
                              "raw_win_rate": round(spearman([raw[k] for k in everyone],
                                                              [survived(k) for k in everyone]), 4)},
    }


def main(argv: list[str] | None = None) -> int:
    import json

    ap = argparse.ArgumentParser(description="reliability до cutoff против исходов dev-периода (#72)")
    ap.add_argument("--min-after", type=int, default=3, help="минимум лотов после cutoff для доли побед")
    ap.add_argument("--quantiles", type=int, default=5)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print(json.dumps(check(args.min_after, args.quantiles), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
