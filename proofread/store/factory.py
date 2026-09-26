"""Backend selection for the storage ports, for callers that build their own store (baselines runner,
scripts). A thin layer over proofread.store.backend, so every entry point agrees on the backend:

    STORAGE_BACKEND=mongodb         # default sqlite
    MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>/?retryWrites=true&w=majority
    MONGODB_DB_NAME=proofread       # optional, default "proofread" (MONGODB_DB also accepted)

in the process env or the repo .env.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from proofread.models import config
from proofread.store import backend as _backend
from proofread.store.sqlite_store import SqliteEventLog, SqliteStore

DEFAULT_SQLITE_PATH = config.DATA_DIR / "proofread.sqlite"


def _env_value(name: str) -> str | None:
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    if config.ENV_PATH.exists():
        from dotenv import dotenv_values

        v = (dotenv_values(config.ENV_PATH).get(name) or "").strip()
        return v or None
    return None


def mongodb_uri() -> str | None:
    return _env_value("MONGODB_URI")


def mongodb_db() -> str:
    return _env_value("MONGODB_DB_NAME") or _env_value("MONGODB_DB") or "proofread"


def backend() -> str:
    return "mongo" if _backend.backend_name() == "mongodb" else "sqlite"


def make_store(path: str | Path | None = None, *, uri: str | None = None, db: str | None = None) -> Any:
    """Store port. `path` is the SQLite file (ignored for Mongo); an explicit `uri` forces Mongo."""
    if uri or backend() == "mongo":
        from proofread.store.mongo_store import MongoStore

        return MongoStore(uri or mongodb_uri(), db or mongodb_db())
    return SqliteStore(path or DEFAULT_SQLITE_PATH)


def make_eventlog(path: str | Path | None = None, *, uri: str | None = None, db: str | None = None) -> Any:
    if uri or backend() == "mongo":
        from proofread.store.mongo_store import MongoEventLog

        return MongoEventLog(uri or mongodb_uri(), db or mongodb_db())
    return SqliteEventLog(path or DEFAULT_SQLITE_PATH)


def make_vector_index(store: Any = None, namespace: str = "default") -> Any:
    """VectorIndex port matching the store: Atlas Vector Search for a MongoStore, numpy otherwise."""
    return _backend.vector_index_for(store, namespace)
