"""One table of regions (#292): every module reads src/regions.py, the copies agree with it."""

from __future__ import annotations

from src import regions as R
from src.enrich import discover, enrich
from src.match import filters, search
from src.sources import msp_dump


def test_table_is_consistent():
    codes = {r.code for r in R.REGIONS}
    assert all(r.neighbour in codes and R.NEIGHBOUR[r.neighbour] == r.code for r in R.REGIONS)  # symmetric
    assert all(r.in_ == f"в {r.prep}" for r in R.REGIONS)
    assert {R.REGION_CODE[n]: n for n in R.REGION_CODE} == R.REGION_NAME


def test_every_module_reads_the_same_table():
    assert filters.REGION_CODE is R.REGION_CODE and search.REGION_CODE is R.REGION_CODE
    assert search.REGION_NAME is R.REGION_NAME and discover.REGION_NAME is R.REGION_NAME
    assert msp_dump.REGION_NAME is R.REGION_NAME
    assert search.NEIGHBOUR is R.NEIGHBOUR and discover.NEIGHBOUR is R.NEIGHBOUR
    assert search.NEIGHBOUR_VALUE == discover.NEIGHBOUR_VALUE == R.NEIGHBOUR_VALUE


def test_names_by_code_as_before():
    assert enrich._region("780601001") == "Санкт-Петербург" and enrich._region("470301001") == "Ленинградская область"
    assert enrich._region("770101001") is None
    assert enrich._region_by_code("77") == "регион 77" and enrich._region_by_code(None) is None
    assert enrich._region_by_code("47") == "Ленинградская область"
