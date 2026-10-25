"""Run a Locust load test (headless, programmatic) and collect per-endpoint latency stats.

Locust runs in a child interpreter (:mod:`app.runner.locust_worker`) because its
gevent monkey-patching would break the asyncio server that triggers runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from app.db.models import EndpointLoadStats
from app.generator.models import TestCase
from app.runner.functional import OriginGuard, build_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
STARTUP_GRACE_S = 60


class LoadTestError(RuntimeError):
    """The load-test subprocess failed."""


def select_targets(
    baseline_cases: list[TestCase], include_unsafe: bool = False
) -> list[dict[str, object]]:
    """Turn valid baseline cases into load-test targets (safe methods only by default)."""
    targets: list[dict[str, object]] = []
    for case in baseline_cases:
        if not include_unsafe and case.method not in SAFE_METHODS:
            continue
        targets.append(
            {
                "endpoint": case.endpoint,
                "method": case.method,
                "path": build_path(case),
                "query": case.query,
                "include_body": case.include_body,
                "body": case.body,
            }
        )
    return targets


def run_load_test(
    base_url: str,
    targets: list[dict[str, object]],
    users: int,
    duration_s: int,
) -> list[EndpointLoadStats]:
    """Run Locust against ``base_url`` for ``users`` users over ``duration_s`` seconds."""
    if not targets:
        return []
    guard = OriginGuard(base_url)
    for target in targets:
        path = str(target["path"])
        if not path.startswith("/") or path.startswith("//"):
            raise LoadTestError(f"refusing non-relative load target {path!r}")
        guard.check(httpx.URL(base_url).join(path))

    config = {"base_url": base_url, "targets": targets, "users": users, "duration_s": duration_s}
    with tempfile.TemporaryDirectory(prefix="sentinel-load-") as tmp:
        config_path = Path(tmp) / "config.json"
        output_path = Path(tmp) / "result.json"
        config_path.write_text(json.dumps(config, default=str), encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.runner.locust_worker",
                    str(config_path),
                    str(output_path),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=duration_s + STARTUP_GRACE_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LoadTestError(f"load test did not finish within {exc.timeout}s") from exc
        if proc.returncode != 0 or not output_path.exists():
            tail = (proc.stderr or "").strip().splitlines()[-5:]
            raise LoadTestError(f"locust worker exited with {proc.returncode}: {' | '.join(tail)}")
        data = json.loads(output_path.read_text(encoding="utf-8"))
    return [EndpointLoadStats.model_validate(e) for e in data["endpoints"]]
