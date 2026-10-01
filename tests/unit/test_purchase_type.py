"""Purchase type by OKPD2 (#242, rule of #188): 01–33 goods, 41–43 works, the rest services — remote ones by the
class table; the goods exceptions of #188; one point for the UI filter default and the scoring profiles of #104."""

import pytest

from src.match.purchase_type import (
    PURCHASE_LABEL,
    REMOTE_CLASSES,
    REMOTE_SHARE,
    code_type,
    default_near,
    purchase_basis,
    purchase_type,
)


@pytest.mark.parametrize("code, kind", [
    ("01.11.11.111", "goods"), ("17.12.14.110", "goods"), ("33.12.29.000", "goods"),
    ("34.10", "services"), ("35.11.10.110", "remote"), ("40", "services"),
    ("41.20.40.000", "works"), ("42.11", "works"), ("43.39.11.000", "works"),
    ("44.00", "services"), ("61.10.11.110", "services"), ("81.21.10.000", "services"), ("99.00.10.000", "services"),
])
def test_class_ranges(code, kind):
    assert code_type(code) == kind


@pytest.mark.parametrize("code, kind", [
    ("58.11.11.000", "goods"), ("58.14.11.000", "goods"), ("58.19.11.000", "goods"),  # printed editions (#188)
    ("46.19.10.000", "goods"), ("47.11.10.000", "goods"),  # trade classes (#188)
    ("58.29.29.000", "remote"), ("58.13.10.000", "remote"),  # software, newspapers: class 58 outside the exceptions
    ("62.01.29.000", "remote"), ("85.42.19.000", "remote"), ("65.12.21.000", "remote"),
    ("81.21.10.000", "services"), ("86.90.19.000", "services"), ("56.29.20.000", "services"),
])
def test_exceptions_of_188_and_remote_services(code, kind):
    assert code_type(code) == kind


def test_remote_classes_are_services_with_a_far_share_of_at_least_20_percent():
    assert REMOTE_CLASSES == set(REMOTE_SHARE)
    assert all(share >= 20 for share in REMOTE_SHARE.values())
    assert not REMOTE_CLASSES & {*range(1, 34), 41, 42, 43, 46, 47}  # goods and works stay as the rule says


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
    (["62.01.29.000", "81.21.10.000"], "services"),  # remote = services -> services (on site is stricter)
    (["62.01.29.000", "17.12.14.110"], "remote"),  # remote = goods -> remote
    (["62.01.29.000", "62.02.30.000", "81.21.10.000"], "remote"),
])
def test_tie_works_over_services_over_remote_over_goods(codes, kind):
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
    assert PURCHASE_LABEL == {"goods": "товар", "works": "работа", "services": "услуга",
                              "remote": "услуга не на месте заказчика"}
    assert default_near("works") == "near" and default_near("services") == "near"
    assert default_near("goods") == "any" and default_near("remote") == "any" and default_near(None) == "any"
