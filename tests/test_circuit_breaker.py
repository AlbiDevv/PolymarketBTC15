import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchange_client.circuit_breaker import CircuitBreaker, CircuitState, RateLimiter


def test_starts_closed():
    cb = CircuitBreaker(failure_threshold=3)
    assert cb.state == CircuitState.CLOSED
    assert cb.is_allowed


def test_trips_after_threshold():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure("err1")
    cb.record_failure("err2")
    assert cb.state == CircuitState.CLOSED
    cb.record_failure("err3")
    assert cb.state == CircuitState.OPEN
    assert not cb.is_allowed


def test_success_resets():
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure("err1")
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    # Need 2 more failures to trip again
    cb.record_failure("err1")
    assert cb.state == CircuitState.CLOSED


def test_recovery_after_timeout():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_sec=0)
    cb.record_failure("err")
    # With timeout=0, property access immediately transitions to HALF_OPEN
    import time
    time.sleep(0.01)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.is_allowed  # half-open allows a test request


def test_rate_limiter_has_tokens():
    rl = RateLimiter(max_tokens=100)
    assert rl.available_tokens > 0
