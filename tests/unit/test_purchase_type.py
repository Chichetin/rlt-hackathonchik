"""Purchase type by OKPD2 (#242, rule of #188): 01–33 goods, 41–43 works, the rest services; one point for the UI
filter default and the scoring profiles of #104."""

import pytest

from src.match.purchase_type import (
    PURCHASE_LABEL,
    code_type,
    default_near,
    purchase_basis,
    purchase_type,
)


@pytest.mark.parametrize("code, kind", [
    ("01.11.11.111", "goods"), ("17.12.14.110", "goods"), ("33.12.29.000", "goods"),
    ("34.10", "services"), ("35.11.10.110", "services"), ("40", "services"),
    ("41.20.40.000", "works"), ("42.11", "works"), ("43.39.11.000", "works"),
    ("44.00", "services"), ("61.10.11.110", "services"), ("81.21.10.000", "services"), ("99.00.10.000", "services"),
])
def test_class_ranges(code, kind):
    assert code_type(code) == kind


@pytest.mark.parametrize("code", ["", " ", "7", "A1.11", "1.11.11", "00.00", "xx"])
def test_unrecognised_code_has_no_type(code):
    assert code_type(code) is None


def test_majority_of_codes():
    assert purchase_type(["17.12.14.110", "17.12.14.119", "81.21.10.000"]) == "goods"
    assert purchase_type(["81.21.10.000", "81.22.11.000", "43.39.11.000"]) == "services"
    assert purchase_type(["43.39.11.000", "43.21.10.110", "17.12.14.110"]) == "works"


@pytest.mark.parametrize("codes, kind", [
    (["17.12.14.110", "43.39.11.000"], "works"),  # goods = works -> works
    (["17.12.14.110", "81.21.10.000"], "services"),  # goods = services -> services
    (["81.21.10.000", "43.39.11.000"], "works"),  # services = works -> works
    (["17.12.14.110", "81.21.10.000", "43.39.11.000"], "works"),  # all equal -> works
    (["17.12.14.110", "17.11.11.000", "81.21.10.000", "81.22.11.000"], "services"),
])
def test_tie_works_over_services_over_goods(codes, kind):
    assert purchase_type(codes) == kind


def test_empty_or_unrecognised_is_none_not_guessed():
    assert purchase_type([]) is None
    assert purchase_type(["", "xx"]) is None
    assert purchase_type(["xx", "", "81.21.10.000"]) == "services"  # only the recognised codes vote


def test_basis_is_up_to_three_codes_of_the_chosen_type_in_order():
    codes = ["81.21.10.000", "17.12.14.110", "81.22.11.000", "81.21.10.000", "81.29.11.000", "81.10.10.000"]
    assert purchase_basis(codes) == ("services", ["81.21.10.000", "81.22.11.000", "81.29.11.000"])
    assert purchase_basis([" 43.39.11.000 "]) == ("works", ["43.39.11.000"])
    assert purchase_basis([]) == (None, [])


def test_labels_and_default_near():
    assert PURCHASE_LABEL == {"goods": "товар", "works": "работа", "services": "услуга"}
    assert default_near("works") == "near" and default_near("services") == "near"
    assert default_near("goods") == "any" and default_near(None) == "any"
