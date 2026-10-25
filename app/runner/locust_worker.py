"""Locust load-test worker, run as a subprocess by :mod:`app.runner.load`.

Usage: ``python -m app.runner.locust_worker <config.json> <output.json>``

Importing Locust monkey-patches the process with gevent, which is incompatible with
the asyncio event loop of the API server - so this module must never be imported
there; it always runs in its own interpreter.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import gevent
from locust import HttpUser, between, events
from locust.env import Environment

PERCENTILES = (0.5, 0.95, 0.99)


def _validate_targets(base_url: str, targets: list[dict[str, Any]]) -> None:
    """Refuse any target that would resolve outside ``base_url``'s origin."""
    base = urlsplit(base_url)
    for target in targets:
        path = target["path"]
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"target path must be a relative path, got {path!r}")
        resolved = urlsplit(urljoin(base_url, path))
        if (resolved.scheme, resolved.netloc) != (base.scheme, base.netloc):
            raise ValueError(f"target {path!r} resolves outside {base.netloc}")


def _make_task(target: dict[str, Any]) -> Any:
    body = json.dumps(target["body"]).encode() if target["include_body"] else None
    headers = {"Content-Type": "application/json"} if body is not None else {}

    def task(user: HttpUser) -> None:
        with user.client.request(
            target["method"],
            target["path"],
            params=target["query"],
            data=body,
            headers=headers,
            name=target["endpoint"],
            catch_response=True,
        ) as response:
            # Only server errors and connection failures count as load-test errors;
            # e.g. a 404 for a missing resource is a valid answer under load.
            if response.status_code == 0 or response.status_code >= 500:
                response.failure(f"status {response.status_code}")
            else:
                response.success()

    return task


def run(config: dict[str, Any]) -> dict[str, Any]:
    """Run a headless Locust test and return per-endpoint stats."""
    base_url: str = config["base_url"]
    targets: list[dict[str, Any]] = config["targets"]
    _validate_targets(base_url, targets)

    class SentinelUser(HttpUser):
        host = base_url
        wait_time = between(0.05, 0.15)
        tasks = [_make_task(t) for t in targets]

    env = Environment(user_classes=[SentinelUser], events=events)
    runner = env.create_local_runner()
    runner.start(config["users"], spawn_rate=config["users"])
    gevent.spawn_later(config["duration_s"], runner.quit)
    runner.greenlet.join()

    endpoints = []
    for (name, _method), entry in env.stats.entries.items():
        if entry.num_requests == 0:
            continue
        p50, p95, p99 = (entry.get_response_time_percentile(p) for p in PERCENTILES)
        endpoints.append(
            {
                "endpoint": name,
                "requests": entry.num_requests,
                "failures": entry.num_failures,
                "error_rate": round(entry.num_failures / entry.num_requests, 4),
                "rps": round(entry.total_rps, 2),
                "p50_ms": float(p50),
                "p95_ms": float(p95),
                "p99_ms": float(p99),
                "avg_ms": round(entry.avg_response_time, 2),
                "max_ms": round(float(entry.max_response_time or 0), 2),
            }
        )
    return {"endpoints": endpoints}


def main(argv: list[str]) -> int:
    """CLI entry point."""
    logging.basicConfig(level=logging.WARNING)
    config = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    result = run(config)
    Path(argv[2]).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
