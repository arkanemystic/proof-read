"""MongoDB backend: the SQLite store's contract tests run against Mongo, plus change streams, Atlas Vector
Search, the action sink, migration and a full orchestrator run.

Needs a MongoDB with transactions, change streams and (for the slow test) Atlas Vector Search: Atlas or the
mongodb/mongodb-atlas-local image. URI from MONGODB_TEST_URI, else MONGODB_URI (env or .env). Every test
uses its own throwaway database, dropped afterwards.
"""

import json
import multiprocessing as mp
import os
import threading
import time
import uuid

import pytest

pytest.importorskip("pymongo")

from proofread.agent.loop import Tracer  # noqa: E402
from proofread.contracts import EventLog, ModelResponse, ScriptedModelClient, Store, StubEpisodeRunner, Task, VectorIndex  # noqa: E402
from proofread.evolve.config import arm_preset  # noqa: E402
from proofread.evolve.orchestrator import run_arm  # noqa: E402
from proofread.store.migrate import migrate_sqlite_to_mongo  # noqa: E402
from proofread.store.mongo_store import (  # noqa: E402
    AtlasVectorIndex, MongoActionSink, MongoEventLog, MongoStore, get_client, settings,
)
from proofread.store.sqlite_store import SqliteEventLog, SqliteStore  # noqa: E402
from proofread.store.vector import NumpyVectorIndex, embed  # noqa: E402

pytestmark = pytest.mark.network

NAMES = {"harness_versions": "genomes", "events": "event_logs", "actions": "actions"}


def _uri() -> str:
    return os.environ.get("MONGODB_TEST_URI") or settings().get("MONGODB_URI", "")


@pytest.fixture(scope="module")
def uri():
    u = _uri()
    if not u:
        pytest.skip("no MONGODB_TEST_URI / MONGODB_URI")
    try:
        get_client(u).admin.command("ping")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"mongo unreachable: {type(e).__name__}")
    return u


@pytest.fixture
def db(uri):
    name = f"proofread_test_{uuid.uuid4().hex[:10]}"
    yield name
    get_client(uri).drop_database(name)


def _store(uri, db):
    return MongoStore(uri, db, names=NAMES)


def _log(uri, db):
    return MongoEventLog(uri, db, names=NAMES)


# ------------------------------------------------------------------ Store (same assertions as SqliteStore)


def test_store_put_get_find(uri, db):
    s = _store(uri, db)
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


def test_store_documents_identical_to_sqlite(uri, db, tmp_path):
    m, q = _store(uri, db), SqliteStore(tmp_path / "db.sqlite")
    docs = {
        "a": {"_id": "a", "run_id": "r", "tags": ["x", "y"], "files": {"func.py": "def f(): ...", "$weird": 1},
              "score": {"pass": 0.5}, "t": 1.25, "big": 2**53, "u": "héllo", "none": None},
        "b": {"run_id": "r", "tags": "x", "tuple": (1, 2)},
        "c": {"run_id": "other", "status": "$notanoperator"},
    }
    for k, d in docs.items():
        m.put("episodes", k, d)
        q.put("episodes", k, d)
    for k in docs:
        assert m.get("episodes", k) == q.get("episodes", k)
    for where in ({"run_id": "r"}, {"tags": "x"}, {"status": "$notanoperator"}, {}, {"none": None}):
        assert m.find("episodes", where) == q.find("episodes", where), where
    # genomes collection name mapping
    m.put("harness_versions", "v1", {"version": 1})
    assert m.db["genomes"].count_documents({}) == 1 and m.get("harness_versions", "v1") == {"version": 1}


# ------------------------------------------------------------------ EventLog


def test_eventlog_idempotent_monotonic_cursors(uri, db):
    log = _log(uri, db)
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
    log2 = _log(uri, db)
    assert log2.get_cursor("analysis") == s1
    assert [e.seq for e in log2.read(after_seq=log2.get_cursor("analysis"))] == [s2]
    s3 = log2.append("x", "k3", {})
    assert s3 > s2 and log.append("x", "k3", {"other": 1}) == s3
    assert log.get_by_key("k3").seq == s3 and log.get_by_key("missing") is None
    assert log.events.name == "event_logs"


def _writer(uri: str, db: str, wid: int, n: int) -> None:
    log = MongoEventLog(uri, db, names=NAMES)
    store = MongoStore(uri, db, names=NAMES)
    for i in range(n):
        log.append("t", f"w{wid}:{i}", {"w": wid, "i": i})
        log.append("t", f"shared:{i}", {"w": wid})  # contended idempotency key
        store.put("episodes", f"w{wid}:{i}", {"w": wid})


