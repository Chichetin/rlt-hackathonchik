"""HostPacer (#289): the one implementation of pauses between requests and of the block after a refusal.

Time is a fake clock and a fake sleep (nothing really waits), except the two-thread test: it uses the real
monotonic clock with an interval of 50 ms, because it checks that the lock really holds a second thread back.
"""

from __future__ import annotations

import threading
import time

import pytest

from src.sources.pacing import HostPacer, NoTimeLeft


class FakeTime:
    """A clock that moves only when somebody sleeps or the test advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def ft() -> FakeTime:
    return FakeTime()


def pacer(ft: FakeTime, interval: float = 2.0, cooldown: float = 900.0) -> HostPacer:
    return HostPacer(interval, cooldown, clock=ft.clock, sleep=ft.sleep)


# --- the interval between requests ---------------------------------------------------------------------------

def test_first_request_does_not_wait(ft):
    ft.now = 0.0  # the clock may be at zero: «never requested» is not «requested at 0»
    with pacer(ft, 60.0).slot():
        pass
    assert ft.slept == []


def test_next_request_waits_the_interval_counted_from_the_end_of_the_previous_one(ft):
    p = pacer(ft, 2.0)
    with p.slot():
        ft.now += 0.5  # the request itself took half a second
    with p.slot():
        pass
    assert ft.slept == [2.0]  # the pause starts when the first request is over
    ft.now += 0.5
    with p.slot():
        pass
    assert ft.slept == [2.0, 1.5]  # 0.5 s of the interval has already passed


def test_no_pause_when_the_interval_has_already_passed(ft):
    p = pacer(ft, 2.0)
    with p.slot():
        pass
    ft.now += 2.0  # exactly the interval: nothing to wait for, and no sleep(0) either
    with p.slot():
        pass
    ft.now += 10.0
    with p.slot():
        pass
    assert ft.slept == []


def test_interval_is_read_at_every_request_when_it_is_a_callable(ft):
    interval = [2.0]
    p = HostPacer(lambda: interval[0], 900.0, clock=ft.clock, sleep=ft.sleep)  # like `lambda: MIN_INTERVAL_S`
    with p.slot():
        pass
    interval[0] = 5.0
    with p.slot():
        pass
    interval[0] = 0.0
    with p.slot():
        pass
    assert ft.slept == [5.0]


def test_a_failed_request_counts_for_the_interval_too(ft):
    p = pacer(ft, 2.0)
    with pytest.raises(ConnectionError), p.slot():
        raise ConnectionError("down")
    with p.slot():
        pass
    assert ft.slept == [2.0]  # knocking on a broken host is not free either


def test_a_slot_refused_for_lack_of_time_leaves_no_trace(ft):
    p = pacer(ft, 2.0)
    with pytest.raises(NoTimeLeft):
        with p.slot(deadline=0.0, min_attempt_s=0.2):
            pytest.fail("no request may be made")
    with p.slot():  # nothing was sent, so there is nothing to wait for
        pass
    assert ft.slept == []


# --- the block after a refusal --------------------------------------------------------------------------------

def test_block_lasts_the_cooldown_and_ends_exactly_at_its_end(ft):
    p = pacer(ft, cooldown=900.0)
    assert not p.blocked()
    p.block()
    assert p.blocked()
    ft.now = 899.9
    assert p.blocked()
    ft.now = 900.0
    assert not p.blocked()  # until, not through


def test_block_takes_explicit_seconds_over_the_default(ft):
    p = pacer(ft, cooldown=900.0)
    p.block(90.0)
    ft.now = 89.0
    assert p.blocked()
    ft.now = 90.0
    assert not p.blocked()


def test_a_shorter_block_never_shortens_a_longer_one(ft):
    p = pacer(ft, cooldown=900.0)
    p.block(900.0)  # a captcha: until 900
    ft.now = 100.0
    p.block(90.0)  # a timeout (until 190) after the captcha: the captcha pause stays
    ft.now = 899.0
    assert p.blocked()
    ft.now = 900.0
    assert not p.blocked()
    p.block(900.0)  # a new refusal at 900 extends it: until 1800
    ft.now = 1799.0
    assert p.blocked()
    ft.now = 1800.0
    assert not p.blocked()


def test_a_pacer_without_cooldown_does_not_block_by_default(ft):
    p = HostPacer(1.0, clock=ft.clock, sleep=ft.sleep)
    p.block()
    assert not p.blocked()


def test_reset_forgets_the_block_and_the_last_request(ft):
    p = pacer(ft, 2.0)
    with p.slot():
        pass
    p.block()
    p.reset()
    assert not p.blocked()
    with p.slot():
        pass
    assert ft.slept == []


def test_block_can_be_set_from_inside_a_slot(ft):
    p = pacer(ft)
    with p.slot():
        p.block(90.0)  # the way a source reacts to a timeout: the turn is held, the state is not
    assert p.blocked()


def test_blocked_does_not_wait_for_a_request_in_progress():
    p = HostPacer(0.0, 900.0)
    inside, release = threading.Event(), threading.Event()

    def long_request() -> None:
        with p.slot():
            inside.set()
            release.wait(5)

    t = threading.Thread(target=long_request)
    t.start()
    try:
        assert inside.wait(5)
        started = time.monotonic()
        assert not p.blocked()  # must answer at once: every search asks it before it goes to the network
        p.block(1.0)
        assert p.blocked()
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        t.join(5)


# --- the deadline ---------------------------------------------------------------------------------------------

def test_no_time_before_the_deadline_makes_no_request_and_does_not_sleep(ft):
    ft.now = 100.0
    with pytest.raises(NoTimeLeft):
        with pacer(ft, 0.0).slot(deadline=100.1, min_attempt_s=0.2):
            pytest.fail("no request may be made")
    assert ft.slept == []


def test_the_pause_is_part_of_the_budget(ft):
    p = pacer(ft, 1.0)
    with p.slot():
        pass
    # a second request must wait 1 s; of the 1.1 s until the deadline only 0.1 s would be left: not worth it
    with pytest.raises(NoTimeLeft):
        with p.slot(deadline=ft.now + 1.1, min_attempt_s=0.2):
            pytest.fail("no request may be made")
    assert ft.slept == []  # and it did not sit out the pause to find that out
    with p.slot(deadline=ft.now + 3.0, min_attempt_s=0.2) as limit:
        assert ft.slept == [1.0]  # the pause is served before the request
        assert limit == pytest.approx(2.0)  # 3 s − 1 s of the pause


def test_limit_is_the_timeout_cut_to_what_is_left(ft):
    p = pacer(ft, 0.0)
    with p.slot(timeout=3.0) as limit:
        assert limit == 3.0  # no deadline: the timeout as is
    with p.slot(deadline=ft.now + 60, timeout=3.0) as limit:
        assert limit == 3.0  # plenty of time: the timeout is the cap
    with p.slot(deadline=ft.now + 1.0, timeout=3.0) as limit:
        assert limit == pytest.approx(1.0)  # little time: what is left
    with p.slot() as limit:
        assert limit == float("inf")  # no timeout, no deadline: no limit


def test_the_smallest_worthwhile_attempt_is_allowed(ft):
    ft.now = 10.0  # binary-exact numbers: the boundary is compared without rounding noise
    with pacer(ft, 0.0).slot(deadline=10.5, min_attempt_s=0.5, timeout=3.0) as limit:
        assert limit == 0.5  # exactly the minimum: still goes
    with pytest.raises(NoTimeLeft):
        with pacer(ft, 0.0).slot(deadline=10.25, min_attempt_s=0.5, timeout=3.0):
            pytest.fail("no request may be made")


def test_a_deadline_in_the_past_is_no_time(ft):
    ft.now = 50.0
    with pytest.raises(NoTimeLeft):
        with pacer(ft, 0.0).slot(deadline=49.0):
            pytest.fail("no request may be made")


# --- threads --------------------------------------------------------------------------------------------------

def test_threads_never_make_requests_at_the_same_time_and_keep_the_interval():
    interval, n_threads = 0.05, 4
    p = HostPacer(interval)
    start = threading.Barrier(n_threads)
    spans: list[tuple[float, float]] = []  # (request started, request ended)

    def worker() -> None:
        start.wait()  # all threads ask for a turn at the same moment
        with p.slot():
            began = time.monotonic()
            time.sleep(0.01)  # the request itself
            ended = time.monotonic()
        spans.append((began, ended))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(spans) == n_threads
    spans.sort()
    for (_, prev_end), (next_begin, _) in zip(spans, spans[1:], strict=False):
        # next starts after the previous one ended AND a full interval later (10 ms of slack for clock grain)
        assert next_begin - prev_end >= interval - 0.01, f"{next_begin - prev_end:.3f} s between two requests"
