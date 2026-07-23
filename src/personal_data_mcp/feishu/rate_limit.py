"""A small token-bucket limiter for outbound Feishu calls.

Feishu enforces per-app QPS limits, and a single-user agent is nowhere near
them, but an unbounded retry or recovery loop could be. The limiter caps the
outbound rate so a loop degrades into waiting rather than into a provider-side
rate-limit error that would look like an outage.

The clock is injected so the limiter is testable without sleeping. `acquire`
returns the seconds a caller must wait; the async adapter awaits that, and a
test reads it directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """A classic token bucket. `rate` tokens per second, up to `capacity`."""

    rate: float
    capacity: float
    now: Callable[[], float]
    _tokens: float = field(init=False)
    _last: float = field(init=False)

    def __post_init__(self) -> None:
        if self.rate <= 0 or self.capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._tokens = self.capacity
        self._last = self.now()

    def _refill(self) -> None:
        current = self.now()
        elapsed = max(0.0, current - self._last)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last = current

    def acquire(self) -> float:
        """Take one token, returning the seconds to wait before it is available.

        Zero means the call may proceed now. A positive value is how long the
        caller must wait; the token is still consumed, so the bucket accounts
        for the call that is about to happen.
        """
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        deficit = 1.0 - self._tokens
        self._tokens -= 1.0  # goes negative; repaid by refill over time
        return deficit / self.rate
