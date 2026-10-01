"""HostPacer (#289): the one implementation of pauses between requests and of the block after a refusal.

Time is a fake clock and a fake sleep (nothing really waits), except the thread tests: they use the real
monotonic clock with an interval of 50 ms, because they check that the lock really holds a second thread back.
The second half checks that pb_nalog, bo_nalog, rmsp and eis_rnp use their pacers with their own numbers;
the network is httpx.MockTransport.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from src.sources import bo_nalog, eis_rnp, pb_nalog, rmsp
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


# --- the four sources are wired to their pacers ----------------------------------------------------------------
# The real PACER of every module (its interval and cooldown as the module sets them), on a fake clock and a fake
# sleep; the host is an httpx.MockTransport. A pacer that is not used, or used with a wrong number, shows here.

INN = "7804428656"  # passes the checksum
RNP_PAGE = '<div class="search-registry-entrys-block">Поиск не дал результатов</div>'


@pytest.fixture
def fake_time(monkeypatch) -> FakeTime:
    ft = FakeTime()
    for mod in (pb_nalog, bo_nalog, rmsp, eis_rnp):
        monkeypatch.setattr(mod.PACER, "_clock", ft.clock)
        monkeypatch.setattr(mod.PACER, "_sleep", ft.sleep)
        monkeypatch.setattr(mod, "MIN_INTERVAL_S", 7.0)
    return ft


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "1", "content": [], "data": []}, request=request)


def _rnp_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=RNP_PAGE, request=request)


# one live request to the host of each module: (module, call, handler answering it)
ONE_REQUEST = {
    "pb": (pb_nalog, lambda c: pb_nalog._post(c, "/search-proc.json", {}), _ok),
    "bo": (bo_nalog, lambda c: bo_nalog.fetch_raw(INN, c), _ok),
    "rmsp": (rmsp, lambda c: rmsp.fetch_raw("17.23", "78", "UL", c), _ok),
    "rmsp-quick": (rmsp, lambda c: rmsp.fetch_quick_raw(INN, c), _ok),
    "rnp": (eis_rnp, lambda c: eis_rnp._get(c, "https://zakupki.gov.ru/x"), _rnp_ok),
}


@pytest.mark.parametrize("name", list(ONE_REQUEST))
def test_every_source_serves_its_interval_before_the_second_request(fake_time, name):
    _, call, handler = ONE_REQUEST[name]
    client = _client(handler)
    call(client)
    assert fake_time.slept == []  # nobody was asked before
    call(client)
    assert fake_time.slept == [7.0]  # MIN_INTERVAL_S of the module, read at the request


def test_pb_captcha_blocks_the_host_for_the_cooldown(fake_time):
    refuse = httpx.Response(400, json={"ERRORS": {"pbSearchCaptcha": ["введите код"]}})
    with pytest.raises(pb_nalog.CaptchaRequired):
        pb_nalog._post(_client(lambda r: refuse), "/search-proc.json", {})
    assert pb_nalog.blocked()
    fake_time.now = pb_nalog.BLOCK_COOLDOWN_S - 1
    assert pb_nalog.blocked()
    fake_time.now = pb_nalog.BLOCK_COOLDOWN_S
    assert not pb_nalog.blocked()


def test_pb_disabled_by_the_environment_blocks_without_a_timer(fake_time, monkeypatch):
    assert not pb_nalog.blocked()
    monkeypatch.setenv("PB_DISABLED", "1")
    assert pb_nalog.blocked()
    monkeypatch.setenv("PB_DISABLED", "0")
    assert not pb_nalog.blocked()


@pytest.mark.parametrize("status", [403, 429])
def test_bo_refusal_blocks_the_host_and_the_next_call_sends_nothing(fake_time, status):
    with pytest.raises(bo_nalog.Blocked):
        bo_nalog.fetch_raw(INN, _client(lambda r: httpx.Response(status)))
    assert bo_nalog.blocked()
    with pytest.raises(bo_nalog.Blocked):
        bo_nalog.fetch_raw(INN, _client(lambda r: pytest.fail("request to a blocked host")))
    fake_time.now = bo_nalog.BLOCK_COOLDOWN_S
    assert not bo_nalog.blocked()


def test_rmsp_refusal_blocks_for_the_cooldown_and_a_dead_connection_for_the_short_one(fake_time):
    with pytest.raises(rmsp.Blocked):
        rmsp.fetch_raw("17.23", "78", "UL", _client(lambda r: httpx.Response(403)))
    assert rmsp.blocked()
    fake_time.now = rmsp.BLOCK_COOLDOWN_S - 1
    assert rmsp.blocked()
    fake_time.now = rmsp.BLOCK_COOLDOWN_S
    assert not rmsp.blocked()

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("down", request=request)

    with pytest.raises(httpx.ConnectTimeout):
        rmsp.fetch_raw("17.23", "78", "UL", _client(dead))
    assert rmsp.blocked()  # the short pause: the next search does not wait for a hanging source again
    fake_time.now += rmsp.DOWN_COOLDOWN_S - 1
    assert rmsp.blocked()
    fake_time.now += 1
    assert not rmsp.blocked()
    assert rmsp.DOWN_COOLDOWN_S < rmsp.BLOCK_COOLDOWN_S


def test_rmsp_without_time_before_the_deadline_raises_paused_and_sends_nothing(fake_time):
    client = _client(lambda r: pytest.fail("no request may be made"))
    with pytest.raises(rmsp.Paused):
        rmsp.fetch_raw("17.23", "78", "UL", client, deadline=fake_time.now + rmsp.MIN_ATTEMPT_S / 2)
    assert not rmsp.blocked() and fake_time.slept == []  # not a refusal, and nothing was slept
    rmsp.PACER.block(5.0)
    with pytest.raises(rmsp.Paused):  # the pause after a refusal is Paused as well
        rmsp.fetch_raw("17.23", "78", "UL", client)


def test_rmsp_timeout_of_the_request_is_cut_to_the_deadline_after_the_pause(fake_time):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"data": []}, request=request)

    client = _client(handler)
    rmsp.fetch_raw("17.23", "78", "UL", client)  # the first one: no pause
    rmsp.fetch_raw("17.23", "78", "UL", client, deadline=fake_time.now + 7.0 + 1.0)  # the pause (7 s) leaves 1 s
    assert fake_time.slept == [7.0]
    assert [(t["connect"], t["read"]) for t in seen[1:]] == [(0.5, 0.5)]  # half of the remaining second each


def test_rmsp_quick_search_has_its_own_longer_timeout_and_no_deadline(fake_time):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"data": []}, request=request)

    rmsp.fetch_quick_raw(INN, _client(handler))
    assert [(t["connect"], t["read"]) for t in seen] == [(rmsp.QUICK_TIMEOUT_S / 2, rmsp.QUICK_TIMEOUT_S / 2)]
    assert rmsp.QUICK_TIMEOUT_S > rmsp.TIMEOUT_S  # enrich is not on the 4 s search budget


def test_rnp_refusal_is_not_remembered_between_requests(fake_time):
    for status in (403, 429):
        with pytest.raises(eis_rnp.Blocked):
            eis_rnp._get(_client(lambda r, s=status: httpx.Response(s)), "https://zakupki.gov.ru/x")
    assert not eis_rnp.PACER.blocked()  # this source's policy is unchanged: a refusal raises, only the pause remains
