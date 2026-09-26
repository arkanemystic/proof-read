"""MongoDB (Atlas or atlas-local) backends for the Store, EventLog and VectorIndex ports.

Mapping from the SQLite layout (proofread/store/sqlite_store.py, proofread/store/vector.py):

- Store: one Mongo collection per logical collection, `_id = doc_id`, documents stored as-is. Documents
  are normalised through the same JSON round trip as SQLite (sort_keys, default=str), so both backends
  return identical Python values. `find` keeps insertion order (a hidden `_ord` ObjectId set on first
  insert and preserved by upserts) and uses Python equality as the authoritative filter, exactly like
  SqliteStore. Hidden fields (`_id`, `_ord`, `_uid`, `embedding`, `_vec_*`) are never returned; a document
  that carries its own `_id` field gets it back unchanged (kept in `_uid`).
- EventLog: collection `events` (`_id = seq`, unique index on `key`) plus a `counters` document
  advanced with `$inc`. Allocation and insert happen in one multi-document transaction, so the counter
  document serialises appenders and events become visible in seq order (a reader never sees seq n+1
  before seq n). Named cursors live in `cursors`. Requires a replica set (Atlas, atlas-local, or
  `mongod --replSet`).
- EditsChangeFeed: change stream on `edits` whose resume tokens are persisted in `stream_tokens`, so a
  restarted consumer resumes exactly after the last change it handled (at-least-once delivery).
- AtlasVectorIndex: Atlas Vector Search (`$vectorSearch`) over `rejected_edits.embedding` (cosine,
  numDimensions = proofread.store.vector.DIM), with a `_vec_ns` filter field for the namespace.
  Scores are converted back to raw cosine (Atlas reports (1 + cos) / 2) so results match
  NumpyVectorIndex.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure
from pymongo.operations import SearchIndexModel
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from proofread.contracts import Event
from proofread.store.vector import DIM

DEFAULT_DB = "proofread"
HIDDEN_FIELDS = ("_id", "_ord", "embedding", "_vec_ns", "_vec_meta", "_vec_orphan", "_uid")
_PROJECTION = {f: 0 for f in HIDDEN_FIELDS}

_clients: dict[str, MongoClient] = {}
_clients_lock = threading.Lock()


def get_client(uri: str, **kw: Any) -> MongoClient:
    """One MongoClient per URI per process (MongoClient is thread-safe and pools connections)."""
    with _clients_lock:
        c = _clients.get(uri)
        if c is None:
            kw.setdefault("serverSelectionTimeoutMS", 10_000)
            kw.setdefault("tz_aware", False)
            c = _clients[uri] = MongoClient(uri, **kw)
        return c


def _database(uri: str | None, db: str | Database, client: MongoClient | None) -> Database:
    if isinstance(db, Database):
        return db
    if client is None:
        if not uri:
            raise ValueError("MongoDB backend needs a uri (or client / Database)")
        client = get_client(uri)
    return client[db]


def _normalise(doc: Any) -> Any:
    """Same JSON round trip as SqliteStore, so both backends hand back identical values."""
    return json.loads(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str))


def _strip(d: dict[str, Any]) -> dict[str, Any]:
    uid = d.get("_uid")
    for f in HIDDEN_FIELDS:
        d.pop(f, None)
    if isinstance(uid, dict) and "v" in uid:  # the stored document had its own "_id" field
        d["_id"] = uid["v"]
    return d


def _prefilterable(v: Any) -> bool:
    return isinstance(v, str) or (isinstance(v, (int, float)) and not isinstance(v, bool))


# --------------------------------------------------------------------------------------------- Store


class MongoStore:
    """contracts.Store over MongoDB. `find` is equality on top-level fields (same as SqliteStore)."""

    def __init__(self, uri: str | None = None, db: str | Database = DEFAULT_DB, *,
                 client: MongoClient | None = None) -> None:
        self.db = _database(uri, db, client)
        self._indexed: set[str] = set()

    def _coll(self, collection: str) -> Collection:
        c = self.db[collection]
        if collection not in self._indexed:
            c.create_index([("_ord", ASCENDING)], name="ord")
            self._indexed.add(collection)
        return c

    def put(self, collection: str, doc_id: str, doc: dict[str, Any]) -> None:
        body = _normalise(doc)
        if "_id" in body:  # Mongo's _id is the doc_id; keep the caller's own "_id" value aside
            body["_uid"] = {"v": body.pop("_id")}
        body["_id"] = doc_id
        # Atomic replace that keeps the insertion-order key and any vector fields written by
        # AtlasVectorIndex (so put() after add() does not drop the embedding). $literal keeps user
        # strings starting with "$" from being read as field paths.
        keep = {"_ord": {"$ifNull": ["$_ord", ObjectId()]}, "embedding": "$embedding",
                "_vec_ns": "$_vec_ns", "_vec_meta": "$_vec_meta"}
        self._coll(collection).update_one(
            {"_id": doc_id}, [{"$replaceWith": {"$mergeObjects": [keep, {"$literal": body}]}}], upsert=True)

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        d = self.db[collection].find_one({"_id": doc_id, "_vec_orphan": {"$exists": False}})
        return _strip(d) if d is not None else None

    def find(self, collection: str, where: dict[str, Any] | None = None, limit: int = 0) -> list[dict[str, Any]]:
        where = where or {}
        q: dict[str, Any] = {"_vec_orphan": {"$exists": False}}
        for k, v in where.items():
            if not _prefilterable(v) or k.startswith("$"):
                continue
            if k == "_id":
                q["_uid.v"] = v  # the document's own "_id" field, not Mongo's key
            elif k not in HIDDEN_FIELDS:
                q[k] = v  # server-side prefilter; the Python check below is authoritative
        out = []
        for d in self._coll(collection).find(q).sort([("_ord", ASCENDING), ("_id", ASCENDING)]):
            d = _strip(d)
            if all(d.get(k) == v for k, v in where.items()):
                out.append(d)
                if limit and len(out) >= limit:
                    break
        return out

    def delete(self, collection: str, doc_id: str) -> None:
        self.db[collection].delete_one({"_id": doc_id})

    def collections(self) -> list[str]:
        return sorted(n for n in self.db.list_collection_names() if not n.startswith("system."))

    def close(self) -> None:
        pass  # clients are shared per URI; see get_client


# ------------------------------------------------------------------------------------------ EventLog


def _event(d: dict[str, Any]) -> Event:
    return Event(seq=int(d["seq"]), type=d["type"], key=d["key"], payload=d["payload"], ts=float(d["ts"]))


class MongoEventLog:
    """contracts.EventLog over MongoDB: append-only API, monotonic seq, idempotency keys, named cursors."""

    COUNTER_ID = "events"

    def __init__(self, uri: str | None = None, db: str | Database = DEFAULT_DB, *,
                 client: MongoClient | None = None) -> None:
        self.db = _database(uri, db, client)
        wc, rc = WriteConcern("majority"), ReadConcern("majority")
        self.events = self.db.get_collection("events", write_concern=wc, read_concern=rc)
        self.counters = self.db.get_collection("counters", write_concern=wc, read_concern=rc)
        self.cursors = self.db.get_collection("cursors", write_concern=wc, read_concern=rc)
        self.events.create_index([("key", ASCENDING)], unique=True, name="key_unique")
        self.events.create_index([("seq", ASCENDING)], unique=True, name="seq_unique")
        try:  # counters doc must exist before the first transaction (upserts inside txns can race)
            self.counters.insert_one({"_id": self.COUNTER_ID, "seq": 0})
        except DuplicateKeyError:
            pass

    def append(self, type: str, key: str, payload: dict[str, Any]) -> int:
        existing = self.events.find_one({"key": key}, {"seq": 1})
        if existing:
            return int(existing["seq"])
        body = _normalise(payload)

        def txn(session) -> int:
            found = self.events.find_one({"key": key}, {"seq": 1}, session=session)
            if found:
                return int(found["seq"])
            c = self.counters.find_one_and_update({"_id": self.COUNTER_ID}, {"$inc": {"seq": 1}},
                                                  return_document=ReturnDocument.AFTER, session=session)
            seq = int(c["seq"])
            self.events.insert_one({"_id": seq, "seq": seq, "type": type, "key": key, "payload": body,
                                    "ts": time.time()}, session=session)
            return seq

        try:
            with self.db.client.start_session() as s:
                return s.with_transaction(txn, read_concern=ReadConcern("snapshot"),
                                          write_concern=WriteConcern("majority"))
        except DuplicateKeyError:
            found = self.events.find_one({"key": key}, {"seq": 1})
            if found:
                return int(found["seq"])
            raise

    def read(self, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        cur = self.events.find({"seq": {"$gt": int(after_seq)}}).sort("seq", ASCENDING)
        if limit > 0:
            cur = cur.limit(limit)
        return [_event(d) for d in cur]

    def get_by_key(self, key: str) -> Event | None:
        d = self.events.find_one({"key": key})
        return _event(d) if d else None

    def get_cursor(self, consumer: str) -> int:
        d = self.cursors.find_one({"_id": consumer})
        return int(d["seq"]) if d else 0

    def set_cursor(self, consumer: str, seq: int) -> None:
        self.cursors.update_one({"_id": consumer}, {"$set": {"seq": int(seq)}}, upsert=True)

    def close(self) -> None:
        pass


# ------------------------------------------------------------------------------------ change stream


class EditsChangeFeed:
    """Change-stream subscription on `edits` (or any collection) with persisted resume tokens.

    Delivery is at-least-once: a change's resume token is saved only after its handler returned, so a
    crash between handler and save redelivers that change. Handlers should be idempotent (e.g. key
    their side effects by edit id, or use EventLog.append with a derived key).

    The stream is opened in the constructor; for a consumer with no saved token the current position
    is saved immediately, so changes made after construction are never lost across restarts.
    """

    TOKENS = "stream_tokens"

    def __init__(self, consumer: str, uri: str | None = None, db: str | Database = DEFAULT_DB, *,
                 client: MongoClient | None = None, collection: str = "edits",
                 operation_types: tuple[str, ...] = ("insert", "update", "replace")) -> None:
        self.db = _database(uri, db, client)
        self.consumer, self.collection = consumer, collection
        self.tokens = self.db.get_collection(self.TOKENS, write_concern=WriteConcern("majority"))
        self._token_id = f"{consumer}:{collection}"
        saved = self.tokens.find_one({"_id": self._token_id})
        pipeline = [{"$match": {"operationType": {"$in": list(operation_types)}}}]
        self._stream = self.db[collection].watch(pipeline, full_document="updateLookup",
                                                 resume_after=saved["token"] if saved else None,
                                                 max_await_time_ms=200)
        if not saved and self._stream.resume_token is not None:
            self._save(self._stream.resume_token)

    def _save(self, token: Any) -> None:
        self.tokens.update_one({"_id": self._token_id}, {"$set": {"token": token, "ts": time.time()}}, upsert=True)

    @staticmethod
    def _shape(ch: dict[str, Any]) -> dict[str, Any]:
        full = ch.get("fullDocument")
        return {"op": ch["operationType"], "id": ch["documentKey"]["_id"],
                "doc": _strip(dict(full)) if full is not None else None}

    def poll(self, handler: Callable[[dict[str, Any]], Any] | None = None, max_events: int = 100,
             max_wait_s: float = 2.0) -> list[dict[str, Any]]:
        """Deliver up to max_events changes (waiting at most max_wait_s for the first/next ones)."""
        out: list[dict[str, Any]] = []
        deadline = time.monotonic() + max_wait_s
        while len(out) < max_events:
            ch = self._stream.try_next()
            if ch is None:
                if time.monotonic() >= deadline:
                    break
                continue
            item = self._shape(ch)
            if handler is not None:
                handler(item)
            self._save(ch["_id"])
            out.append(item)
        return out

    def close(self) -> None:
        self._stream.close()


# ------------------------------------------------------------------------------------- vector index


class AtlasVectorIndex:
    """contracts.VectorIndex over Atlas Vector Search on `rejected_edits.embedding`.

    add() writes `embedding`, `_vec_ns` and `_vec_meta` onto the document `_id = doc_id` (the rejected
    edit itself, when the orchestrator has already stored it; otherwise a hidden placeholder that
    MongoStore.get/find ignore until the real document is put). One namespace per doc_id.

    Search is read-your-writes: after add(), search() polls until the search index has caught up with
    the number of vectors in the namespace (mongot indexes asynchronously), up to `sync_timeout_s`.
    """

    COLLECTION = "rejected_edits"

    def __init__(self, store: MongoStore | None = None, namespace: str = "default", *, uri: str | None = None,
                 db: str | Database = DEFAULT_DB, client: MongoClient | None = None,
                 index_name: str = "rejected_edits_embedding", exact: bool = True, num_candidates: int = 200,
                 ready_timeout_s: float = 180.0, sync_timeout_s: float = 60.0) -> None:
        self.db = store.db if store is not None else _database(uri, db, client)
        self.namespace, self.index_name = namespace, index_name
        self.exact, self.num_candidates = exact, num_candidates
        self.sync_timeout_s = sync_timeout_s
        self.coll = self.db[self.COLLECTION]
        self._dirty = True  # other processes may have written; verify once on first search
        self.ensure_search_index(ready_timeout_s)

    # ---- index management
    def _definition(self) -> dict[str, Any]:
        return {"fields": [{"type": "vector", "path": "embedding", "numDimensions": DIM, "similarity": "cosine"},
                           {"type": "filter", "path": "_vec_ns"}]}

    def _index_info(self) -> dict[str, Any] | None:
        for ix in self.coll.list_search_indexes(self.index_name):
            return ix
        return None

    def ensure_search_index(self, timeout_s: float = 180.0) -> None:
        if self.COLLECTION not in self.db.list_collection_names():
            self.db.create_collection(self.COLLECTION)
        if self._index_info() is None:
            try:
                self.coll.create_search_indexes([SearchIndexModel(definition=self._definition(),
                                                                  name=self.index_name, type="vectorSearch")])
            except OperationFailure as e:  # created concurrently by another process
                if "already exists" not in str(e) and getattr(e, "code", None) != 68:
                    raise
        deadline = time.monotonic() + timeout_s
        while True:
            ix = self._index_info()
            if ix and ix.get("queryable") and ix.get("status") in (None, "READY"):
                return
            if ix and ix.get("status") == "FAILED":
                raise RuntimeError(f"vector search index {self.index_name} failed to build: {ix}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"vector search index {self.index_name} not queryable after {timeout_s}s")
            time.sleep(0.5)

    # ---- port
    def __len__(self) -> int:
        return self.coll.count_documents({"_vec_ns": self.namespace, "embedding": {"$exists": True}})

    def add(self, doc_id: str, vector: list[float], meta: dict[str, Any] | None = None) -> None:
        vec = [float(x) for x in vector]
        if len(vec) != DIM:
            raise ValueError(f"vector has {len(vec)} dims, index expects {DIM}")
        self.coll.update_one({"_id": doc_id},
                             {"$set": {"embedding": vec, "_vec_ns": self.namespace,
                                       "_vec_meta": _normalise(dict(meta or {}))},
                              "$setOnInsert": {"_vec_orphan": True, "_ord": ObjectId()}}, upsert=True)
        self._dirty = True

    def _vector_search(self, vector: list[float], k: int, exact: bool | None = None) -> list[dict[str, Any]]:
        stage: dict[str, Any] = {"index": self.index_name, "path": "embedding", "queryVector": vector,
                                 "limit": int(k), "filter": {"_vec_ns": self.namespace}}
        if self.exact if exact is None else exact:
            stage["exact"] = True
        else:
            stage["numCandidates"] = min(10_000, max(self.num_candidates, int(k) * 10))
        return list(self.coll.aggregate([
            {"$vectorSearch": stage},
            {"$project": {"_id": 1, "_vec_meta": 1, "score": {"$meta": "vectorSearchScore"}}}]))

    def _wait_in_sync(self) -> None:
        """Poll until an exact search over the namespace returns every stored vector."""
        deadline = time.monotonic() + self.sync_timeout_s
        probe = [1.0] + [0.0] * (DIM - 1)
        while True:
            n = len(self)
            if n == 0 or len(self._vector_search(probe, n, exact=True)) >= n:
                self._dirty = False
                return
            if time.monotonic() > deadline:
                raise TimeoutError("vector search index did not catch up with recent writes")
            time.sleep(0.2)

    def search(self, vector: list[float], k: int = 5) -> list[tuple[str, float, dict[str, Any]]]:
        if k <= 0:
            return []
        if self._dirty:
            self._wait_in_sync()
        q = [float(x) for x in vector]
        if not any(q):
            q = [1e-12] * DIM  # a zero query is undefined under cosine; keep the call valid
        out = []
        for d in self._vector_search(q, k):
            cos = 2.0 * float(d["score"]) - 1.0  # Atlas cosine score is (1 + cos) / 2
            out.append((d["_id"], cos, d.get("_vec_meta") or {}))
        return out
