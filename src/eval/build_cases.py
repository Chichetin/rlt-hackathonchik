"""Eval cases from organizers' lots (T-07, research/EVAL.md A and D).

    uv run python -m src.eval.build_cases [--processed data/processed] [--sample 2000] [--seed 20261001]

Lot -> EvalCase:
  query     text = subject (procedure_name if subject is the «…статьи 93» template) + up to MAX_ITEMS
            distinct position names; okpd2 = position codes; customer_inn, budget = НМЦК, channel;
            as_of = split cutoff for every part (features only before cutoff, one index — D-018);
            query_id = "lot-<lot_id>" (as GET /api/lot).
  relevant  ЭМ: every offer (winner grade 2, other offers grade 1); АИС ГЗ: the winner(s) (grade 2) —
            the data holds only the contractor there. INNs failing the checksum are never relevant:
            search can not return them (inn=None, src/ingest/load.py).
  excluded  in order: no participants; АИС ГЗ «ч.12 ст.93» / «единственный поставщик» in subject or
            procedure_name (a direct contract, nobody was chosen among offers); no valid INN; no text.

Parts (configs/split.yaml, paths via src/eval/split.py):
  dev_full  every dev case, [cutoff, holdout_from) — outside git (data/processed/eval/);
  dev       stratified sample (channel x OKPD2 class of the lot, seed) — in git, < 5 MB;
  real      5–10 readable dev cases, both channels, distinct OKPD2 classes — demo and case review;
  holdout   from holdout_from — outside git, sealed: written here, read only by `make eval-final`.
Counts — build_stats.json next to dev (holdout: counts only, never metrics).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from src.contracts import Query
from src.eval.split import ROOT, EvalCase, split_config, write_cases

MAX_ITEMS = 10  # position names in Query.text; lots have p90 = 8 positions (D-018)
SAMPLE_N = 2000
SEED = 20261001
REAL_PER_CHANNEL = 4
EXCLUSIONS = ("no_participants", "aisgz_st93", "no_valid_inn", "no_text")

# Direct contract markers (research/DATA_DICTIONARY.md): «частью 12 статьи 93», «у единственного поставщика».
ST93 = re.compile(r"част\w*\s+12\s+стать\w*\s+93|единственн\w*\s+поставщик")
TEMPLATE = re.compile(r"стать\w*\s+93")  # subject is a legal template, not the item


@dataclass
class Lot:
    lot_id: int
    date: date
    channel: str
    subject: str | None
    procedure_name: str | None
    customer_inn: str | None
    start_price: float | None
    items: list[tuple[str | None, str | None]]  # (product_name, okpd2_code), ordered
    participants: list[tuple[str, bool, bool]]  # (inn, is_winner, inn_valid), ordered by INN

    @property
    def main_class(self) -> str | None:
        """Most frequent OKPD2 class (2 digits) of the positions; ties -> smaller code."""
        cnt = Counter(c[:2] for _, c in self.items if c and len(c) >= 2)
        return min(cnt, key=lambda k: (-cnt[k], k)) if cnt else None


def _is_template(s: str | None) -> bool:
    return not (s or "").strip() or bool(TEMPLATE.search(s.lower()))  # type: ignore[union-attr]


def query_text(subject: str | None, procedure_name: str | None, names: Sequence[str | None]) -> str:
    head = next((s.strip() for s in (subject, procedure_name) if not _is_template(s)), "")  # type: ignore[union-attr]
    parts, seen = ([head], {head.casefold()}) if head else ([], set())
    for name in names:
        n = (name or "").strip()
        if len(parts) - bool(head) >= MAX_ITEMS:
            break
        if n and n.casefold() not in seen:
            seen.add(n.casefold())
            parts.append(n)
    return "; ".join(parts)


def _relevant(lot: Lot) -> dict[str, int]:
    """INN -> grade. ЭМ: offers 1, winner 2; АИС ГЗ: winners only, grade 2."""
    out = {}
    for inn, win, valid in lot.participants:
        if valid and (win or lot.channel == "ЭМ"):
            out[inn] = 2 if win else 1
    return out


def exclusion(lot: Lot) -> str | None:
    if not lot.participants:
        return "no_participants"
    if lot.channel == "АИС ГЗ" and any(ST93.search((s or "").lower()) for s in (lot.subject, lot.procedure_name)):
        return "aisgz_st93"
    if not _relevant(lot):
        return "no_valid_inn"
    if not query_text(lot.subject, lot.procedure_name, [n for n, _ in lot.items]):
        return "no_text"
    return None


def to_case(lot: Lot, as_of: date) -> EvalCase:
    grades = _relevant(lot)
    okpd2 = sorted({c for _, c in lot.items if c})
    query = Query(
        query_id=f"lot-{lot.lot_id}",
        text=query_text(lot.subject, lot.procedure_name, [n for n, _ in lot.items]),
        okpd2=okpd2,
        budget=lot.start_price,
        customer_inn=lot.customer_inn,
        channel=lot.channel,  # type: ignore[arg-type]
        as_of=as_of,
    )
    return EvalCase(query=query, relevant_inns=sorted(grades), date=lot.date, grades=grades)


def read_lots(processed: Path, start: date, end: date | None = None) -> list[Lot]:
    """Every lot published on or after `start` (no earlier lot is ever a case) and before `end`, ordered by lot_id."""
    def pq(name: str) -> str:
        return "'" + (processed / f"{name}.parquet").as_posix().replace("'", "''") + "'"

    rows = duckdb.sql(
        f"""
        WITH it AS (
            SELECT lot_id,
                   list(struct_pack(n := product_name, c := okpd2_code) ORDER BY okpd2_code, product_name) AS items
            FROM {pq("items")} GROUP BY lot_id
        ), pt AS (
            SELECT lot_id,
                   list(struct_pack(i := supplier_inn, w := is_winner, v := inn_valid) ORDER BY supplier_inn)
                       AS participants
            FROM {pq("participants")} GROUP BY lot_id
        )
        SELECT n.lot_id, n.publish_date, n.channel, n.subject, n.procedure_name, n.customer_inn,
               n.start_price, coalesce(it.items, []), coalesce(pt.participants, [])
        FROM {pq("notices")} n LEFT JOIN it USING (lot_id) LEFT JOIN pt USING (lot_id)
        WHERE n.publish_date >= DATE '{start.isoformat()}'
          {f"AND n.publish_date < DATE '{end.isoformat()}'" if end else ""}
        ORDER BY n.lot_id
        """
    ).fetchall()
    return [
        Lot(r[0], r[1], r[2], r[3], r[4], r[5], r[6],
            [(x["n"], x["c"]) for x in r[7]],
            [(x["i"], bool(x["w"]), bool(x["v"])) for x in r[8]])
        for r in rows
    ]


def _h(seed: int, lot_id: int) -> str:
    return hashlib.sha256(f"{seed}:{lot_id}".encode()).hexdigest()


def stratified_sample(keys: list[tuple[str, int]], n: int, seed: int) -> set[int]:
    """Proportional allocation over strata (largest remainder), lowest seeded hash inside a stratum."""
    if n >= len(keys):
        return {lid for _, lid in keys}
    groups: dict[str, list[int]] = defaultdict(list)
    for s, lid in keys:
        groups[s].append(lid)
    quota = {s: n * len(g) / len(keys) for s, g in groups.items()}
    alloc = {s: int(q) for s, q in quota.items()}
    for s in sorted(groups, key=lambda s: (alloc[s] - quota[s], s))[: n - sum(alloc.values())]:
        alloc[s] += 1
    return {lid for s, g in groups.items() for lid in sorted(g, key=lambda x: _h(seed, x))[: alloc[s]]}


def _readable(lot: Lot) -> bool:
    """A real case to show and discuss: plain subject, one OKPD2 class, SPb customer, a real choice."""
    subj = (lot.subject or "").strip()
    winners = sum(1 for _, w, _ in lot.participants if w)
    choice = len(lot.participants) >= 3 and winners == 1 if lot.channel == "ЭМ" else winners == 1
    return (
        choice
        and not _is_template(subj)
        and 10 <= len(subj) <= 90
        and 1 <= len(lot.items) <= 5
        and len({c[:2] for _, c in lot.items if c}) == 1
        and (lot.customer_inn or "").startswith("78")
        and lot.start_price is not None
    )


def pick_real(lots: list[Lot], per_channel: int, seed: int) -> list[Lot]:
    """Classes by frequency among dev cases; per class one lot of the channel with fewer picks."""
    freq = Counter(lot.main_class for lot in lots if lot.main_class)
    pool: dict[tuple[str, str], list[Lot]] = defaultdict(list)
    for lot in lots:
        if _readable(lot) and lot.main_class:
            pool[(lot.channel, lot.main_class)].append(lot)
    picked: dict[str, list[Lot]] = {"ЭМ": [], "АИС ГЗ": []}
    for cls in sorted(freq, key=lambda c: (-freq[c], c)):
        for ch in sorted(picked, key=lambda ch: (len(picked[ch]), ch != "ЭМ")):
            if len(picked[ch]) < per_channel and pool[(ch, cls)]:
                picked[ch].append(min(pool[(ch, cls)], key=lambda x: _h(seed, x.lot_id)))
                break
        if all(len(v) >= per_channel for v in picked.values()):
            break
    return sorted(picked["ЭМ"] + picked["АИС ГЗ"], key=lambda x: x.lot_id)


def build_all(processed: Path, cfg: dict, sample_n: int = SAMPLE_N, seed: int = SEED,
              real_per_channel: int = REAL_PER_CHANNEL) -> dict:
    cutoff, holdout_from = (date.fromisoformat(str(cfg[k])) for k in ("cutoff", "holdout_from"))
    windows = {"dev": (cutoff, holdout_from), "holdout": (holdout_from, date.max)}
    kept: dict[str, list[Lot]] = {"dev": [], "holdout": []}
    stats: dict = {"as_of": cutoff.isoformat(), "holdout_from": holdout_from.isoformat(), "parts": {}}
    lots = read_lots(processed, cutoff)
    for part, (lo, hi) in windows.items():
        window = [lot for lot in lots if lo <= lot.date < hi]
        excluded = Counter(r for r in map(exclusion, window) if r)
        kept[part] = [lot for lot in window if exclusion(lot) is None]
        stats["parts"][part] = {
            "lots": len(window),
            "lots_by_channel": dict(Counter(lot.channel for lot in window)),
            "excluded": {r: excluded.get(r, 0) for r in EXCLUSIONS},
            "excluded_aisgz_st93_with_items": sum(1 for lot in window if exclusion(lot) == "aisgz_st93"
                                                  and lot.items),
            "cases": len(kept[part]),
            "by_channel": {ch: sum(1 for lot in kept[part] if lot.channel == ch) for ch in ("ЭМ", "АИС ГЗ")},
            "em_without_winner": sum(1 for lot in kept[part]
                                     if lot.channel == "ЭМ" and not any(w for _, w, _ in lot.participants)),
        }
    dev_cases = {lot.lot_id: to_case(lot, cutoff) for lot in kept["dev"]}
    write_cases("dev_full", list(dev_cases.values()), cfg)
    write_cases("holdout", [to_case(lot, cutoff) for lot in kept["holdout"]], cfg)
    chosen = stratified_sample([(f"{lot.channel}|{lot.main_class or '-'}", lot.lot_id) for lot in kept["dev"]],
                               sample_n, seed)
    sample = [dev_cases[lid] for lid in sorted(chosen)]
    dev_path = write_cases("dev", sample, cfg)
    real = pick_real(kept["dev"], real_per_channel, seed)
    write_cases("real", [dev_cases[lot.lot_id] for lot in real], cfg)
    stats["sample"] = {
        "n": len(sample), "seed": seed, "strata": "channel x main OKPD2 class (2 digits)",
        "by_channel": dict(Counter(c.query.channel for c in sample)),
    }
    stats["real"] = [{"query_id": f"lot-{lot.lot_id}", "channel": lot.channel, "class": lot.main_class,
                      "subject": lot.subject} for lot in real]
    stats["max_items"] = MAX_ITEMS
    dev_path.with_name("build_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
                                                      encoding="utf-8")
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--processed", type=Path,
                    default=Path(os.environ.get("DATA_PROCESSED_DIR") or ROOT / "data" / "processed"))
    ap.add_argument("--sample", type=int, default=SAMPLE_N)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)
    if not (args.processed / "notices.parquet").exists():
        print(f"{args.processed}/notices.parquet нет: сначала `uv run python -m src.ingest.build`", file=sys.stderr)
        return 2
    stats = build_all(args.processed, split_config(), args.sample, args.seed)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
