"""Extra candidates beyond the text match (#195). Owner: MATCH.

search() takes candidates only from lots similar by text: the true participant is among them in ~77% of
dev cases (eval/results/emb-bm25-s1.json, #115). Two more sources, off by default (`candidates:` in the
config, `{customer: N, okpd2: M}` = how many to add from each):
  customer — suppliers of THIS customer's lots in the query's OKPD2 groups (XX.XX) before as_of: 57.85% of
             2025 wins go to a supplier who already won at the same customer (research/PROBLEM.md);
  okpd2    — the most frequent suppliers of the query's OKPD2 groups before as_of.
Added candidates have BM25 = 0 (product_fit gets only the OKPD2 part) and say where they come from in the
first phrase of product_fit. A supplier already found by text is never duplicated.
"""

from __future__ import annotations

from datetime import date

from src.match.store import Store, levels


def sizes(cfg: dict) -> tuple[int, int]:
    """(customer, okpd2) from `candidates:`; unknown keys or negative sizes are an error."""
    raw = cfg.get("candidates") or {}
    if unknown := set(raw) - {"customer", "okpd2"}:
        raise ValueError(f"неизвестные источники кандидатов: {', '.join(sorted(unknown))}")
    out = int(raw.get("customer", 0)), int(raw.get("okpd2", 0))
    if min(out) < 0:
        raise ValueError(f"candidates: размеры должны быть ≥ 0, получено {raw}")
    return out


def _groups(codes: list[str]) -> list[str]:
    return sorted({g for g in (levels(c)[2] for c in codes) if g})


def extra(store: Store, have: set[str], codes: list[str], customer_inn: str | None, as_of: date | None,
          cfg: dict) -> list:
    """Candidates from the customer's history and from the OKPD2 groups, not already in `have`."""
    from src.match.search import Candidate

    n_customer, n_okpd2 = sizes(cfg)
    groups = _groups(codes)
    if not groups or not (n_customer or n_okpd2):
        return []
    out: list[Candidate] = []
    seen = set(have)

    def add(rows: list[tuple], why: str) -> None:
        for inn, n_lots, n_wins, lot_id, text, day in rows:
            if inn in seen:
                continue
            seen.add(inn)
            c = Candidate(inn, 0.0, int(n_lots), int(n_wins), int(lot_id), text or "", day)
            c.found_by = why.format(n=int(n_lots))
            out.append(c)

    g = ", ".join(groups)
    if n_customer and customer_inn:
        add(store.customer_suppliers(customer_inn, groups, as_of, n_customer),
            f"Не найден по названию; поставщик этого заказчика: {{n}} лот(ов) в группе ОКПД2 {g}")
    if n_okpd2:
        add(store.okpd2_suppliers(2, groups, as_of, n_okpd2),
            f"Не найден по названию; частый поставщик группы ОКПД2 {g}: {{n}} лот(ов)")
    return out
