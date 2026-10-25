"""End-to-end demo: baseline runs against a healthy target, then one run with faults on.

1. Starts ``target_api`` (uvicorn subprocess) with faults OFF and performs N baseline runs.
2. Restarts it with ``TARGET_API_FAULTS=1`` (200 ms delay + null bug) and performs one run.
3. Prints the slowed endpoint's p95 before/after, the flags raised, and writes
   ``docs/demo_results.json``. These are the numbers quoted in the README.

Usage::

    python scripts/run_demo.py [--baseline-runs 3] [--users 10] [--duration 15] [--port 9000]

Uses MONGODB_URI from the environment / .env (``mongomock://demo`` works without MongoDB,
but then the dashboard - a separate process - cannot see the runs).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db.models import RunDoc, RunStatus  # noqa: E402
from app.db.mongo_client import create_client  # noqa: E402
from app.db.repo import RunRepository  # noqa: E402
from app.generator.llm_client import LLMClient  # noqa: E402
from app.pipeline import execute_run  # noqa: E402

SLOW_ENDPOINT = "GET /items/{item_id}"


def start_target(port: int, faults: bool) -> subprocess.Popen[bytes]:
    """Start the demo API and wait until /health answers."""
    env = {**os.environ, "TARGET_API_FAULTS": "1" if faults else "0"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "target_api.app:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise SystemExit("target API did not start")


def stop_target(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the demo API."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def one_run(repo: RunRepository, base: str, users: int, duration: int) -> RunDoc:
    """Create and execute one run synchronously."""
    settings = get_settings()
    run = repo.create_run(
        RunDoc(spec_url=f"{base}/openapi.json", base_url=base, users=users, duration_s=duration)
    )
    run = asyncio.run(execute_run(run.id, repo, settings, LLMClient(settings)))
    if run.status != RunStatus.COMPLETED:
        raise SystemExit(f"run failed: {run.error}")
    return run


def p95_of(run: RunDoc, endpoint: str) -> float | None:
    """p95 of one endpoint in a run."""
    return next((s.p95_ms for s in run.load if s.endpoint == endpoint), None)


def main() -> int:
    """Run the demo."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-runs", type=int, default=3)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    settings = get_settings()
    repo = RunRepository(create_client(settings.mongodb_uri)[settings.mongodb_db])
    repo.ensure_indexes()
    # 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 first and adds ~2 s per
    # new connection when the server only listens on IPv4, which pollutes p99.
    base = f"http://127.0.0.1:{args.port}"

    baseline_runs: list[RunDoc] = []
    target = start_target(args.port, faults=False)
    try:
        for i in range(args.baseline_runs):
            run = one_run(repo, base, args.users, args.duration)
            baseline_runs.append(run)
            print(
                f"baseline run {i + 1}: {SLOW_ENDPOINT} p95={p95_of(run, SLOW_ENDPOINT)} ms, "
                f"failed cases={run.summary.failed}, flags={len(run.flags)}"
            )
    finally:
        stop_target(target)

    target = start_target(args.port, faults=True)
    try:
        faulty = one_run(repo, base, args.users, args.duration)
    finally:
        stop_target(target)

    before = [p for r in baseline_runs if (p := p95_of(r, SLOW_ENDPOINT)) is not None]
    failing = [r for r in repo.get_results(faulty.id) if not r.passed]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "machine": f"{platform.system()} {platform.release()}, Python {platform.python_version()}",
        "storage": settings.mongodb_uri.split("://")[0],
        "llm": "fallback (rule-based only)" if faulty.generation.llm_fallback else "used",
        "users": args.users,
        "duration_s": args.duration,
        "baseline_runs": args.baseline_runs,
        "slow_endpoint": SLOW_ENDPOINT,
        "baseline_p95_ms_per_run": before,
        "baseline_p95_ms_median": statistics.median(before) if before else None,
        "faulty_p95_ms": p95_of(faulty, SLOW_ENDPOINT),
        "baseline_failed_cases": [r.summary.failed for r in baseline_runs],
        "faulty_total_cases": faulty.summary.total,
        "faulty_failed_cases": [
            {
                "endpoint": r.endpoint,
                "case": r.case_name,
                "status": r.status_code,
                "body": r.request.get("body_json"),
            }
            for r in failing
        ],
        "flags": [f.model_dump() for f in faulty.flags],
        "load_faulty": [s.model_dump() for s in faulty.load],
        "load_last_baseline": [s.model_dump() for s in baseline_runs[-1].load],
        "run_ids": {"baseline": [r.id for r in baseline_runs], "faulty": faulty.id},
    }
    out = ROOT / "docs" / "demo_results.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(
        f"\nfaulty run: {SLOW_ENDPOINT} p95={report['faulty_p95_ms']} ms "
        f"(baseline median {report['baseline_p95_ms_median']} ms)"
    )
    print(f"failed cases: {len(failing)}")
    for r in failing:
        print(f"  - {r.endpoint} '{r.case_name}' -> {r.status_code}")
    print("flags:")
    for f in faulty.flags:
        print(f"  - [{f.kind}] {f.endpoint}: {f.message}")
    print(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
