"""Fixtures for the MongoDB backend tests.

Target: PROOFREAD_TEST_MONGODB_URI, else a local mongodb/mongodb-atlas-local container on
127.0.0.1:27018 (see notes/MONGO.md). MONGODB_URI is deliberately NOT used, so tests never touch a
real Atlas database. Every test uses its own throwaway database, dropped afterwards.
All tests here are marked `mongo` and skip cleanly when no replica-set Mongo is reachable.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest

DEFAULT_URIS = ("mongodb://127.0.0.1:27018/?directConnection=true",
                "mongodb://127.0.0.1:27017/?directConnection=true")

_probe: dict[str, object] = {}


def pytest_configure(config):
    config.addinivalue_line("markers", "mongo: needs a MongoDB replica set (atlas-local container); "
                                       "run explicitly with `uv run pytest -m mongo tests/mongo`")
    config.addinivalue_line("markers", "nomongo: test under tests/mongo that needs no MongoDB")


def pytest_collection_modifyitems(config, items):
    here = Path(__file__).parent
    for it in items:
        if Path(str(it.fspath)).is_relative_to(here) and not it.get_closest_marker("nomongo"):
            it.add_marker(pytest.mark.mongo)


def _discover() -> dict[str, object]:
    if _probe:
        return _probe
    try:
        from pymongo import MongoClient
    except ImportError:
        _probe.update(uri=None, why="pymongo not installed", vector=False)
        return _probe
    env = os.environ.get("PROOFREAD_TEST_MONGODB_URI")
    for uri in ([env] if env else list(DEFAULT_URIS)):
        try:
            c = MongoClient(uri, serverSelectionTimeoutMS=1500)
            hello = c.admin.command("hello")
        except Exception as e:  # unreachable
            _probe["why"] = f"no MongoDB reachable ({type(e).__name__})"
            continue
        if not hello.get("setName"):
            _probe.update(uri=None, why="MongoDB is not a replica set (transactions/change streams needed)")
            return _probe
        vector = True
        try:
            db = c[f"pr_probe_{uuid.uuid4().hex[:8]}"]
            db.create_collection("x")
            list(db["x"].list_search_indexes())
        except Exception:
            vector = False
        finally:
            c.drop_database(db.name)
        _probe.update(uri=uri, why="", vector=vector)
        return _probe
    _probe.setdefault("uri", None)
    return _probe


@pytest.fixture(scope="session")
def mongo_uri() -> str:
    p = _discover()
    if not p.get("uri"):
        pytest.skip(str(p.get("why") or "no MongoDB"))
    return str(p["uri"])


@pytest.fixture(scope="session")
def vector_search(mongo_uri) -> bool:
    if not _discover().get("vector"):
        pytest.skip("MongoDB reachable but Atlas Vector Search (mongot) is not; vector tests untested")
    return True


@pytest.fixture
def mongo_dbs(mongo_uri):
    """Maps SQLite-style paths to throwaway database names; drops them all at teardown."""
    from proofread.store.mongo_store import get_client

    created: set[str] = set()

    def name_for(key: object | None = None) -> str:
        k = str(key) if key is not None else uuid.uuid4().hex
        name = "pr_test_" + hashlib.sha1(k.encode()).hexdigest()[:20]
        created.add(name)
        return name

    yield name_for
    client = get_client(mongo_uri)
    for n in created:
        client.drop_database(n)


@pytest.fixture
def mongo_backends(mongo_uri, mongo_dbs):
    """Drop-in replacements for SqliteStore(path) / SqliteEventLog(path) / NumpyVectorIndex(...)."""
    from proofread.store.mongo_store import AtlasVectorIndex, MongoEventLog, MongoStore

    def store(path):
        return MongoStore(mongo_uri, mongo_dbs(Path(path).resolve()))

    def eventlog(path):
        return MongoEventLog(mongo_uri, mongo_dbs(Path(path).resolve()))

    def vector_index(store_=None, namespace="default"):
        s = store_ if store_ is not None else MongoStore(mongo_uri, mongo_dbs())
        return AtlasVectorIndex(s, namespace=namespace)

    return {"SqliteStore": store, "SqliteEventLog": eventlog, "NumpyVectorIndex": vector_index}
