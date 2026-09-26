"""PROOFREAD_STORE_BACKEND=sqlite pins the SQLite backend even when MONGODB_URI is configured."""

from proofread.store import factory
from proofread.store.sqlite_store import SqliteStore


def test_pin_overrides_mongodb_uri(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.invalid:1/")
    assert factory.backend() == "mongo"
    monkeypatch.setenv("PROOFREAD_STORE_BACKEND", "sqlite")
    assert factory.mongodb_uri() is None
    assert factory.backend() == "sqlite"
    assert isinstance(factory.make_store(tmp_path / "s.sqlite"), SqliteStore)
