from __future__ import annotations

import pytest

from app.db.models import CaseResult, EndpointLoadStats
from app.regression.detector import (
    baseline_p95,
    detect_functional_changes,
    detect_latency_regressions,
)


def stats(endpoint: str, p95: float, requests: int = 100) -> EndpointLoadStats:
    return EndpointLoadStats(
        endpoint=endpoint,
        requests=requests,
        failures=0,
        error_rate=0.0,
        rps=10.0,
        p50_ms=p95 / 2,
        p95_ms=p95,
        p99_ms=p95 * 1.2,
        avg_ms=p95 / 2,
        max_ms=p95 * 2,
    )


def result(hash_: str, status: int | None, passed: bool) -> CaseResult:
    return CaseResult(
        endpoint="POST /items",
        method="POST",
        path="/items",
        case_name=f"case {hash_}",
        category="null",
        source="rule",
        expected="2xx",
        payload_hash=hash_,
        status_code=status,
        passed=passed,
        failure_reason=None if passed else "boom",
    )


def test_baseline_is_median_of_history() -> None:
    history = [[stats("A", 10)], [stats("A", 30)], [stats("A", 1000)], [stats("B", 5)]]
    assert baseline_p95("A", history) == 30  # median ignores the 1000 ms outlier
    assert baseline_p95("A", history[:2]) == 20  # even count -> mean of middle two
    assert baseline_p95("C", history) is None
    assert baseline_p95("A", [[stats("A", 999, requests=0)]]) is None


@pytest.mark.parametrize(
    ("current", "flagged"),
    [
        (100, False),  # unchanged
        (119, False),  # +19 %  -> below X
        (149, False),  # +49 %, +49 ms -> pct ok but not > Y ms
        (150, False),  # +50 ms is not *greater than* 50
        (151, True),  # +51 %, +51 ms -> both exceeded
        (400, True),
    ],
)
def test_latency_needs_both_thresholds(current: float, flagged: bool) -> None:
    history = [[stats("A", 100)], [stats("A", 100)], [stats("A", 100)]]
    flags = detect_latency_regressions([stats("A", current)], history, 20, 50)
    assert bool(flags) is flagged


def test_percent_threshold_matters_for_slow_endpoints() -> None:
    history = [[stats("A", 3000)]]
    # +60 ms on a 3 s endpoint is only +2 % -> not a regression
    assert detect_latency_regressions([stats("A", 3060)], history, 20, 50) == []


def test_flag_details_and_message() -> None:
    history = [[stats("GET /items/{item_id}", 6)], [stats("GET /items/{item_id}", 8)]]
    (flag,) = detect_latency_regressions([stats("GET /items/{item_id}", 210)], history, 20, 50)
    assert flag.kind == "latency_regression"
    assert flag.details["baseline_p95_ms"] == 7
    assert flag.details["delta_ms"] == 203
    assert flag.details["delta_pct"] == pytest.approx(2900)
    assert "+203 ms" in flag.message


def test_zero_baseline_and_new_endpoints() -> None:
    assert (
        detect_latency_regressions([stats("A", 80)], [[stats("A", 0)]], 20, 50)[0].details[
            "delta_pct"
        ]
        is None
    )
    assert detect_latency_regressions([stats("NEW", 900)], [[stats("A", 1)]], 20, 50) == []
    assert detect_latency_regressions([stats("A", 900)], [], 20, 50) == []


def test_new_5xx_and_newly_failing() -> None:
    previous = [
        result("h1", 201, True),
        result("h2", 500, False),  # already broken last time
        result("h3", 201, True),
        result("h4", 422, True),
    ]
    current = [
        result("h1", 500, False),  # new 5xx (also newly failing, reported once as new_5xx)
        result("h2", 500, False),  # still 5xx -> not new
        result("h3", 400, False),  # newly failing, not a 5xx
        result("h4", 422, True),  # unchanged
        result("h5", 503, False),  # never seen before -> new 5xx
    ]
    flags = detect_functional_changes(current, previous)
    assert [(f.kind, f.details["payload_hash"]) for f in flags] == [
        ("new_5xx", "h1"),
        ("newly_failing", "h3"),
        ("new_5xx", "h5"),
    ]
    assert flags[0].details["previous_status_code"] == 201


def test_first_run_flags_every_5xx_but_nothing_newly_failing() -> None:
    current = [result("h1", 500, False), result("h2", 400, False), result("h3", None, False)]
    flags = detect_functional_changes(current, None)
    assert [f.kind for f in flags] == ["new_5xx"]
