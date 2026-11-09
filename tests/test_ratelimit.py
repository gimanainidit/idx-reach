"""Tests for the RateLimiter module (Task 2, plan §2.4 and Global Constraint 2).

No test sleeps in real time: the limiter takes an injected clock.
"""

import pytest

from idx_reach.ratelimit import ManualClock, RateLimitBreakerTripped, RateLimiter


def test_first_acquire_does_not_sleep():
    clock = ManualClock()
    limiter = RateLimiter(clock=clock)

    limiter.acquire()

    assert clock.slept == []


def test_second_acquire_sleeps_the_remaining_gap():
    clock = ManualClock()
    limiter = RateLimiter(delay=6.5, clock=clock)

    limiter.acquire()
    clock.advance(2.0)
    limiter.acquire()

    assert clock.slept == [pytest.approx(4.5)]
    assert clock.monotonic() == pytest.approx(6.5)


def test_acquire_does_not_sleep_when_the_gap_has_already_passed():
    clock = ManualClock()
    limiter = RateLimiter(delay=6.5, clock=clock)

    limiter.acquire()
    clock.advance(10.0)
    limiter.acquire()

    assert clock.slept == []


def test_default_delay_is_the_measured_safe_value():
    assert RateLimiter(clock=ManualClock()).delay == 6.5


def test_three_consecutive_429s_trip_the_breaker():
    limiter = RateLimiter(clock=ManualClock())

    limiter.record(429)
    limiter.record(429)
    with pytest.raises(RateLimitBreakerTripped) as excinfo:
        limiter.record(429)

    assert "429" in str(excinfo.value)
    assert limiter.tripped is True


def test_two_429s_then_a_200_resets_the_streak():
    limiter = RateLimiter(clock=ManualClock())

    limiter.record(429)
    limiter.record(429)
    limiter.record(200)
    limiter.record(429)
    limiter.record(429)

    assert limiter.consecutive_429 == 2
    assert limiter.tripped is False


def test_ten_503s_do_not_trip_the_breaker():
    """§2.4: a 503 from Varnish means a rejected parameter, not rate limiting."""
    limiter = RateLimiter(clock=ManualClock())

    for _ in range(10):
        limiter.record(503)

    assert limiter.tripped is False
    assert limiter.consecutive_429 == 0


def test_503_is_neutral_and_neither_counts_nor_clears_the_429_streak():
    limiter = RateLimiter(clock=ManualClock())

    limiter.record(429)
    limiter.record(503)
    limiter.record(429)
    with pytest.raises(RateLimitBreakerTripped):
        limiter.record(429)


def test_acquire_refuses_to_run_once_the_breaker_is_tripped():
    limiter = RateLimiter(clock=ManualClock())
    limiter.record(429)
    limiter.record(429)
    with pytest.raises(RateLimitBreakerTripped):
        limiter.record(429)

    with pytest.raises(RateLimitBreakerTripped):
        limiter.acquire()
