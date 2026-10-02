"""One notice of organizers' data -> Query: the rule for training lots (/api/lot) and test exports (#294 п. 2).

Lives in the data layer: src/api/lots.py (lots of the store) and src/ingest/testset.py (the uploaded test export)
both build the query here, so the test export no longer imports the API (D-106).
"""

from __future__ import annotations

from src.contracts import Query


def is_template(subject: str | None) -> bool:
    # DATA_DICTIONARY: in АИС ГЗ subject is sometimes «Закупка … частью 12 статьи 93» instead of the item
    s = (subject or "").lower()
    return not s.strip() or ("закупка" in s and "статьи 93" in s)


def notice_query(lot_id, subject, procedure_name, price, customer_inn, channel, *, okpd2: list[str],
                 names: list[str] | None = None) -> Query:
    # subject, else procedure_name, else the lot's position names (both are «ч. 12 ст. 93» templates in ~840 lots)
    if is_template(subject):
        if not is_template(procedure_name):
            subject = procedure_name
        elif names:
            subject = "; ".join(names)
        elif procedure_name and str(procedure_name).strip():
            subject = procedure_name
    try:
        budget = float(price) if price not in (None, "") else None
    except ValueError:
        budget = None
    return Query(
        query_id=f"lot-{lot_id}",
        text=(subject or "").strip() or f"Закупка {lot_id}",
        okpd2=okpd2,
        budget=budget,
        customer_inn=customer_inn or None,
        channel=channel if channel in ("ЭМ", "АИС ГЗ") else None,  # type: ignore[arg-type]
    )
