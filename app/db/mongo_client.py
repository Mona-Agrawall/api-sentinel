"""MongoDB connection factory.

``mongomock://...`` URIs return an in-memory mongomock client, which is handy for
tests and for trying the tool without a database (data is lost when the process exits).
"""

from __future__ import annotations

from typing import Any

from pymongo import MongoClient

SERVER_SELECTION_TIMEOUT_MS = 3000


def create_client(uri: str) -> MongoClient[dict[str, Any]]:
    """Create a Mongo client for ``uri`` (real or mongomock)."""
    if uri.startswith("mongomock://"):
        import mongomock

        client: MongoClient[dict[str, Any]] = mongomock.MongoClient(tz_aware=True)
        return client
    return MongoClient(uri, serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS, tz_aware=True)
