"""Deliberately broken test (#285 acceptance): CI `check` must go red on it. Reverted in the next commit."""


def test_ci_goes_red_on_a_broken_test() -> None:
    assert 1 + 1 == 3
