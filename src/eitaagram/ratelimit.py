"""Async token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Allow ``rate`` operations per second with a burst of ``burst``.

    ``acquire`` is FIFO-fair: the bucket is debited under a lock and the caller
    then sleeps outside it for exactly the time it must wait.
    """

    def __init__(self, rate: float, burst: int = 1) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rate = float(rate)
        self.burst = int(burst)
        self._tokens = float(burst)
        self._stamp = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate)
            self._stamp = now
            self._tokens -= cost
            wait = -self._tokens / self.rate if self._tokens < 0 else 0.0
        if wait > 0:
            await asyncio.sleep(wait)
