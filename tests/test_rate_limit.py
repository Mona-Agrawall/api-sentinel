from __future__ import annotations

import pytest

from app.rate_limit import TokenBucketLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_allows_a_burst_then_refills_over_time() -> None:
    clock = FakeClock()
    limiter = TokenBucketLimiter(per_minute=3, clock=clock)

    assert [limiter.acquire("k") for _ in range(3)] == [None, None, None]
    assert limiter.acquire("k") == pytest.approx(20.0)  # one token every 20 s

    clock.now = 19.0
    assert limiter.acquire("k") == pytest.approx(1.0)
    clock.now = 20.0
    assert limiter.acquire("k") is None
    assert limiter.acquire("k") is not None


def test_buckets_are_independent_per_key() -> None:
    limiter = TokenBucketLimiter(per_minute=1, clock=FakeClock())
    assert limiter.acquire("a") is None
    assert limiter.acquire("a") is not None
    assert limiter.acquire("b") is None


def test_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError, match="per_minute"):
        TokenBucketLimiter(per_minute=0)
