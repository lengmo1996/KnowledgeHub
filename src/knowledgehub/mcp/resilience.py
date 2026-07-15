"""Bounded MCP runtime concurrency, rate limiting, and circuit breakers."""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_seconds: float = 30.0
    max_attempts: int = 2
    failures: int = 0
    opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if time.monotonic() - self.opened_at >= self.recovery_seconds:
            return "half_open"
        return "open"

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        if self.state == "open":
            raise RuntimeError("circuit_open")
        for attempt in range(self.max_attempts):
            try:
                result = await operation()
            except Exception:
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(random.uniform(0.01, 0.05))
                    continue
                self.failures += 1
                if self.failures >= self.failure_threshold:
                    self.opened_at = time.monotonic()
                raise
            self.failures = 0
            self.opened_at = None
            return result
        raise AssertionError("unreachable")


class SlidingWindowLimiter:
    def __init__(self, limit: int, *, seconds: float = 60.0) -> None:
        self.limit = limit
        self.seconds = seconds
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, principal: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            events = self._events[principal]
            while events and events[0] <= now - self.seconds:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True
