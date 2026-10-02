"""Purchase type of #188 — the boundary classes of the rule and the exceptions table (acceptance of #188).

The implementation is the one of #242/#104 (src/match/purchase_type.py); tests/unit/test_purchase_type.py covers
the voting, the remote services and the labels — here only what #188 accepts: the class boundaries 33/41/43/45
and the table of exceptions shown to the user.
"""

import pytest

from src.match.purchase_type import GOODS_CLASSES, GOODS_SUBCLASSES, code_type, purchase_type


@pytest.mark.parametrize("code, kind", [
    ("32.50.13.190", "goods"), ("33", "goods"), ("33.20.29.000", "goods"),  # 33 is the last goods class (section C)
    ("41", "works"), ("41.10.10.000", "works"),  # 41 opens works (section F)
    ("43", "works"), ("43.99.90.190", "works"),  # 43 closes works
    ("45", "services"), ("45.11.10.000", "services"), ("45.20.11.200", "services"),  # 45: trade and repair of
    # vehicles — not in the exceptions, a service
])
def test_boundary_classes_33_41_43_45(code, kind):
    assert code_type(code) == kind


def test_exceptions_table_is_the_one_of_188():
    assert GOODS_CLASSES == {46, 47}
    assert GOODS_SUBCLASSES == {"58.11", "58.14", "58.19"}


@pytest.mark.parametrize("code, kind", [
    ("46", "goods"), ("46.90.10.000", "goods"), ("47", "goods"), ("47.99.10.000", "goods"),  # the whole class
    ("58.11", "goods"), ("58.14", "goods"), ("58.19", "goods"),  # the subclass itself, no deeper level
    ("58", "remote"), ("58.1", "remote"), ("58.12.10.000", "remote"),  # class 58 outside the three subclasses
    ("48.00", "services"),  # the neighbour of the trade classes is not an exception
])
def test_exceptions_cover_the_whole_class_or_subclass_and_nothing_else(code, kind):
    assert code_type(code) == kind


def test_a_lot_of_vehicle_repair_and_trade_is_a_service_by_the_tie():
    assert purchase_type(["45.20.11.200", "46.90.10.000"]) == "services"  # services = goods -> services
    assert purchase_type(["45.20.11.200", "33.12.29.000", "33.12.11.000"]) == "goods"
