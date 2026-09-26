import multiprocessing as mp
import sqlite3

import pytest

from proofread.contracts import EventLog, Store
from proofread.store.sqlite_store import SqliteEventLog, SqliteStore


def test_store_put_get_find(tmp_path):
    s = SqliteStore(tmp_path / "db.sqlite")
    assert isinstance(s, Store)
    s.put("edits", "e1", {"id": "e1", "status": "promoted", "gen": 1, "ok": True, "x": None, "nested": {"a": [1]}})
    s.put("edits", "e2", {"id": "e2", "status": "rejected_formal", "gen": 1, "ok": False})
    s.put("edits", "e3", {"id": "e3", "status": "promoted", "gen": 2, "ok": True})
    assert s.get("edits", "e1")["nested"] == {"a": [1]}
    assert s.get("edits", "nope") is None
    assert [d["id"] for d in s.find("edits", {"status": "promoted"})] == ["e1", "e3"]
    assert [d["id"] for d in s.find("edits", {"gen": 1, "ok": False})] == ["e2"]
    assert [d["id"] for d in s.find("edits", {"ok": True}, limit=1)] == ["e1"]
    assert [d["id"] for d in s.find("edits", {"x": None})] == ["e1", "e2", "e3"]  # missing == None like InMemoryStore
    assert len(s.find("edits")) == 3 and s.find("episodes") == []
    s.put("edits", "e1", {"id": "e1", "status": "invalid"})  # upsert
    assert s.get("edits", "e1") == {"id": "e1", "status": "invalid"}
    assert [d["id"] for d in s.find("edits")] == ["e1", "e2", "e3"]  # upsert keeps insertion order


def test_store_two_connections_and_wal(tmp_path):
    p = tmp_path / "db.sqlite"
    a, b = SqliteStore(p), SqliteStore(p)
    a.put("episodes", "x", {"v": 1})
    assert b.get("episodes", "x") == {"v": 1}
    b.put("episodes", "x", {"v": 2})
    assert a.get("episodes", "x") == {"v": 2}
    mode = sqlite3.connect(p).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_eventlog_idempotent_monotonic_cursors(tmp_path):
    log = SqliteEventLog(tmp_path / "db.sqlite")
    assert isinstance(log, EventLog)
    s1 = log.append("edit.proposed", "r:e1:proposed", {"a": 1})
    s2 = log.append("edit.decided", "r:e1:decided", {"a": 2})
    again = log.append("edit.proposed", "r:e1:proposed", {"a": 999})
    assert s1 < s2 and again == s1
    evs = log.read()
    assert [e.seq for e in evs] == [s1, s2] and evs[0].payload == {"a": 1}
    assert [e.key for e in log.read(after_seq=s1)] == ["r:e1:decided"]
    assert len(log.read(limit=1)) == 1
    assert log.get_cursor("analysis") == 0
    log.set_cursor("analysis", s1)
    log2 = SqliteEventLog(tmp_path / "db.sqlite")
    assert log2.get_cursor("analysis") == s1
    assert [e.seq for e in log2.read(after_seq=log2.get_cursor("analysis"))] == [s2]
    s3 = log2.append("x", "k3", {})
    assert s3 > s2 and log.append("x", "k3", {"other": 1}) == s3
    assert log.get_by_key("k3").seq == s3 and log.get_by_key("missing") is None


def test_eventlog_append_only(tmp_path):
    p = tmp_path / "db.sqlite"
    log = SqliteEventLog(p)
    log.append("t", "k", {})
    c = sqlite3.connect(p)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM events")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE events SET type='z'")


def _writer(path: str, wid: int, n: int) -> None:
    log = SqliteEventLog(path)
    store = SqliteStore(path)
    for i in range(n):
        log.append("t", f"w{wid}:{i}", {"w": wid, "i": i})
        log.append("t", f"shared:{i}", {"w": wid})  # contended idempotency key
        store.put("episodes", f"w{wid}:{i}", {"w": wid})


def test_multiprocess_writers(tmp_path):
    path = str(tmp_path / "db.sqlite")
    SqliteEventLog(path)
    SqliteStore(path)
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(path, w, 40)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    evs = SqliteEventLog(path).read(limit=0)
    seqs = [e.seq for e in evs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert len(evs) == 4 * 40 + 40
    assert len(SqliteStore(path).find("episodes")) == 160
