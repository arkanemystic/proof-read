"""MongoDB Atlas backends for the Store, EventLog and VectorIndex ports, plus an action sink.

Layout (one database, default "proofread"; logical names map to physical collections via
MONGODB_<NAME>_COLLECTION, e.g. harness_versions -> MONGODB_GENOMES_COLLECTION):
- Store: one collection per logical collection, `_id` = doc_id, documents stored as-is (after the same
  JSON normalization the SQLite store applies). One internal field `_pf` holds {ord, has_id, updated_at}:
  `ord` is an ObjectId set on first insert so `find` keeps insertion order across upserts, exactly like
  SQLite rowid. `_pf` (and `_id`, unless the caller put one) is stripped on read.
- EventLog: collection "event_logs" (configurable), `_id` = seq. append runs in a transaction that
  bumps a counters document and inserts the event, so concurrent writers serialize on the counter:
  seq order equals commit order (no reader ever skips an event), idempotency keys are unique, and a
  repeated key returns the original seq. watch() tails a change stream with persisted resume tokens.
- VectorIndex: collection "vectors" (same layout as NumpyVectorIndex persistence: namespace, doc_id,
  vector, meta) searched with Atlas Vector Search ($vectorSearch, cosine, filter on namespace).
  Scores are returned as cosine similarity (Atlas reports (1 + cos) / 2) so both indexes agree.
- Actions: collection "actions" receives every verified Action with its failed policies, streamed from
  the episode tracer by a background writer so the agent loop never waits on the network.

Connection string comes from MONGODB_URI (process env first, then .env). Switching between a local
Atlas image and Atlas proper is only that URI.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
import time
from typing import Any, Iterator

import numpy as np
from bson import ObjectId
from pymongo import ASCENDING, MongoClient, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError, OperationFailure
from pymongo.operations import SearchIndexModel
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from proofread.contracts import Event
from proofread.store.vector import DIM

log = logging.getLogger(__name__)

PF = "_pf"  # internal bookkeeping field on every stored document
DEFAULT_DB = "proofread"
# logical collection -> env var that may rename it
COLLECTION_ENV = {
    "harness_versions": "MONGODB_GENOMES_COLLECTION",
    "events": "MONGODB_EVENTS_COLLECTION",
    "actions": "MONGODB_ACTIONS_COLLECTION",
}
DEFAULT_NAMES = {"events": "event_logs"}
INDEXES: dict[str, list[list[tuple[str, int]]]] = {
    "edits": [[("status", ASCENDING)]],
    "episodes": [[("arm", ASCENDING), ("generation", ASCENDING)]],
}

_clients: dict[str, MongoClient] = {}
_clients_lock = threading.Lock()


def settings() -> dict[str, str]:
    """MONGODB_* and STORAGE_BACKEND from the process env, falling back to the repo .env (not exported)."""
    import os

    from dotenv import dotenv_values

    from proofread.models.config import ENV_PATH

    env = {k: (v or "") for k, v in (dotenv_values(ENV_PATH).items() if ENV_PATH.exists() else [])}
    keys = {"MONGODB_URI", "MONGODB_DB_NAME", "STORAGE_BACKEND", *COLLECTION_ENV.values()}
    return {k: (os.environ.get(k) or env.get(k) or "").strip() for k in keys}


def collection_names(cfg: dict[str, str] | None = None) -> dict[str, str]:
    cfg = settings() if cfg is None else cfg
    names = dict(DEFAULT_NAMES)
    for logical, var in COLLECTION_ENV.items():
        if cfg.get(var):
            names[logical] = cfg[var]
    return names


def get_client(uri: str) -> MongoClient:
    """One pooled client per URI per process (MongoClient is thread-safe; not fork-safe, so spawn only)."""
    with _clients_lock:
        c = _clients.get(uri)
        if c is None:
            c = MongoClient(uri, appname="proofread", serverSelectionTimeoutMS=15000, retryWrites=True, tz_aware=False)
            _clients[uri] = c
        return c


def _normalize(doc: Any) -> Any:
    # Same JSON round trip as the SQLite store so both backends return identical documents.
    return json.loads(json.dumps(doc, sort_keys=True, ensure_ascii=False, default=str))


class _Mongo:
    def __init__(self, uri: str | None = None, db: str | None = None, *, names: dict[str, str] | None = None,
                 client: MongoClient | None = None) -> None:
        cfg = settings() if (uri is None or db is None or names is None) else {}
        self.uri = uri or cfg.get("MONGODB_URI", "")
        if not self.uri and client is None:
            raise RuntimeError("MONGODB_URI is not set (process env or .env)")
        self.client = client or get_client(self.uri)
        self.db_name = db or cfg.get("MONGODB_DB_NAME") or DEFAULT_DB
        self.db = self.client[self.db_name]
        self.names = dict(collection_names(cfg) if names is None else names)

    def coll(self, logical: str):
        return self.db[self.names.get(logical, logical)]


# ------------------------------------------------------------------------------------------ Store


class MongoStore(_Mongo):
    """contracts.Store over MongoDB. `find` is equality on top-level fields (same as SqliteStore)."""

    def __init__(self, uri: str | None = None, db: str | None = None, **kw: Any) -> None:
        super().__init__(uri, db, **kw)
        self._indexed: set[str] = set()
        self._lock = threading.Lock()

    def _ensure_indexes(self, collection: str) -> None:
        if collection in self._indexed:
            return
        with self._lock:
            if collection in self._indexed:
                return
            c = self.coll(collection)
            c.create_index([(f"{PF}.ord", ASCENDING)], name="pf_ord")
            c.create_index([("run_id", ASCENDING), (f"{PF}.ord", ASCENDING)], name="run_id_ord")
            for keys in INDEXES.get(collection, []):
                c.create_index(keys)
            self._indexed.add(collection)

    @staticmethod
    def _op(doc_id: str, doc: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        body = _normalize(doc)
        had_id = "_id" in body
        body.pop("_id", None)
        body.pop(PF, None)
        # Pipeline update: replace the document but keep the original insertion ordinal (atomic upsert).
        pipeline = [{"$replaceWith": {"$mergeObjects": [
            {"$literal": body},
            {"_id": doc_id, PF: {"ord": {"$ifNull": [f"${PF}.ord", {"$literal": ObjectId()}]},
                                 "has_id": had_id, "updated_at": time.time()}},
        ]}}]
        return {"_id": doc_id}, pipeline

    def put(self, collection: str, doc_id: str, doc: dict[str, Any]) -> None:
        self._ensure_indexes(collection)
        flt, pipeline = self._op(doc_id, doc)
        self.coll(collection).update_one(flt, pipeline, upsert=True)

    def put_many(self, collection: str, items: list[tuple[str, dict[str, Any]]], batch: int = 500) -> int:
        self._ensure_indexes(collection)
        n = 0
        for i in range(0, len(items), batch):
            ops = [UpdateOne(*self._op(doc_id, doc), upsert=True) for doc_id, doc in items[i:i + batch]]
            if ops:
                self.coll(collection).bulk_write(ops, ordered=True)
                n += len(ops)
        return n

    @staticmethod
    def _strip(d: dict[str, Any]) -> dict[str, Any]:
        meta = d.pop(PF, None) or {}
        if not meta.get("has_id"):
            d.pop("_id", None)
        return d

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        d = self.coll(collection).find_one({"_id": doc_id})
        return self._strip(d) if d is not None else None

    def find(self, collection: str, where: dict[str, Any] | None = None, limit: int = 0) -> list[dict[str, Any]]:
        where = where or {}
        q: dict[str, Any] = {}
        for k, v in where.items():
            # Server prefilter for scalar values; the exact Python equality check below is authoritative
            # (Mongo equality also matches array members and treats missing fields as null).
            if isinstance(v, str) or (isinstance(v, (int, float)) and not isinstance(v, bool)):
                q[k] = v
        out = []
        for d in self.coll(collection).find(q).sort(f"{PF}.ord", ASCENDING):
            d = self._strip(d)
            if all(d.get(k) == v for k, v in where.items()):
                out.append(d)
                if limit and len(out) >= limit:
                    break
        return out

    def delete(self, collection: str, doc_id: str) -> None:
        self.coll(collection).delete_one({"_id": doc_id})

    def close(self) -> None:
        pass  # the pooled client is shared per process


# ------------------------------------------------------------------------------------------ EventLog


class MongoEventLog(_Mongo):
    """contracts.EventLog over MongoDB: append-only, monotonic gap-free-ordered seq, idempotency keys,
    named cursors, and a change-stream tail (watch) with persisted resume tokens."""

    COUNTERS = "counters"
    CURSORS = "cursors"

    def __init__(self, uri: str | None = None, db: str | None = None, **kw: Any) -> None:
        super().__init__(uri, db, **kw)
        self.events = self.coll("events")
        self.counters = self.db[self.COUNTERS]
        self.cursors = self.db[self.CURSORS]
        self.events.create_index([("key", ASCENDING)], unique=True, name="key_unique")
        self.events.create_index([("type", ASCENDING), ("_id", ASCENDING)], name="type_seq")
        self._counter_id = f"seq:{self.events.name}"

    @staticmethod
    def _event(d: dict[str, Any]) -> Event:
        return Event(seq=int(d["_id"]), type=d["type"], key=d["key"], payload=d.get("payload") or {}, ts=d["ts"])

    def append(self, type: str, key: str, payload: dict[str, Any]) -> int:
        body = _normalize(payload)

        def txn(session) -> int:
            ex = self.events.find_one({"key": key}, {"_id": 1}, session=session)
            if ex is not None:
                return int(ex["_id"])
            c = self.counters.find_one_and_update({"_id": self._counter_id}, {"$inc": {"seq": 1}}, upsert=True,
                                                  return_document=ReturnDocument.AFTER, session=session)
            seq = int(c["seq"])
            self.events.insert_one({"_id": seq, "type": type, "key": key, "payload": body, "ts": time.time()},
                                   session=session)
            return seq

        for _ in range(5):
            try:
                with self.client.start_session() as s:
                    return s.with_transaction(txn, read_concern=ReadConcern("snapshot"),
                                              write_concern=WriteConcern("majority"))
            except DuplicateKeyError:
                ex = self.events.find_one({"key": key}, {"_id": 1})
                if ex is not None:
                    return int(ex["_id"])
        raise RuntimeError(f"event append for key {key!r} did not settle")

    def read(self, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        cur = self.events.find({"_id": {"$gt": int(after_seq)}}).sort("_id", ASCENDING)
        if limit > 0:
            cur = cur.limit(limit)
        return [self._event(d) for d in cur]

    def get_by_key(self, key: str) -> Event | None:
        d = self.events.find_one({"key": key})
        return self._event(d) if d is not None else None

    def get_cursor(self, consumer: str) -> int:
        d = self.cursors.find_one({"_id": consumer}, {"seq": 1})
        return int(d["seq"]) if d and "seq" in d else 0

    def set_cursor(self, consumer: str, seq: int) -> None:
        self.cursors.update_one({"_id": consumer}, {"$set": {"seq": int(seq)}}, upsert=True)

    def watch(self, consumer: str | None = None, types: list[str] | None = None,
              max_await_ms: int = 1000, idle_timeout_s: float | None = None) -> Iterator[Event]:
        """Yield newly inserted events as they commit (change stream). With `consumer`, the resume token
        and seq are persisted after each event so a restarted watcher continues where it stopped."""
        match: dict[str, Any] = {"operationType": "insert"}
        if types:
            match["fullDocument.type"] = {"$in": list(types)}
        token = None
        if consumer:
            d = self.cursors.find_one({"_id": consumer}) or {}
            token = d.get("resume_token")
        last = time.time()
        with self.events.watch([{"$match": match}], resume_after=token, max_await_time_ms=max_await_ms) as stream:
            while stream.alive:
                ch = stream.try_next()
                if ch is None:
                    if idle_timeout_s is not None and time.time() - last > idle_timeout_s:
                        return
                    continue
                last = time.time()
                ev = self._event(ch["fullDocument"])
                if consumer:
                    self.cursors.update_one({"_id": consumer}, {"$set": {"resume_token": stream.resume_token,
                                                                         "seq": ev.seq}}, upsert=True)
                yield ev

    def close(self) -> None:
        pass


# ------------------------------------------------------------------------------------------ Vectors


class AtlasVectorIndex:
    """contracts.VectorIndex over Atlas Vector Search. Documents live in the store's "vectors"
    collection in the NumpyVectorIndex layout, so either index can read what the other wrote.

    If the search index is missing or still building, search falls back to exact cosine over the
    namespace (numpy) and records `last_via = "exact"`; otherwise `last_via = "atlas"`. Atlas indexes new
    writes within about a second, so a vector added a moment ago may not be returned yet."""

    COLLECTION = "vectors"

    def __init__(self, store: MongoStore, namespace: str = "default", index_name: str = "intent_vectors",
                 dims: int = DIM, create_index: bool = True) -> None:
        self.store = store
        self.namespace = namespace
        self.index_name = index_name
        self.dims = dims
        self.coll = store.coll(self.COLLECTION)
        self.last_via = ""
        self._ready = False
        if create_index:
            self.ensure_search_index()

    def _definition(self) -> dict[str, Any]:
        return {"fields": [
            {"type": "vector", "path": "vector", "numDimensions": self.dims, "similarity": "cosine"},
            {"type": "filter", "path": "namespace"},
        ]}

    def search_index_status(self) -> dict[str, Any] | None:
        try:
            for idx in self.coll.list_search_indexes(self.index_name):
                return {"name": idx.get("name"), "status": idx.get("status"), "queryable": bool(idx.get("queryable"))}
        except OperationFailure as e:
            return {"name": self.index_name, "status": f"unavailable: {e.code}", "queryable": False}
        return None

    def ensure_search_index(self) -> None:
        st = self.search_index_status()
        if st is None:
            if self.COLLECTION not in self.store.db.list_collection_names(filter={"name": self.COLLECTION}):
                self.store.db.create_collection(self.COLLECTION)
            try:
                self.coll.create_search_index(SearchIndexModel(definition=self._definition(), name=self.index_name,
                                                               type="vectorSearch"))
            except OperationFailure as e:  # e.g. already being created by a concurrent process
                log.warning("create_search_index %s: %s", self.index_name, e)
        self._ready = bool(st and st.get("queryable"))

    def wait_until_queryable(self, timeout_s: float = 180.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            st = self.search_index_status()
            if st and st.get("queryable"):
                self._ready = True
                return True
            time.sleep(2)
        return False

    def __len__(self) -> int:
        return self.coll.count_documents({"namespace": self.namespace})

    def add(self, doc_id: str, vector: list[float], meta: dict[str, Any] | None = None) -> None:
        self.store.put(self.COLLECTION, f"{self.namespace}:{doc_id}",
                       {"namespace": self.namespace, "doc_id": doc_id, "vector": list(map(float, vector)),
                        "meta": dict(meta or {})})

    def search(self, vector: list[float], k: int = 5) -> list[tuple[str, float, dict[str, Any]]]:
        if k <= 0:
            return []
        if not self._ready:
            st = self.search_index_status()
            self._ready = bool(st and st.get("queryable"))
        if self._ready:
            try:
                pipeline = [
                    {"$vectorSearch": {"index": self.index_name, "path": "vector", "queryVector": list(map(float, vector)),
                                       "numCandidates": min(10000, max(100, 20 * k)), "limit": k,
                                       "filter": {"namespace": self.namespace}}},
                    {"$project": {"_id": 0, "doc_id": 1, "meta": 1, "score": {"$meta": "vectorSearchScore"}}},
                ]
                hits = list(self.coll.aggregate(pipeline))
                self.last_via = "atlas"
                return [(h["doc_id"], 2.0 * float(h["score"]) - 1.0, h.get("meta") or {}) for h in hits]
            except OperationFailure as e:
                log.warning("$vectorSearch failed (%s); using exact cosine", e)
        return self._exact(vector, k)

    def _exact(self, vector: list[float], k: int) -> list[tuple[str, float, dict[str, Any]]]:
        self.last_via = "exact"
        docs = list(self.coll.find({"namespace": self.namespace}, {"doc_id": 1, "vector": 1, "meta": 1})
                    .sort(f"{PF}.ord", ASCENDING))
        if not docs:
            return []
        m = np.asarray([d["vector"] for d in docs], dtype=np.float64)
        m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-300)
        q = np.asarray(vector, dtype=np.float64)
        q /= max(float(np.linalg.norm(q)), 1e-300)
        sims = m @ q
        order = np.argsort(-sims, kind="stable")[:k]
        return [(docs[i]["doc_id"], float(sims[i]), docs[i].get("meta") or {}) for i in order]


# ------------------------------------------------------------------------------------------ Actions


class MongoActionSink:
    """Tracer sink: turns each "verify" trace record into one document per Action in the actions
    collection, tagged with the episode context from the "meta" record. Writes happen on a background
    thread; failures are logged and counted, never raised into the agent loop (the JSONL trace stays
    the source of truth)."""

    _shared: "MongoActionSink | None" = None

    def __init__(self, uri: str | None = None, db: str | None = None, **kw: Any) -> None:
        self.m = _Mongo(uri, db, **kw)
        self.coll = self.m.coll("actions")
        self.coll.create_index([("episode_id", ASCENDING), ("seq", ASCENDING)], name="episode_seq")
        self.coll.create_index([("failed_policies", ASCENDING)], name="failed_policies")
        self._q: queue.Queue = queue.Queue()
        self._ctx: dict[str, dict[str, Any]] = {}
        self._n: dict[str, int] = {}
        self.errors = 0
        self.written = 0
        self._t = threading.Thread(target=self._run, name="mongo-action-sink", daemon=True)
        self._t.start()
        atexit.register(self.flush)

    @classmethod
    def shared(cls) -> "MongoActionSink":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def __call__(self, rec: dict[str, Any]) -> None:
        t = rec.get("type")
        if t == "meta":
            ep = rec.get("episode_id", "")
            self._ctx[ep] = {k: rec.get(k) for k in ("episode_id", "task_id", "variant", "mode", "model", "arm",
                                                     "generation", "candidate_id", "seed")}
            return
        if t == "result":
            ep = rec.get("episode_id", "")
            self._ctx.pop(ep, None)
            self._n.pop(ep, None)
            return
        if t != "verify":
            return
        actions = rec.get("actions") or []
        failed = rec.get("failed") or [[] for _ in actions]
        docs = []
        for a, f in zip(actions, failed):
            ep = a.get("episode_id", "")
            n = self._n.get(ep, 0)
            self._n[ep] = n + 1
            docs.append({"_id": f"{ep}:{n:05d}", "seq": n, **self._ctx.get(ep, {"episode_id": ep}), "action": a,
                         "kind": a.get("kind"), "path": a.get("path"), "failed_policies": list(f), "ok": not f,
                         "stage": rec.get("stage"), "round": rec.get("round"), "ts": rec.get("ts", time.time())})
        if docs:
            self._q.put(docs)

    def _run(self) -> None:
        while True:
            docs = self._q.get()
            try:
                self.coll.insert_many(docs, ordered=False)
                self.written += len(docs)
            except Exception as e:  # noqa: BLE001 - never break the agent loop over telemetry
                errs = (getattr(e, "details", None) or {}).get("writeErrors") or []
                if not (errs and all(err.get("code") == 11000 for err in errs)):  # duplicates = already written
                    self.errors += 1
                    log.warning("action sink insert failed: %s", e)
            finally:
                self._q.task_done()

    def flush(self, timeout_s: float = 30.0) -> None:
        t0 = time.time()
        while self._q.unfinished_tasks and time.time() - t0 < timeout_s:
            time.sleep(0.05)
