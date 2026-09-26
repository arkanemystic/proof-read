"""Backend selection for the storage ports.

SQLite (default) unless MONGODB_URI is set, either in the process environment or in the repo .env
(read with the same dotenv loader the model client uses). Switching to Atlas is therefore only:

    MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>/?retryWrites=true&w=majority
    MONGODB_DB=proofread            # optional, default "proofread"

in .env. Nothing here is wired into running code paths; callers opt in by using these helpers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from proofread.models import config
from proofread.store.sqlite_store import SqliteEventLog, SqliteStore
from proofread.store.vector import NumpyVectorIndex

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
    # PROOFREAD_STORE_BACKEND=sqlite pins SQLite even when .env carries MONGODB_URI (used by runs that
    # must keep writing to their existing SQLite store, e.g. the section 10 replication retries).
    if (os.environ.get("PROOFREAD_STORE_BACKEND") or "").strip().lower() == "sqlite":
        return None
    return _env_value("MONGODB_URI")


def mongodb_db() -> str:
    return _env_value("MONGODB_DB") or "proofread"


def backend() -> str:
    return "mongo" if mongodb_uri() else "sqlite"


def make_store(path: str | Path | None = None, *, uri: str | None = None, db: str | None = None) -> Any:
    """Store port. `path` is the SQLite file (ignored for Mongo); `uri`/`db` override the environment."""
    uri = uri or mongodb_uri()
    if uri:
        from proofread.store.mongo_store import MongoStore

        return MongoStore(uri, db or mongodb_db())
    return SqliteStore(path or DEFAULT_SQLITE_PATH)


def make_eventlog(path: str | Path | None = None, *, uri: str | None = None, db: str | None = None) -> Any:
    uri = uri or mongodb_uri()
    if uri:
        from proofread.store.mongo_store import MongoEventLog

        return MongoEventLog(uri, db or mongodb_db())
    return SqliteEventLog(path or DEFAULT_SQLITE_PATH)


def make_vector_index(store: Any = None, namespace: str = "default", **kw: Any) -> Any:
    """VectorIndex port matching the store: Atlas Vector Search for a MongoStore, numpy otherwise."""
    from proofread.store.mongo_store import MongoStore

    if isinstance(store, MongoStore):
        from proofread.store.mongo_store import AtlasVectorIndex

        return AtlasVectorIndex(store, namespace=namespace, **kw)
    return NumpyVectorIndex(store, namespace=namespace)
