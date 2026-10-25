"""Command-line entry point: run API Sentinel without starting the API server.

Example::

    python -m app.cli run --spec-url http://localhost:9000/openapi.json \\
        --base-url http://localhost:9000 --users 10 --duration 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.config import get_settings
from app.db.models import RunDoc, RunStatus
from app.db.mongo_client import create_client
from app.db.repo import RunRepository
from app.generator.llm_client import LLMClient
from app.pipeline import execute_run


def _summarise(run: RunDoc) -> dict[str, object]:
    return {
        "id": run.id,
        "status": run.status.value,
        "error": run.error,
        "spec": run.spec_title,
        "cases": run.generation.total_cases,
        "llm_fallback": run.generation.llm_fallback,
        "summary": run.summary.model_dump(),
        "load": [s.model_dump() for s in run.load],
        "load_error": run.load_error,
        "flags": [f"[{f.kind}] {f.endpoint}: {f.message}" for f in run.flags],
    }


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and execute one run. Exit code 1 if the run failed or raised flags."""
    parser = argparse.ArgumentParser(prog="api-sentinel")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="execute one run and print its summary as JSON")
    run_p.add_argument("--spec-url", required=True)
    run_p.add_argument("--base-url", required=True)
    run_p.add_argument("--users", type=int, default=10)
    run_p.add_argument("--duration", type=int, default=10, help="load test duration (seconds)")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    repo = RunRepository(create_client(settings.mongodb_uri)[settings.mongodb_db])
    repo.ensure_indexes()
    run = repo.create_run(
        RunDoc(
            spec_url=args.spec_url,
            base_url=args.base_url.rstrip("/"),
            users=min(args.users, settings.max_users),
            duration_s=min(args.duration, settings.max_duration_s),
        )
    )
    run = asyncio.run(execute_run(run.id, repo, settings, LLMClient(settings)))
    print(json.dumps(_summarise(run), indent=2, default=str))
    return 0 if run.status == RunStatus.COMPLETED and not run.flags else 1


if __name__ == "__main__":
    sys.exit(main())
