from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from proofread.store.mongo_store import AtlasVectorIndex, MongoEventLog, MongoStore
from proofread.store.sqlite_store import SqliteEventLog, SqliteStore
from proofread.store.vector import NumpyVectorIndex, embed

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_sqlite_to_mongo.py"


def _script():
    spec = importlib.util.spec_from_file_location("_migrate_sqlite_to_mongo", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture_db(p: Path):
    s, log = SqliteStore(p), SqliteEventLog(p)
    for i in range(5):
        s.put("edits", f"e{i}", {"_id": f"e{i}", "id": f"e{i}", "gen": i,
                                 "status": "promoted" if i % 2 else "rejected_formal"})
        s.put("episodes", f"ep{i}", {"episode_id": f"ep{i}", "passed": bool(i % 2), "nested": {"v": [i]}})
    s.put("harness_versions", "armC:v1", {"id": "armC:v1", "genome": {"memory_notes": ["x"]}})
    s.put("actions", "a1", {"kind": "write", "path": "/workspace/func.py"})
    s.put("edits", "e0", {"_id": "e0", "id": "e0", "status": "invalid", "gen": 0})  # upsert keeps order
    idx = NumpyVectorIndex(s, namespace="rejected:armC")
    texts = {"e0": "run the tests after each edit", "e2": "shorten the system prompt", "e4": "read the docstring"}
    for k, t in texts.items():
        s.put("rejected_edits", k, {"id": k, "intent": t})
        idx.add(k, embed(t), {"intent": t})
    seqs = [log.append(f"t{i}", f"k{i}", {"i": i}) for i in range(7)]
    log.set_cursor("analysis", seqs[3])
    return s, log, idx, seqs, texts


def test_migration_copies_everything_and_is_idempotent(tmp_path, mongo_uri, mongo_dbs, vector_search):
    src = tmp_path / "src.sqlite"
    s, log, idx, seqs, texts = _fixture_db(src)
    before = src.stat().st_mtime_ns
    db = mongo_dbs()
    mig = _script()
    rep = mig.migrate(src, mongo_uri, db)
    assert rep["events"] == 7 and rep["cursors"] == 1 and rep["event_conflicts"] == []
    assert rep["docs"]["edits"] == 5 and rep["vectors"] == {"rejected:armC": 3}
    assert src.stat().st_mtime_ns == before  # source opened read-only

    m, mlog = MongoStore(mongo_uri, db), MongoEventLog(mongo_uri, db)
    for coll in ("edits", "episodes", "harness_versions", "actions", "rejected_edits", "vectors"):
        assert m.find(coll) == s.find(coll), coll
    assert [(e.seq, e.type, e.key, e.payload) for e in mlog.read(limit=0)] == \
           [(e.seq, e.type, e.key, e.payload) for e in log.read(limit=0)]
    assert mlog.get_cursor("analysis") == seqs[3] and mlog.get_by_key("k5").seq == seqs[5]
    aidx = AtlasVectorIndex(m, namespace="rejected:armC")
    q = embed("run tests after every edit")
    assert [h[0] for h in aidx.search(q, k=3)] == [h[0] for h in idx.search(q, k=3)]
    assert aidx.search(q, k=1)[0][2] == {"intent": texts["e0"]}

    # second run: idempotent (no duplicates, same content), then appends continue after max seq
    rep2 = mig.migrate(src, mongo_uri, db)
    assert rep2["events"] == 7 and rep2["event_conflicts"] == []
    assert m.find("edits") == s.find("edits") and len(mlog.read(limit=0)) == 7 and len(aidx) == 3
    assert mlog.append("new", "k-new", {}) == max(seqs) + 1
    assert mlog.append("dup", "k2", {}) == seqs[2]


def test_migration_reports_key_conflicts(tmp_path, mongo_uri, mongo_dbs):
    src = tmp_path / "src.sqlite"
    SqliteEventLog(src).append("t", "same-key", {})
    db = mongo_dbs()
    mlog = MongoEventLog(mongo_uri, db)
    mlog.append("t", "pre", {})
    mlog.append("t", "same-key", {})  # seq 2 in Mongo vs seq 1 in SQLite
    rep = _script().migrate(src, mongo_uri, db, vector_index=False)
    assert rep["event_conflicts"] == [{"key": "same-key", "sqlite_seq": 1, "mongo_seq": 2}]
    assert mlog.get_by_key("pre").seq == 1  # untouched
    src2 = tmp_path / "src2.sqlite"
    SqliteEventLog(src2).append("t", "fresh", {})  # seq 1, already used by "pre" in Mongo
    rep2 = _script().migrate(src2, mongo_uri, db, vector_index=False)
    assert rep2["event_conflicts"] == [{"key": "fresh", "sqlite_seq": 1, "mongo_key": "pre"}]
    assert mlog.get_by_key("pre").seq == 1 and mlog.get_by_key("fresh") is None


@pytest.mark.nomongo
def test_cli_dry_run(tmp_path, capsys, monkeypatch):
    import json

    src = tmp_path / "src.sqlite"
    _fixture_db(src)
    assert _script().main(["--sqlite", str(src), "--dry-run"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["events"] == 7 and rep["docs"]["rejected_edits"] == 3
