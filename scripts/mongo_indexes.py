"""Create the section 10c indexes on Atlas proofread_runs (idempotent) and wait for the vector index.

    uv run python scripts/mongo_indexes.py

B-tree: actions {episode_id: 1}; episodes {arm: 1, task_id: 1}; edits {status: 1, arm: 1};
harness_versions {arm: 1, version: 1}. Vector: one Atlas Vector Search index `rejected_edits_embedding`
on rejected_edits.embedding (numDimensions = proofread.store.vector.DIM, cosine, filter _vec_ns),
reused if it exists. Prints a JSON report (no URI).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proofread.store import factory  # noqa: E402
from proofread.store.mongo_store import AtlasVectorIndex, MongoStore  # noqa: E402

BTREE = {"actions": [("episode_id", 1)], "episodes": [("arm", 1), ("task_id", 1)],
         "edits": [("status", 1), ("arm", 1)], "harness_versions": [("arm", 1), ("version", 1)]}


def main() -> int:
    uri, db = factory.mongodb_uri(), factory.mongodb_db()
    assert uri and db == "proofread_runs", "needs MONGODB_URI and MONGODB_DB=proofread_runs"
    store = MongoStore(uri, db)
    rep: dict = {"database": db, "btree": {}, "vector": {}}
    for coll, keys in BTREE.items():
        if coll not in store.db.list_collection_names():
            store.db.create_collection(coll)
        name = store.db[coll].create_index(keys)
        rep["btree"][coll] = {"name": name, "keys": dict(keys)}
    t0 = time.time()
    idx = AtlasVectorIndex(store, namespace="default", ready_timeout_s=600)
    info = idx._index_info() or {}
    rep["vector"] = {"collection": "rejected_edits", "name": info.get("name"), "type": info.get("type"),
                     "status": info.get("status"), "queryable": info.get("queryable"),
                     "definition": info.get("latestDefinition"), "wait_s": round(time.time() - t0, 1)}
    rep["search_indexes_on_db"] = sum(len(list(store.db[c].list_search_indexes()))
                                      for c in store.db.list_collection_names() if not c.startswith("system."))
    print(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
