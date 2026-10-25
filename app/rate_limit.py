"""In-memory token-bucket rate limiting.

State lives in the process, which matches how runs execute (in the API process). Several API
instances would each enforce their own limit.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

MAX_TRACKED_KEYS = 10_000


class TokenBucketLimiter:
    """One token bucket per key: bursts of up to ``per_minute``, refilled at ``per_minute``."""

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be >= 1")
        self.capacity = float(per_minute)
        self.refill_per_s = per_minute / 60
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, updated_at)
        self._lock = threading.Lock()

    def acquire(self, key: str) -> float | None:
        """Take a token for ``key``. Returns None if allowed, else seconds until one is free."""
        with self._lock:
            now = self._clock()
            tokens = self._tokens(key, now)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                if len(self._buckets) > MAX_TRACKED_KEYS:
                    self._drop_full_buckets(now)
                return None
            self._buckets[key] = (tokens, now)
            return (1 - tokens) / self.refill_per_s

    def _tokens(self, key: str, now: float) -> float:
        tokens, updated_at = self._buckets.get(key, (self.capacity, now))
        return min(self.capacity, tokens + (now - updated_at) * self.refill_per_s)

    def _drop_full_buckets(self, now: float) -> None:
        # A full bucket is indistinguishable from an untracked key, so forgetting it is safe.
        for key in [k for k in self._buckets if self._tokens(k, now) >= self.capacity]:
            del self._buckets[key]
