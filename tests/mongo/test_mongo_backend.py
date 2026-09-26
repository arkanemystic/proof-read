"""Mongo-specific behaviour: shared connections, concurrent writers, seq visibility order, change feed
with persisted resume tokens, vector fields on rejected_edits."""

from __future__ import annotations

import multiprocessing as mp
import threading

import pytest

from proofread.contracts import EventLog, Store, VectorIndex
from proofread.store.mongo_store import AtlasVectorIndex, EditsChangeFeed, MongoEventLog, MongoStore, get_client
from proofread.store.vector import NumpyVectorIndex, embed


def test_two_instances_share_state(mongo_uri, mongo_dbs):
    db = mongo_dbs()
    a, b = MongoStore(mongo_uri, db), MongoStore(mongo_uri, db)
    assert isinstance(a, Store)
    a.put("episodes", "x", {"v": 1})
    assert b.get("episodes", "x") == {"v": 1}
    b.put("episodes", "x", {"v": 2})
    assert a.get("episodes", "x") == {"v": 2}


def test_documents_round_trip_like_sqlite(mongo_uri, mongo_dbs):
    s = MongoStore(mongo_uri, mongo_dbs())
    doc = {"a.b": 1, "s": "$not_a_path", "t": (1, 2), "f": 1.5, "n": None, "deep": {"x": [{"y": "$z"}]}}
    s.put("edits", "e", doc)
    assert s.get("edits", "e") == {"a.b": 1, "s": "$not_a_path", "t": [1, 2], "f": 1.5, "n": None,
                                   "deep": {"x": [{"y": "$z"}]}}
    s.put("edits", "own", {"_id": "own", "x": 1})  # documents that carry their own _id keep it
    s.put("edits", "odd", {"_id": 7, "x": 2})
    assert s.get("edits", "own") == {"_id": "own", "x": 1} and s.get("edits", "odd") == {"_id": 7, "x": 2}
    assert [d["x"] for d in s.find("edits", {"_id": "own"})] == [1] and s.find("edits", {"_id": 7})[0]["x"] == 2
    s.put("edits", "own", {"x": 3})
    assert s.get("edits", "own") == {"x": 3}
    s.delete("edits", "e")
    assert s.get("edits", "e") is None


def test_eventlog_api_is_append_only(mongo_uri, mongo_dbs):
    log = MongoEventLog(mongo_uri, mongo_dbs())
    assert isinstance(log, EventLog)
    assert not any(hasattr(log, m) for m in ("delete", "update", "remove"))


def _writer(uri: str, db: str, wid: int, n: int) -> None:
    log, store = MongoEventLog(uri, db), MongoStore(uri, db)
    for i in range(n):
        log.append("t", f"w{wid}:{i}", {"w": wid, "i": i})
        log.append("t", f"shared:{i}", {"w": wid})  # contended idempotency key
        store.put("episodes", f"w{wid}:{i}", {"w": wid})


