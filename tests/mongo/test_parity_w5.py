"""The SQLite store test suite (tests/w5) re-run against the MongoDB backends.

The original test bodies are imported unchanged; only their SqliteStore / SqliteEventLog /
NumpyVectorIndex globals are swapped for Mongo-backed factories. SQLite-only tests (WAL pragma,
SQLite triggers, spawn workers that re-import the originals) have Mongo equivalents in
test_mongo_backend.py.
"""

from __future__ import annotations

import inspect

import pytest

from mongo_reuse import load_w5

STORE_TESTS = ["test_store_put_get_find", "test_eventlog_idempotent_monotonic_cursors"]
ORCH = load_w5("test_orchestrator")
ORCH_TESTS = sorted(n for n, f in vars(ORCH).items() if n.startswith("test_") and inspect.iscoroutinefunction(f))


def _patched(mod, monkeypatch, backends, names):
    for n in names:
        monkeypatch.setattr(mod, n, backends[n])
    return mod


@pytest.mark.parametrize("name", STORE_TESTS)
def test_w5_store_suite_on_mongo(name, tmp_path, monkeypatch, mongo_backends):
    mod = _patched(load_w5("test_store"), monkeypatch, mongo_backends, ["SqliteStore", "SqliteEventLog"])
    getattr(mod, name)(tmp_path)


def test_w5_vector_suite_on_atlas_vector_search(tmp_path, monkeypatch, mongo_backends, vector_search):
    mod = _patched(load_w5("test_vector_and_gates"), monkeypatch, mongo_backends, ["SqliteStore", "NumpyVectorIndex"])
    mod.test_vector_search_similarity_and_persistence(tmp_path)


def test_orchestrator_suite_discovered():
    assert len(ORCH_TESTS) >= 7


@pytest.mark.parametrize("name", ORCH_TESTS)
async def test_w5_orchestrator_suite_on_mongo(name, tmp_path, monkeypatch, mongo_backends):
    _patched(ORCH, monkeypatch, mongo_backends, ["SqliteStore", "SqliteEventLog"])
    await getattr(ORCH, name)(tmp_path)


@pytest.mark.parametrize("name", ["test_multi_generation_screening_invalid_and_retrieval",
                                  "test_noret_arm_skips_retrieval", "test_resume_after_crash_matches_clean_run"])
async def test_w5_orchestrator_all_mongo_ports(name, tmp_path, monkeypatch, mongo_backends, vector_search):
    """Store, EventLog and the proposer's retrieval index (Atlas Vector Search) all on Mongo."""
    import proofread.evolve.orchestrator as orch

    _patched(ORCH, monkeypatch, mongo_backends, ["SqliteStore", "SqliteEventLog"])
    monkeypatch.setattr(orch, "NumpyVectorIndex", mongo_backends["NumpyVectorIndex"])
    await getattr(ORCH, name)(tmp_path)
