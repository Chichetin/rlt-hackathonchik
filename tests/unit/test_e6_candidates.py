"""E6 pool audits use real search/store on synthetic parquet.

The trace contains confirmed INNs that can match evaluation labels. Training retains its
existing rows with INN=None for invalid identifiers; those rows do not belong to the audit.
"""

from datetime import date

import pytest

from src.contracts import Query
from src.match import ranker
from src.match.search import load_config, search
from src.match.store import ParquetStore

pytestmark = pytest.mark.usefixtures("v2_formula")

CUTOFF = date(2025, 7, 1)
TEXT = "7801000012"
CUSTOMER_EXTRA = {"7801000020", "780100000090"}
GROUP_EXTRA = "4701000038"
UNRELATED = "470100000088"  # repairs before cutoff; paper participation only after cutoff


def query(query_id: str = "e6-pool", as_of: date | None = CUTOFF) -> Query:
    # Water matches only TEXT; the explicit paper code lets the other two sources add suppliers.
    return Query(query_id=query_id, text="Вода питьевая", okpd2=["17.12.14.110"],
                 customer_inn="7801000051", as_of=as_of)


def config(okpd2: int = 20) -> dict:
    return {**load_config(), "candidates": {"customer": 30, "okpd2": okpd2}}


def test_full_trace_and_training_features_include_extras_before_top_k(search_data):
    cfg = config()
    baseline = search(query(), k=50, config={**cfg, "candidates": {}})
    assert {m.counterparty.inn for m in baseline} == {TEXT}

    trace: list[str] = []
    features: list[tuple[str | None, list[float]]] = []
    matches = search(query(), k=1, config=cfg, features_out=features, candidate_inns_out=trace)

    expected = {TEXT, *CUSTOMER_EXTRA, GROUP_EXTRA}
    assert len(matches) == 1
    identified = [inn for inn, _ in features if inn is not None]
    assert set(trace) == expected == set(identified)
    assert None not in trace
    assert any(inn is None for inn, _ in features)  # invalid fixture INN remains an unidentified training row
    assert len(trace) == len(set(trace)) == len(identified)  # overlapping sources never duplicate identified rows
    assert CUSTOMER_EXTRA - {m.counterparty.inn for m in matches}
    rows = {inn: dict(zip(ranker.NAMES, row, strict=True)) for inn, row in features if inn is not None}
    assert rows[TEXT]["bm25"] > 0
    for inn in CUSTOMER_EXTRA | {GROUP_EXTRA}:
        assert rows[inn]["bm25"] == 0
    assert all(rows[inn]["cust_lots"] > 0 for inn in CUSTOMER_EXTRA)

    # Pool audit alone must not depend on collecting training features or on the requested top-k.
    audit_only: list[str] = []
    full = search(query(), k=50, config=cfg, candidate_inns_out=audit_only)
    assert set(audit_only) == expected == {m.counterparty.inn for m in full if m.counterparty.inn is not None}


def test_customer_union_does_not_inject_suppliers_without_a_retrieval_source(search_data):
    trace: list[str] = []
    search(query(), k=1, config=config(okpd2=0), candidate_inns_out=trace)

    assert set(trace) == {TEXT, *CUSTOMER_EXTRA}
    # GROUP_EXTRA has paper history at another customer and repair history at this customer.
    # Neither supplies a matching customer+group lot, so knowing its INN cannot insert it.
    assert GROUP_EXTRA not in trace
    assert UNRELATED not in trace


def test_own_lot_is_excluded_from_customer_and_group_candidate_sources(search_data):
    trace: list[str] = []
    features: list[tuple[str | None, list[float]]] = []
    search(query(query_id="lot-1"), k=1, config=config(), features_out=features, candidate_inns_out=trace)

    # Lot 1 is the only eligible paper lot of CUSTOMER_EXTRA. Other pre-cutoff lots of one
    # supplier contain cartridges, and the other's next paper lot is after cutoff.
    assert set(trace) == {TEXT, GROUP_EXTRA} == {inn for inn, _ in features if inn is not None}
    assert not CUSTOMER_EXTRA.intersection(trace)


def test_future_matching_history_cannot_insert_an_existing_unrelated_supplier(search_data):
    before: list[str] = []
    all_dates: list[str] = []
    search(query(), k=1, config=config(), candidate_inns_out=before)
    search(query(as_of=None), k=1, config=config(), candidate_inns_out=all_dates)

    # This supplier already exists in the cutoff snapshot, so supplier_rows cannot mask a
    # temporal leak in the source query. Only its future paper lot could put it in the pool.
    store = ParquetStore(search_data)
    assert store.supplier_rows([UNRELATED], CUTOFF)
    assert UNRELATED not in before
    assert UNRELATED in all_dates


def test_no_candidates_leave_trace_and_features_empty(search_data):
    trace: list[str] = []
    features: list[tuple[str | None, list[float]]] = []
    matches = search(query(as_of=date(2020, 1, 1)), k=1, config=config(),
                     features_out=features, candidate_inns_out=trace)

    assert matches == []
    assert trace == []
    assert features == []


@pytest.mark.parametrize("source", ["customer", "okpd2"])
def test_extra_source_cutoff_is_strict_and_excludes_own_lot(fixture_processed, source):
    store = ParquetStore(fixture_processed)

    def suppliers(as_of: date, exclude_lot: int = -1) -> set[str]:
        if source == "customer":
            rows = store.customer_suppliers("7801000051", ["17.12"], as_of, 30, exclude_lot)
        else:
            rows = store.okpd2_suppliers(2, ["17.12"], as_of, 20, exclude_lot)
        return {row[0] for row in rows}

    # The first paper lot is published exactly on March 1: neither source may use it that day.
    assert suppliers(date(2024, 3, 1)) == set()
    assert suppliers(date(2024, 3, 2)) == {TEXT, *CUSTOMER_EXTRA}
    assert suppliers(date(2024, 3, 2), exclude_lot=1) == set()
