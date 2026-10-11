"""Retries with capped exponential backoff and full jitter, plus a token bucket."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from .base import ModelError

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0

    def backoff(self, attempt: int, rng: random.Random) -> float:
        # "Full jitter": uniform in [0, min(cap, base * 2^attempt)]. Spreads out
        # clients that failed together instead of having them retry in lockstep.
        return rng.uniform(0.0, min(self.max_delay, self.base_delay * (2**attempt)))


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, ModelError, float], None] | None = None,
) -> T:
    rng = rng or random.Random()
    attempt = 0
    while True:
        try:
            return await fn()
        except ModelError as exc:
            attempt += 1
            if not exc.retriable or attempt >= policy.max_attempts:
                raise
            delay = exc.retry_after if exc.retry_after is not None else policy.backoff(attempt - 1, rng)
            delay = min(max(delay, 0.0), policy.max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            await sleep(delay)


class TokenBucket:
    """Async token bucket: `rate` tokens per second, bursts up to `capacity`."""

    def __init__(
        self,
        rate: float,
        capacity: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        current = self._clock()
        self._tokens = min(self.capacity, self._tokens + (current - self._updated) * self.rate)
        self._updated = current

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens > self.capacity:
            raise ValueError("cannot acquire more tokens than the bucket holds")
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await self._sleep((tokens - self._tokens) / self.rate)
