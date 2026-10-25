"""Data access for runs and results."""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database

from app.db.models import CaseResult, ResultDoc, RunDoc, RunStatus, utcnow


class RunRepository:
    """All reads and writes of the ``runs`` and ``results`` collections."""

    def __init__(self, db: Database[dict[str, Any]]) -> None:
        self._db = db
        self.runs = db["runs"]
        self.results = db["results"]

    def ensure_indexes(self) -> None:
        """Create the indexes the queries below rely on (idempotent)."""
        self.runs.create_index([("created_at", DESCENDING)])
        self.runs.create_index(
            [("base_url", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)]
        )
        self.runs.create_index(
            [
                ("base_url", ASCENDING),
                ("status", ASCENDING),
                ("users", ASCENDING),
                ("duration_s", ASCENDING),
                ("created_at", DESCENDING),
            ]
        )
        self.results.create_index([("endpoint", ASCENDING), ("timestamp", DESCENDING)])
        self.results.create_index([("run_id", ASCENDING)])

    def ping(self) -> bool:
        """True if the database answers."""
        try:
            self._db.command("ping")
        except Exception:
            return False
        return True

    # --- runs ---------------------------------------------------------------------
    def create_run(self, run: RunDoc) -> RunDoc:
        """Insert a new run."""
        self.runs.insert_one(_to_mongo(run))
        return run

    def save_run(self, run: RunDoc) -> None:
        """Replace a run document with the given state."""
        self.runs.replace_one({"_id": run.id}, _to_mongo(run), upsert=True)

    def fail_interrupted_runs(self, message: str) -> int:
        """Mark every pending or running run as failed; returns how many were updated.

        Runs execute inside the API process, so at startup any such run belongs to a
        process that no longer exists and would otherwise never finish.
        """
        result = self.runs.update_many(
            {"status": {"$in": [RunStatus.PENDING.value, RunStatus.RUNNING.value]}},
            {"$set": {"status": RunStatus.FAILED.value, "error": message, "finished_at": utcnow()}},
        )
        return result.modified_count

    def get_run(self, run_id: str) -> RunDoc | None:
        """Fetch one run by id."""
        doc = self.runs.find_one({"_id": run_id})
        return _from_mongo(doc) if doc else None

    def list_runs(self, limit: int = 50, base_url: str | None = None) -> list[RunDoc]:
        """Most recent runs first."""
        query: dict[str, Any] = {"base_url": base_url} if base_url else {}
        cursor = self.runs.find(query).sort("created_at", DESCENDING).limit(limit)
        return [_from_mongo(d) for d in cursor]

    def previous_completed_runs(
        self, run: RunDoc, limit: int, *, same_load_profile: bool = True
    ) -> list[RunDoc]:
        """Completed runs against the same target created before ``run`` (newest first).

        With ``same_load_profile`` (the default) only runs with the same ``users`` and
        ``duration_s`` count, so latency is never compared across different load levels.
        """
        query: dict[str, Any] = {
            "base_url": run.base_url,
            "status": RunStatus.COMPLETED.value,
            "created_at": {"$lt": run.created_at},
            "_id": {"$ne": run.id},
        }
        if same_load_profile:
            query |= {"users": run.users, "duration_s": run.duration_s}
        cursor = self.runs.find(query).sort("created_at", DESCENDING).limit(limit)
        return [_from_mongo(d) for d in cursor]

    # --- results ------------------------------------------------------------------
    def insert_results(self, run: RunDoc, results: list[CaseResult]) -> None:
        """Store all case results of a run."""
        if not results:
            return
        docs = [
            ResultDoc(run_id=run.id, timestamp=run.started_at or run.created_at, **r.model_dump())
            for r in results
        ]
        self.results.insert_many([d.model_dump(mode="python") for d in docs])

    def get_results(self, run_id: str) -> list[ResultDoc]:
        """All results for a run, in insertion order."""
        cursor = self.results.find({"run_id": run_id}, {"_id": 0}).sort("_id", ASCENDING)
        return [ResultDoc.model_validate(d) for d in cursor]


def _to_mongo(run: RunDoc) -> dict[str, Any]:
    doc = run.model_dump(mode="python")
    doc["_id"] = doc.pop("id")
    doc["status"] = run.status.value
    return doc


def _from_mongo(doc: dict[str, Any]) -> RunDoc:
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    return RunDoc.model_validate(doc)