@pytest.mark.timeout(300)
def test_multiprocess_writers_gap_free(uri, db):
    _log(uri, db)
    n, w = 8, 4
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(uri, db, i, n)) for i in range(w)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(240)
        assert p.exitcode == 0
    evs = _log(uri, db).read(limit=0)
    assert [e.seq for e in evs] == list(range(1, w * n + n + 1))  # transactions: commit order, no gaps
    assert len({e.key for e in evs}) == len(evs)
    assert len(_store(uri, db).find("episodes")) == w * n


def test_watch_and_resume(uri, db):
    log = _log(uri, db)
    got: list = []
    ready = threading.Event()

    def consume():
        ready.set()
        for e in log.watch(consumer="dash", idle_timeout_s=6):
            got.append(e)
            if len(got) == 3:
                return

    t = threading.Thread(target=consume)
    t.start()
    ready.wait()
    time.sleep(1.5)  # let the change stream open
    seqs = [log.append("edit.decided", f"k{i}", {"i": i}) for i in range(3)]
    t.join(30)
    assert [e.seq for e in got] == seqs and log.get_cursor("dash") == seqs[-1]
    # while nobody watches, two more commit; a restarted consumer resumes exactly after the last one seen
    more = [log.append("edit.promoted", f"m{i}", {}) for i in range(2)]
    resumed = [e.seq for e in log.watch(consumer="dash", idle_timeout_s=4)]
    assert resumed == more


# ------------------------------------------------------------------ Vectors

TEXTS = {
    "e1": "run the tests more often after each edit to catch failures",
    "e2": "shorten the system prompt role section",
    "e3": "add a memory note about reading the docstring carefully",
}


def test_vector_exact_fallback_when_index_missing(uri, db):
    s = _store(uri, db)
    idx = AtlasVectorIndex(s, namespace="rejected:armC", index_name="does_not_exist", create_index=False)
    assert isinstance(idx, VectorIndex)
    for k, t in TEXTS.items():
        idx.add(k, embed(t), {"intent": t})
    hits = idx.search(embed("run tests after every edit"), k=2)
    assert idx.last_via == "exact" and hits[0][0] == "e1" and hits[0][1] > hits[1][1]
    ref = NumpyVectorIndex(s, namespace="rejected:armC")  # same layout: numpy index reads Atlas-written vectors
    assert [h[0] for h in ref.search(embed("run tests after every edit"), k=3)] == [h[0] for h in idx.search(
        embed("run tests after every edit"), k=3)]


@pytest.mark.slow
@pytest.mark.timeout(400)
def test_atlas_vector_search(uri, db):
    s = _store(uri, db)
    idx = AtlasVectorIndex(s, namespace="rejected:armC")
    for k, t in TEXTS.items():
        idx.add(k, embed(t), {"intent": t})
    AtlasVectorIndex(s, namespace="other").add("z", embed("unrelated other namespace"), {})
    if not idx.wait_until_queryable(300):
        pytest.skip("vector search index did not become queryable (not Atlas / search unavailable)")
    q = embed("run tests after every edit")
    for _ in range(30):  # new writes reach the search index within a few seconds
        hits = idx.search(q, k=3)
        if len(hits) == 3:
            break
        time.sleep(1)
    assert idx.last_via == "atlas"
    assert [h[0] for h in hits] == [h[0] for h in NumpyVectorIndex(s, namespace="rejected:armC").search(q, k=3)]
    exact = dict((d, sc) for d, sc, _ in idx._exact(q, 3))
    for d, sc, meta in hits:
        assert sc == pytest.approx(exact[d], abs=1e-4) and meta["intent"] == TEXTS[d]  # scores are cosine
    assert len(idx) == 3 and "z" not in [h[0] for h in hits]


# ------------------------------------------------------------------ action sink


def test_action_sink_writes_verified_actions(uri, db):
    sink = MongoActionSink(uri, db, names=NAMES)
    tr = Tracer(None, sink=sink)
    tr.write("meta", episode_id="C-abc", task_id="lcb/t1", variant="original", mode="observe", model="m", arm="C",
             generation=1, candidate_id="c0", seed=0)
    acts = [{"episode_id": "C-abc", "step": 1, "kind": "write", "path": "/workspace/func.py"},
            {"episode_id": "C-abc", "step": 2, "kind": "write", "path": "/workspace/test_func.py"}]
    tr.write("verify", stage="tool", round=0, actions=acts, failed=[[], ["CODE-TEST-001"]], detail="")
    tr.write("result", episode_id="C-abc")
    sink.flush()
    docs = list(sink.coll.find().sort("seq", 1))
    assert sink.coll.name == "actions" and sink.errors == 0 and len(docs) == 2
    assert docs[0]["ok"] and docs[0]["arm"] == "C" and docs[0]["task_id"] == "lcb/t1"
    assert not docs[1]["ok"] and docs[1]["failed_policies"] == ["CODE-TEST-001"]
    assert docs[1]["action"]["path"] == "/workspace/test_func.py"


