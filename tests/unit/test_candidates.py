"""Additional candidate quotas count suppliers not already retrieved from an earlier source."""

from datetime import date
from pathlib import Path

import pytest

from src.match.candidates import extra
from src.match.store import ParquetStore

CUTOFF = date(2025, 7, 1)
PAPER = ["17.12.14.110"]
CUSTOMER = "7801000051"


@pytest.mark.parametrize(("quotas", "have", "expected"), [
    ({"customer": 1}, {"7801000012"}, ["780100000090"]),
    ({"okpd2": 2}, {"7801000012"}, ["4701000038", "780100000090"]),
    # The category quota also skips the supplier just added from the customer's history.
    ({"customer": 1, "okpd2": 1}, {"7801000012"}, ["780100000090", "4701000038"]),
    # Existing suppliers outside these groups must not make either source exceed its quota.
    ({"customer": 1, "okpd2": 1}, {"unrelated"}, ["7801000012", "4701000038"]),
])
def test_each_source_fills_its_quota_with_new_suppliers(fixture_processed: Path, quotas, have, expected):
    before = set(have)
    got = extra(ParquetStore(fixture_processed), have, PAPER, CUSTOMER, CUTOFF, {"candidates": quotas})
    assert [c.inn for c in got] == expected
    assert have == before


def test_exhausted_sources_keep_cutoff_and_own_lot_exclusion(fixture_processed: Path):
    # Customer's only paper lot before cutoff is lot 1. The category can still find lot 3;
    # neither the supplier seen only in excluded lot 1 nor the one first seen after cutoff may join.
    got = extra(ParquetStore(fixture_processed), {"7801000012"}, PAPER, CUSTOMER, CUTOFF,
                {"candidates": {"customer": 10, "okpd2": 10}}, own=1)
    assert [c.inn for c in got] == ["4701000038", "7801000013"]
    assert all(c.best_lot == 3 and c.best_date < CUTOFF for c in got)
