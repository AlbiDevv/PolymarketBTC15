"""
Circuit Breaker — v3.0 §5.4.

Pauses trading when API errors accumulate.
Also provides rate limiting (token bucket) and heartbeat tracking.
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum

from loguru import logger


class CircuitState(Enum):
    CLOSED = "closed"      # normal operation
    OPEN = "open"          # errors detected, trading paused
    HALF_OPEN = "half_open"  # testing recovery


class CircuitBreaker:
    """
    Trips after `failure_threshold` consecutive failures.
    Stays open for `recovery_timeout_sec`, then enters half-open.
    One success in half-open → close; one failure → re-open.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_sec: float = 300,
        on_trip: callable | None = None,
    ):
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_sec
        self._on_trip = on_trip  # callback for alert

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._last_success_time: float = time.time()

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker → HALF_OPEN (testing recovery)")
        return self._state

    @property
    def is_allowed(self) -> bool:
        s = self.state
        return s in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self):
        if self._state == CircuitState.HALF_OPEN:
            logger.info("Circuit breaker → CLOSED (recovery confirmed)")
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_success_time = time.time()

    def record_failure(self, error: str = ""):
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning(f"Circuit breaker → OPEN (failed in half-open): {error}")
            if self._on_trip:
                self._on_trip(error)
            return

        if self._failure_count >= self._threshold:
            self._state = CircuitState.OPEN
            logger.warning(
                f"Circuit breaker TRIPPED after {self._failure_count} failures. "
                f"Pausing for {self._recovery_timeout}s. Last error: {error}"
            )
            if self._on_trip:
                self._on_trip(error)

    def reset(self):
        self._state = CircuitState.CLOSED
        self._failure_count = 0


class RateLimiter:
    """
    Token bucket rate limiter.
    Polymarket CLOB: ~100 req/min recommended.
    """

    def __init__(self, max_tokens: int = 90, refill_interval_sec: float = 60):
        self._max_tokens = max_tokens
        self._tokens = float(max_tokens)
        self._refill_rate = max_tokens / refill_interval_sec
        self._last_refill = time.time()

    async def acquire(self):
        self._refill()
        while self._tokens < 1:
            wait = (1 - self._tokens) / self._refill_rate
            logger.debug(f"Rate limiter: waiting {wait:.1f}s")
            await asyncio.sleep(wait)
            self._refill()
        self._tokens -= 1

    def _refill(self):
        now = time.time()
        elapsed = now - self._last_refill
        self._tokens = min(self._max_tokens, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    @property
    def available_tokens(self) -> float:
        self._refill()
        return self._tokens


class HeartbeatMonitor:
    """
    Tracks liveness of the trading loop.
    If no heartbeat for `timeout_sec`, considered stale.
    """

    def __init__(self, timeout_sec: float = 180):
        self._timeout = timeout_sec
        self._last_beat = time.time()

    def beat(self):
        self._last_beat = time.time()

    @property
    def is_alive(self) -> bool:
        return (time.time() - self._last_beat) < self._timeout

    @property
    def seconds_since_last(self) -> float:
        return time.time() - self._last_beat
