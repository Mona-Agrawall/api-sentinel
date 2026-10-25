"""Compare a run against history and raise flags.

Latency regression (per endpoint)::

    baseline_p95 = median(p95 of that endpoint over the last K completed runs)
    delta_ms     = current_p95 - baseline_p95
    delta_pct    = delta_ms / baseline_p95 * 100
    flag  <=>  delta_pct > X  AND  delta_ms > Y

Requiring *both* thresholds avoids noise: +100% on a 2 ms endpoint (+2 ms) is not
interesting, and +60 ms on a 3 s endpoint (+2%) is within normal jitter.

Functional flags (compared with the most recent previous run):
  * ``new_5xx``       - a case returned 5xx now but did not in the previous run.
  * ``newly_failing`` - a case (same payload hash) passed previously and fails now.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median

from app.db.models import CaseResult, EndpointLoadStats, Flag


def baseline_p95(endpoint: str, history: Sequence[Sequence[EndpointLoadStats]]) -> float | None:
    """Median p95 of ``endpoint`` across historical runs (None if it never appeared)."""
    values = [s.p95_ms for run in history for s in run if s.endpoint == endpoint and s.requests > 0]
    return float(median(values)) if values else None


def detect_latency_regressions(
    current: Sequence[EndpointLoadStats],
    history: Sequence[Sequence[EndpointLoadStats]],
    pct_threshold: float,
    ms_threshold: float,
) -> list[Flag]:
    """Flag endpoints whose p95 is slower than baseline by > X % and > Y ms."""
    flags: list[Flag] = []
    for stats in current:
        base = baseline_p95(stats.endpoint, history)
        if base is None or stats.requests == 0:
            continue
        delta_ms = stats.p95_ms - base
        delta_pct = (delta_ms / base * 100) if base > 0 else (float("inf") if delta_ms > 0 else 0.0)
        if delta_pct > pct_threshold and delta_ms > ms_threshold:
            pct_text = "inf" if delta_pct == float("inf") else f"{delta_pct:.0f}"
            flags.append(
                Flag(
                    kind="latency_regression",
                    endpoint=stats.endpoint,
                    message=(
                        f"p95 {stats.p95_ms:.0f} ms vs baseline {base:.0f} ms "
                        f"(+{delta_ms:.0f} ms, +{pct_text}%)"
                    ),
                    details={
                        "current_p95_ms": stats.p95_ms,
                        "baseline_p95_ms": base,
                        "delta_ms": round(delta_ms, 2),
                        "delta_pct": None if delta_pct == float("inf") else round(delta_pct, 2),
                        "baseline_runs": len(history),
                        "pct_threshold": pct_threshold,
                        "ms_threshold": ms_threshold,
                    },
                )
            )
    return flags


def detect_functional_changes(
    current: Sequence[CaseResult], previous: Sequence[CaseResult] | None
) -> list[Flag]:
    """Flag new 5xx responses and cases that passed last run but fail now.

    With no previous run every 5xx is new; "newly failing" needs a previous run.
    """
    prev_by_hash = {r.payload_hash: r for r in previous or []}
    flags: list[Flag] = []
    for result in current:
        before = prev_by_hash.get(result.payload_hash)
        is_5xx = result.status_code is not None and result.status_code >= 500
        was_5xx = (
            before is not None and before.status_code is not None and before.status_code >= 500
        )
        details = {
            "case": result.case_name,
            "payload_hash": result.payload_hash,
            "status_code": result.status_code,
            "previous_status_code": before.status_code if before else None,
            "request": result.request,
        }
        if is_5xx and not was_5xx:
            flags.append(
                Flag(
                    kind="new_5xx",
                    endpoint=result.endpoint,
                    message=f"'{result.case_name}' returned {result.status_code}",
                    details=details,
                )
            )
        elif before is not None and before.passed and not result.passed:
            flags.append(
                Flag(
                    kind="newly_failing",
                    endpoint=result.endpoint,
                    message=f"'{result.case_name}' passed last run, now: {result.failure_reason}",
                    details=details,
                )
            )
    return flags