def test_multiprocess_writers(mongo_uri, mongo_dbs):
    db = mongo_dbs()
    MongoEventLog(mongo_uri, db)
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(mongo_uri, db, w, 25)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    evs = MongoEventLog(mongo_uri, db).read(limit=0)
    seqs = [e.seq for e in evs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert len(evs) == 4 * 25 + 25 and len({e.key for e in evs}) == len(evs)
    assert len(MongoStore(mongo_uri, db).find("episodes")) == 100


def test_cursor_reader_never_skips_under_concurrent_appends(mongo_uri, mongo_dbs):
    """Events become visible in seq order, so a cursor-following reader sees every event exactly once."""
    db = mongo_dbs()
    log = MongoEventLog(mongo_uri, db)
    n_threads, per = 6, 30
    done = threading.Event()

    def app(t):
        lg = MongoEventLog(mongo_uri, db)
        for i in range(per):
            lg.append("t", f"{t}:{i}", {})

    seen: list[int] = []

    def reader():
        cur = 0
        while True:
            finished = done.is_set()
            for e in log.read(after_seq=cur):
                seen.append(e.seq)
                cur = e.seq
            if finished:
                return

    r = threading.Thread(target=reader)
    r.start()
    ws = [threading.Thread(target=app, args=(t,)) for t in range(n_threads)]
    for w in ws:
        w.start()
    for w in ws:
        w.join()
    done.set()
    r.join(60)
    assert sorted(seen) == seen and len(seen) == len(set(seen)) == n_threads * per


def test_edits_change_feed_resumes_from_persisted_token(mongo_uri, mongo_dbs):
    db = mongo_dbs()
    store = MongoStore(mongo_uri, db)
    feed = EditsChangeFeed("analysis", mongo_uri, db)  # token saved at open, before any change
    store.put("edits", "e1", {"id": "e1", "status": "proposed"})
    store.put("edits", "e2", {"id": "e2", "status": "proposed"})
    store.put("episodes", "ignored", {"x": 1})
    handled: list[tuple[str, str]] = []
    got = feed.poll(lambda c: handled.append((c["id"], c["doc"]["status"])), max_events=2, max_wait_s=10)
    assert [c["id"] for c in got] == ["e1", "e2"] and handled == [("e1", "proposed"), ("e2", "proposed")]
    feed.close()  # consumer "crashes"; changes keep happening meanwhile
    store.put("edits", "e1", {"id": "e1", "status": "promoted"})
    store.put("edits", "e3", {"id": "e3", "status": "rejected_formal"})
    feed2 = EditsChangeFeed("analysis", mongo_uri, db)
    got2 = feed2.poll(max_events=10, max_wait_s=3)
    assert [(c["id"], c["doc"]["status"]) for c in got2] == [("e1", "promoted"), ("e3", "rejected_formal")]
    other = EditsChangeFeed("dashboard", mongo_uri, db)  # independent consumer starts at "now"
    assert other.poll(max_events=10, max_wait_s=1) == []
    store.put("edits", "e4", {"id": "e4"})
    assert [c["id"] for c in other.poll(max_events=1, max_wait_s=10)] == ["e4"]
    assert [c["id"] for c in feed2.poll(max_events=1, max_wait_s=10)] == ["e4"]
    tokens = get_client(mongo_uri)[db]["stream_tokens"].count_documents({})
    assert tokens == 2
    feed2.close()
    other.close()


def test_change_feed_token_saved_before_first_event(mongo_uri, mongo_dbs):
    db = mongo_dbs()
    EditsChangeFeed("c", mongo_uri, db).close()  # opened, never polled
    MongoStore(mongo_uri, db).put("edits", "late", {"id": "late"})
    f = EditsChangeFeed("c", mongo_uri, db)
    assert [c["id"] for c in f.poll(max_events=1, max_wait_s=10)] == ["late"]
    f.close()


def test_atlas_vectors_live_on_rejected_edits(mongo_uri, mongo_dbs, vector_search):
    db = mongo_dbs()
    s = MongoStore(mongo_uri, db)
    idx = AtlasVectorIndex(s, namespace="rejected:armC")
    assert isinstance(idx, VectorIndex)
    raw = get_client(mongo_uri)[db]["rejected_edits"]
    ix = next(iter(raw.list_search_indexes(idx.index_name)))
    fields = ix["latestDefinition"]["fields"]
    assert {"type": "vector", "path": "embedding", "numDimensions": 512, "similarity": "cosine"} in fields
    # orchestrator order: put the rejected edit, then index it
    s.put("rejected_edits", "e1", {"id": "e1", "intent": "run tests after each edit"})
    idx.add("e1", embed("run tests after each edit"), {"intent": "run tests after each edit"})
    assert raw.find_one({"_id": "e1"})["embedding"][:3] == embed("run tests after each edit")[:3]
    s.put("rejected_edits", "e1", {"id": "e1", "intent": "run tests after each edit", "reason": "r"})
    assert "embedding" in raw.find_one({"_id": "e1"})  # put() after add() keeps the vector
    assert s.get("rejected_edits", "e1") == {"id": "e1", "intent": "run tests after each edit", "reason": "r"}
    # vector without a stored edit: hidden from the Store until the edit is put
    idx.add("e2", embed("shorten the prompt"), {"intent": "shorten the prompt"})
    assert s.get("rejected_edits", "e2") is None and [d["id"] for d in s.find("rejected_edits")] == ["e1"]
    s.put("rejected_edits", "e2", {"id": "e2"})
    assert [d["id"] for d in s.find("rejected_edits")] == ["e1", "e2"]
    assert len(idx) == 2 and len(AtlasVectorIndex(s, namespace="other")) == 0
    # scores are raw cosine, matching NumpyVectorIndex
    ref = NumpyVectorIndex()
    ref.add("e1", embed("run tests after each edit"))
    ref.add("e2", embed("shorten the prompt"))
    q = embed("run the tests after every edit")
    got, want = idx.search(q, k=2), ref.search(q, k=2)
    assert [h[0] for h in got] == [h[0] for h in want]
    assert all(abs(g[1] - w[1]) < 1e-4 for g, w in zip(got, want))
    with pytest.raises(ValueError):
        idx.add("bad", [0.1, 0.2])
