"""MongoDB Atlas backend (stub). Same ports as the SQLite backend; not wired yet.

Mapping from the SQLite layout:
- Store: one Mongo collection per logical collection (harness_versions, edits, episodes, actions,
  rejected_edits, events, vectors); `_id` = doc_id; documents stored as-is.
- EventLog: collection "events" with a unique index on `key` and a monotonic `seq` taken from a
  counters document via find_one_and_update($inc) (or use the change stream resume token as cursor).
- VectorIndex: Atlas Vector Search index on vectors.vector (cosine, dims = proofread.store.vector.DIM).
"""

from __future__ import annotations

from typing import Any

from proofread.contracts import Event


class MongoStore:
    def __init__(self, uri: str, db: str = "proofread") -> None:
        # TODO: pymongo.MongoClient(uri)[db]; create indexes (edits.status, episodes.arm+generation).
        raise NotImplementedError("MongoStore is a stub; use SqliteStore")

    def put(self, collection: str, doc_id: str, doc: dict[str, Any]) -> None:
        # TODO: replace_one({"_id": doc_id}, {**doc, "_id": doc_id}, upsert=True)
        raise NotImplementedError

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def find(self, collection: str, where: dict[str, Any] | None = None, limit: int = 0) -> list[dict[str, Any]]:
        raise NotImplementedError


class MongoEventLog:
    def __init__(self, uri: str, db: str = "proofread") -> None:
        # TODO: unique index on events.key; counters collection for seq.
        # TODO: expose watch() over a change stream (operationType insert) so consumers (analysis,
        #       dashboards) react to promotions without polling; persist resume tokens as cursors.
        raise NotImplementedError("MongoEventLog is a stub; use SqliteEventLog")

    def append(self, type: str, key: str, payload: dict[str, Any]) -> int:
        raise NotImplementedError

    def read(self, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        raise NotImplementedError

    def get_cursor(self, consumer: str) -> int:
        raise NotImplementedError

    def set_cursor(self, consumer: str, seq: int) -> None:
        raise NotImplementedError


class AtlasVectorIndex:
    def __init__(self, uri: str, db: str = "proofread", index_name: str = "intent_vectors") -> None:
        # TODO: Atlas Vector Search: $vectorSearch {index, path: "vector", queryVector, numCandidates,
        #       limit, filter: {namespace}}; project score via {"$meta": "vectorSearchScore"}.
        raise NotImplementedError("AtlasVectorIndex is a stub; use NumpyVectorIndex")

    def add(self, doc_id: str, vector: list[float], meta: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def search(self, vector: list[float], k: int = 5) -> list[tuple[str, float, dict[str, Any]]]:
        raise NotImplementedError
