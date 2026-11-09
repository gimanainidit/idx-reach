"""Rate limiting: one global queue, one request at a time, 6.5 s apart.

Global Constraint 2. The 6.5 s default is the empirically safe value: 330
consecutive requests at that spacing produced zero HTTP 429 in the source
session (plan §6).

The breaker counts only HTTP 429. A 503 from Varnish on the announcement
endpoint means an unrecognised parameter was sent, not rate limiting (plan
§2.4), so it is treated as *neutral*: it neither counts toward the streak nor
clears it. Clearing on 503 would let an alternating 429/503 pattern hide a real
block; counting it would trigger a pointless retreat.

The clock is a seam in the small: `RateLimiter` never calls `time` directly, so
tests drive it with `ManualClock` and never sleep in real time.
"""

from __future__ import annotations

import time
from typing import Protocol

DEFAULT_DELAY_SECONDS = 6.5
BREAKER_THRESHOLD = 3


class RateLimitBreakerTripped(RuntimeError):
    """Raised after `BREAKER_THRESHOLD` consecutive HTTP 429 responses.

    The source session's rule is: stop, report, and do not retry automatically.
    """


class Clock(Protocol):
    """The time seam. Monotonic reads plus a blocking sleep."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock. The only implementation that actually waits."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ManualClock:
    """A clock the caller advances by hand. `sleep` advances it and records the call."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)


class RateLimiter:
    """A single global queue with a 429 breaker.

    Not thread-safe by design: parallel requests to IDX are forbidden
    (plan §5), so there is never a second caller to synchronise with.
    """

    def __init__(
        self,
        delay: float = DEFAULT_DELAY_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        self.delay = delay
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._last_release: float | None = None
        self.consecutive_429 = 0
        self.tripped = False

    def acquire(self) -> None:
        """Block until `delay` seconds have passed since the previous release."""
        self._refuse_if_tripped()
        if self._last_release is not None:
            waited = self._clock.monotonic() - self._last_release
            remaining = self.delay - waited
            if remaining > 0:
                self._clock.sleep(remaining)
        self._last_release = self._clock.monotonic()

    def record(self, status: int) -> None:
        """Feed a response status to the breaker. Raises once the streak reaches three."""
        if status == 429:
            self.consecutive_429 += 1
            if self.consecutive_429 >= BREAKER_THRESHOLD:
                self.tripped = True
                self._refuse_if_tripped()
            return
        if status == 503:
            # Neutral: a rejected parameter, not a rate limit (plan §2.4).
            return
        self.consecutive_429 = 0

    def _refuse_if_tripped(self) -> None:
        if self.tripped:
            raise RateLimitBreakerTripped(
                f"{BREAKER_THRESHOLD} consecutive HTTP 429 responses from IDX; "
                "stopping and not retrying automatically. Raise the delay "
                f"(currently {self.delay} s) and start a fresh run."
            )
