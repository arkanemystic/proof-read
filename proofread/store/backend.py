"""Backend selection: STORAGE_BACKEND=sqlite (default) or mongodb, from the process env or .env.

Only entry points (orchestrator CLI, proofread CLI) call open_backend/action_sink; library code and tests
receive their store explicitly, so tests never touch the network because of a value in .env.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from proofread.contracts import EventLog, Store, VectorIndex


def backend_name() -> str:
    v = os.environ.get("STORAGE_BACKEND")
    if v is None:
        from proofread.models.config import ENV_PATH

        if ENV_PATH.exists():
            from dotenv import dotenv_values

            v = dotenv_values(ENV_PATH).get("STORAGE_BACKEND")
    v = (v or "sqlite").strip().lower()
    return "mongodb" if v in ("mongo", "mongodb", "atlas") else "sqlite"


def open_backend(db_path: str | Path = "data/proofread.sqlite", backend: str | None = None) -> tuple[Store, EventLog]:
    if (backend or backend_name()) == "mongodb":
        from proofread.store.mongo_store import MongoEventLog, MongoStore

        return MongoStore(), MongoEventLog()
    from proofread.store.sqlite_store import SqliteEventLog, SqliteStore

    return SqliteStore(db_path), SqliteEventLog(db_path)


def vector_index_for(store: Any, namespace: str) -> VectorIndex:
    """Atlas Vector Search when the store is Mongo, else the numpy index persisted in the store."""
    try:
        from proofread.store.mongo_store import AtlasVectorIndex, MongoStore
    except ImportError:  # pymongo not installed
        MongoStore = None  # type: ignore[assignment]
    if MongoStore is not None and isinstance(store, MongoStore):
        return AtlasVectorIndex(store, namespace=namespace)
    from proofread.store.vector import NumpyVectorIndex

    return NumpyVectorIndex(store, namespace=namespace)


def action_sink(backend: str | None = None) -> Callable[[dict[str, Any]], None] | None:
    if (backend or backend_name()) == "mongodb":
        from proofread.store.mongo_store import MongoActionSink

        return MongoActionSink.shared()
    return None