# ------------------------------------------------------------------ migration


def test_migrate_sqlite_to_mongo(uri, db, tmp_path):
    p = tmp_path / "db.sqlite"
    qs, ql = SqliteStore(p), SqliteEventLog(p)
    for i in range(5):
        qs.put("edits", f"e{i}", {"id": f"e{i}", "status": "promoted" if i % 2 else "invalid", "run_id": "r"})
    qs.put("harness_versions", "r:v1", {"_id": "r:v1", "version": 1})
    NumpyVectorIndex(qs, namespace="rejected:r").add("e0", embed("run tests"), {"intent": "run tests"})
    seqs = [ql.append("edit.decided", f"r:e{i}:decided", {"i": i}) for i in range(3)]
    ql.set_cursor("analysis", seqs[1])
    ms, ml = _store(uri, db), _log(uri, db)
    out = migrate_sqlite_to_mongo(p, ms, ml, create_vector_index=False)
    assert out["docs"] == {"edits": 5, "harness_versions": 1, "vectors": 1}
    assert out["events"] == {"inserted": 3, "already": 0, "conflicts": 0}
    assert ms.find("edits") == qs.find("edits") and ms.find("edits", {"status": "promoted"}) == qs.find(
        "edits", {"status": "promoted"})
    assert ms.get("harness_versions", "r:v1") == {"_id": "r:v1", "version": 1}
    assert [(e.seq, e.key, e.payload) for e in ml.read()] == [(e.seq, e.key, e.payload) for e in ql.read()]
    assert ml.get_cursor("analysis") == seqs[1]
    assert ml.append("x", "new", {}) == seqs[-1] + 1  # counter continues after migrated events
    again = migrate_sqlite_to_mongo(p, ms, ml, create_vector_index=False)
    assert again["events"] == {"inserted": 0, "already": 3, "conflicts": 0} and len(ms.find("edits")) == 5
    assert AtlasVectorIndex(ms, "rejected:r", create_index=False).search(embed("run tests"), k=1)[0][0] == "e0"


# ------------------------------------------------------------------ orchestrator end to end on Mongo


def _tasks(split: str = "training") -> list[Task]:
    return [Task(id=f"lcb/original/t_{i}", variant="original", files={"func.py": "", "test.py": ""},
                 test_paths=["test.py"], spec="s", meta={"i": i}) for i in range(10)]


def _outcome(task, genome, mode, seed):
    notes = " ".join(genome.memory_notes)
    i = task.meta["i"]
    if "CHEAT" in notes:
        return True, (["CODE-TEST-001"] if i >= 3 else [])
    return i < 3, []


def _props(note):
    ops = [{"op": "add", "path": "/memory_notes/-", "value": note}]
    intent = f"intent for {json.dumps(ops)}"
    return [ModelResponse(text=json.dumps({"intent": intent}), cost_usd=0.01),
            ModelResponse(text="```json\n" + json.dumps({"intent": intent, "patch": ops, "rationale": "because"})
                          + "\n```", cost_usd=0.02)]


@pytest.mark.timeout(300)
async def test_orchestrator_runs_on_mongo(uri, db):
    store, log = _store(uri, db), _log(uri, db)
    res = {}
    for arm in ("A", "C"):
        cfg = arm_preset(arm, generations=1, candidates_per_generation=1, agent_model="stub", concurrency=4)
        client = ScriptedModelClient(responses=_props("CHEAT"), role="proposer")
        res[arm] = await run_arm(cfg, StubEpisodeRunner(_outcome), client, store, log, _tasks)
    assert store.get("edits", "armA-g0-c0")["status"] == "promoted" and res["A"]["champion"] == "armA:v2"
    assert store.get("edits", "armC-g0-c0")["status"] == "rejected_formal" and res["C"]["champion"] == "armC:v1"
    assert store.db["genomes"].count_documents({}) >= 3  # harness_versions -> genomes
    assert store.get("rejected_edits", "armC-g0-c0") is not None
    assert store.db["vectors"].count_documents({"namespace": "rejected:armC"}) == 1
    types = [e.type for e in log.read(limit=0)]
    assert "edit.decided" in types and "arm.finished" in types
    # resume: a second run of arm C reuses every stored decision and makes no new proposer calls
    client = ScriptedModelClient(responses=[], role="proposer")
    cfg = arm_preset("C", generations=1, candidates_per_generation=1, agent_model="stub", concurrency=4)
    assert (await run_arm(cfg, StubEpisodeRunner(_outcome), client, store, log, _tasks))["champion"] == "armC:v1"
    assert client.calls == []
