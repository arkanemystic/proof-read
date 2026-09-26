"""Copy a SQLite store (docs, events, cursors) into MongoDB. Idempotent: re-running is a no-op.

Documents keep their ids and insertion order. Events keep their seq (Mongo `_id` = seq) and the Mongo
seq counter is raised to the highest migrated seq, so new appends continue after them. An event whose
seq or key is already taken in Mongo by a different event is skipped and counted as a conflict.
Afterwards the Atlas Vector Search index on the vectors collection is created if missing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from pymongo.errors import BulkWriteError

from proofread.store.mongo_store import AtlasVectorIndex, MongoEventLog, MongoStore


def migrate_sqlite_to_mongo(sqlite_path: str | Path, store: MongoStore, log: MongoEventLog, *,
                            create_vector_index: bool = True,
                            progress: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    p = Path(sqlite_path)
    if not p.exists():
        raise FileNotFoundError(p)
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out: dict[str, Any] = {"docs": {}, "events": {"inserted": 0, "already": 0, "conflicts": 0}, "cursors": 0}

    if "docs" in tables:
        groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for coll, doc_id, blob in con.execute("SELECT collection, id, doc FROM docs ORDER BY rowid"):
            groups.setdefault(coll, []).append((doc_id, json.loads(blob)))
        for coll, items in groups.items():
            out["docs"][coll] = store.put_many(coll, items)
            progress(f"docs {coll}: {len(items)}")

    if "events" in tables:
        rows = con.execute("SELECT seq, type, key, payload, ts FROM events ORDER BY seq").fetchall()
        for i in range(0, len(rows), 1000):
            chunk = rows[i:i + 1000]
            existing = {d["_id"]: d["key"] for d in log.events.find({"_id": {"$in": [r[0] for r in chunk]}},
                                                                  {"key": 1})}
            new = []
            for seq, type_, key, payload, ts in chunk:
                if seq in existing:
                    out["events"]["already" if existing[seq] == key else "conflicts"] += 1
                else:
                    new.append({"_id": int(seq), "type": type_, "key": key, "payload": json.loads(payload), "ts": ts})
            if new:
                try:
                    out["events"]["inserted"] += len(log.events.insert_many(new, ordered=False).inserted_ids)
                except BulkWriteError as e:  # key already used by a different Mongo event
                    out["events"]["inserted"] += e.details.get("nInserted", 0)
                    out["events"]["conflicts"] += len(e.details.get("writeErrors", []))
        if rows:
            log.counters.update_one({"_id": log._counter_id}, {"$max": {"seq": int(rows[-1][0])}}, upsert=True)
        progress(f"events: {out['events']}")

    if "cursors" in tables:
        for consumer, seq in con.execute("SELECT consumer, seq FROM cursors"):
            log.cursors.update_one({"_id": consumer}, {"$max": {"seq": int(seq)}}, upsert=True)
            out["cursors"] += 1
    con.close()

    if create_vector_index:
        idx = AtlasVectorIndex(store, namespace="_migrate")
        out["vector_index"] = idx.search_index_status()
    return out
