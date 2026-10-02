"""One implementation of «do not hurt the host»: pauses and the block after a refusal (#289). Owner: SOURCES.

Every open source we query (pb.nalog.ru, bo.nalog.gov.ru, rmsp.nalog.ru, zakupki.gov.ru) has one
`HostPacer`. It is what keeps our IP from a ban on the venue network, so it lives here once: a fix in this
file reaches every source, a new source takes a pacer instead of copying the logic.

- `slot()` — the turn for ONE request. Threads queue for it: one request to a host at a time, the next one
  starts `min_interval_s` after the previous one ENDED (a failed request counts too: knocking on a broken
  host is not free). The pause is served before the request, inside the turn. An optional `deadline`
  (a time of the pacer's clock) makes the pause part of the caller's budget: when too little is left for
  an attempt, nothing is sent and nothing is slept (`NoTimeLeft`).
- `block(seconds)` / `blocked()` — after a refusal (captcha, 403/429) or a timeout the source tells the
  pacer to leave the host alone. A block only grows: a short pause never cuts a longer one.
  The pacer does not stop `slot()` by itself while blocked: the source checks `blocked()` first (it also
  has its own words for the refusal) and raises its own error.

What differs between hosts stays in the source module: the interval and the cooldown (environment),
`PB_DISABLED`, `Crawl-delay: 60` as a floor of the interval, which responses mean «refusal», the text of
errors. The clock and `sleep` are parameters, so the tests do not really wait.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from math import inf

Seconds = float | Callable[[], float]  # a number, or a function read at every request (overridable setting)


class NoTimeLeft(RuntimeError):
    """The deadline leaves no time for a request: nothing was sent and the pause was not sat out."""


def _value(v: Seconds) -> float:
    return v() if callable(v) else v


class HostPacer:
    def __init__(
        self,
        min_interval_s: Seconds,
        cooldown_s: float = 0.0,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """`cooldown_s` is the default length of `block()`; 0 — the source never blocks by default."""
        self._interval = min_interval_s
        self._cooldown_s = cooldown_s
        # looked up at the call, not bound here: whoever replaces time.sleep / time.monotonic is obeyed
        self._clock = clock or (lambda: time.monotonic())
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._turn = threading.Lock()  # held for the whole request: one at a time per host
        self._state = threading.Lock()  # short: _blocked_until (blocked() must not wait for a request)
        self._last_end = -inf  # end of the last request; -inf: none yet, so the first one never waits
        self._blocked_until = 0.0

    @contextmanager
    def slot(
        self, deadline: float | None = None, *, timeout: float | None = None, min_attempt_s: float = 0.0
    ) -> Iterator[float]:
        """The turn for one request; yields its time limit: `timeout` cut to what is left before `deadline`
        once the pause is served (`inf` when there is neither). With a deadline, a limit below
        `min_attempt_s` raises NoTimeLeft before anything is slept or sent."""
        with self._turn:
            wait = max(0.0, _value(self._interval) - (self._clock() - self._last_end))
            limit = inf if timeout is None else timeout
            if deadline is not None:
                limit = min(limit, deadline - self._clock() - wait)
                if limit < min_attempt_s:
                    raise NoTimeLeft(f"до срока осталось {limit:.2f} с, меньше {min_attempt_s} с на попытку")
            if wait > 0:
                self._sleep(wait)
            try:
                yield limit
            finally:
                self._last_end = self._clock()

    def block(self, seconds: float | None = None) -> None:
        """Leave the host alone for `seconds` (default: the cooldown). Extends a running block, never shortens it."""
        until = self._clock() + (self._cooldown_s if seconds is None else seconds)
        with self._state:
            self._blocked_until = max(self._blocked_until, until)

    def blocked(self) -> bool:
        with self._state:
            return self._clock() < self._blocked_until

    def reset(self) -> None:
        """Forget the block and the last request (tests; a manual restart of a source)."""
        with self._state:
            self._blocked_until = 0.0
        self._last_end = -inf  # not under the turn: reset() from inside a slot must not deadlock
