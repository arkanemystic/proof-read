from __future__ import annotations

import pytest

from proofread.models import config
from proofread.store import factory
from proofread.store.sqlite_store import SqliteEventLog, SqliteStore
from proofread.store.vector import NumpyVectorIndex


@pytest.fixture
def no_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGODB_DB", raising=False)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "missing.env")
    return tmp_path


@pytest.mark.nomongo
def test_sqlite_by_default(no_env):
    p = no_env / "db.sqlite"
    assert factory.backend() == "sqlite"
    s, log = factory.make_store(p), factory.make_eventlog(p)
    assert isinstance(s, SqliteStore) and isinstance(log, SqliteEventLog)
    assert isinstance(factory.make_vector_index(s, "ns"), NumpyVectorIndex)
    assert factory.DEFAULT_SQLITE_PATH == config.DATA_DIR / "proofread.sqlite"


@pytest.mark.nomongo
def test_env_file_is_read(no_env, monkeypatch):
    env = no_env / ".env"
    env.write_text("MONGODB_URI=mongodb://example.invalid:1/\nMONGODB_DB=alt\n")
    monkeypatch.setattr(config, "ENV_PATH", env)
    assert factory.backend() == "mongo" and factory.mongodb_db() == "alt"
    monkeypatch.setenv("MONGODB_URI", "mongodb://other.invalid:2/")
    assert factory.mongodb_uri() == "mongodb://other.invalid:2/"  # process env wins over .env


def test_mongo_when_uri_set(no_env, monkeypatch, mongo_uri, mongo_dbs, vector_search):
    from proofread.store.mongo_store import AtlasVectorIndex, MongoEventLog, MongoStore

    monkeypatch.setenv("MONGODB_URI", mongo_uri)
    monkeypatch.setenv("MONGODB_DB", mongo_dbs())
    s, log = factory.make_store(), factory.make_eventlog()
    assert isinstance(s, MongoStore) and isinstance(log, MongoEventLog)
    assert isinstance(factory.make_vector_index(s, "ns"), AtlasVectorIndex)
    s.put("edits", "a", {"id": "a"})
    assert factory.make_store().get("edits", "a") == {"id": "a"}
