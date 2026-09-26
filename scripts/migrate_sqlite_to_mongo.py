"""Copy a SQLite store (docs, event log, cursors, vector entries) into MongoDB. Idempotent.

    uv run python scripts/migrate_sqlite_to_mongo.py --sqlite data/proofread.sqlite \
        [--uri mongodb://...] [--db proofread] [--no-vector-index] [--dry-run]

--uri defaults to MONGODB_URI from the environment or .env (proofread.store.factory).

- The SQLite file is opened read-only (sqlite URI mode=ro); it is never modified.
- Every document of every collection is upserted with the same id via MongoStore.put, in SQLite rowid
  order, so Mongo `find` returns the same order. Re-running replaces documents in place.
- Events are upserted with their original seq (`_id = seq`) and key; the Mongo counter is raised to at
  least the highest migrated seq (never lowered), so new appends continue after them. An event whose
  key already exists in Mongo under a different seq, or whose seq is taken by a different key, is
  reported as a conflict and skipped (exit code 1); existing Mongo events are never overwritten.
- Cursors are copied as-is (upsert).
- Vector entries (collection "vectors": namespace, doc_id, vector, meta) are copied as documents and
  indexed by Atlas Vector Search (the same "vectors" collection) per namespace, unless
  --no-vector-index is given (use that for a plain mongod without mongot).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proofread.store.factory import mongodb_db, mongodb_uri  # noqa: E402
from proofread.store.mongo_store import AtlasVectorIndex, MongoEventLog, MongoStore  # noqa: E402

VECTORS = "vectors"


def _ro(path: str | Path) -> sqlite3.Connection:
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def migrate(sqlite_path: str | Path, uri: str, db: str = "proofread", *, vector_index: bool = True,
            dry_run: bool = False) -> dict[str, Any]:
    conn = _ro(sqlite_path)
    tables = _tables(conn)
    report: dict[str, Any] = {"docs": {}, "events": 0, "event_conflicts": [], "cursors": 0, "vectors": {}}
    store = None if dry_run else MongoStore(uri, db)
    log = None if dry_run else MongoEventLog(uri, db)

    vec_entries: list[dict[str, Any]] = []
    if "docs" in tables:
        for coll, doc_id, blob in conn.execute("SELECT collection, id, doc FROM docs ORDER BY rowid"):
            doc = json.loads(blob)
            report["docs"][coll] = report["docs"].get(coll, 0) + 1
            if store is not None:
                store.put(coll, doc_id, doc)
            if coll == VECTORS:
                vec_entries.append(doc)

    if "events" in tables:
        max_seq = 0
        for seq, typ, key, payload, ts in conn.execute("SELECT seq, type, key, payload, ts FROM events ORDER BY seq"):
            max_seq = max(max_seq, int(seq))
            if log is not None:
                other = log.events.find_one({"key": key, "_id": {"$ne": int(seq)}}, {"_id": 1})
                if other:
                    report["event_conflicts"].append({"key": key, "sqlite_seq": seq, "mongo_seq": other["_id"]})
                    continue
                taken = log.events.find_one({"_id": int(seq), "key": {"$ne": key}}, {"key": 1})
                if taken:  # seq already used by a different Mongo event: never overwrite it
                    report["event_conflicts"].append({"key": key, "sqlite_seq": seq, "mongo_key": taken["key"]})
                    continue
                log.events.replace_one({"_id": int(seq)}, {"_id": int(seq), "type": typ, "key": key,
                                                         "payload": json.loads(payload), "ts": float(ts)},
                                       upsert=True)
            report["events"] += 1
        if log is not None and max_seq:
            log.counters.update_one({"_id": log._counter_id}, {"$max": {"seq": max_seq}}, upsert=True)
        report["max_seq"] = max_seq

    if "cursors" in tables:
        for consumer, seq in conn.execute("SELECT consumer, seq FROM cursors"):
            if log is not None:
                log.set_cursor(consumer, int(seq))
            report["cursors"] += 1

    by_ns: dict[str, list[dict[str, Any]]] = {}
    for v in vec_entries:
        by_ns.setdefault(v.get("namespace", "default"), []).append(v)
    for ns, entries in by_ns.items():
        report["vectors"][ns] = len(entries)
        if vector_index and store is not None:
            idx = AtlasVectorIndex(store, namespace=ns)
            for v in entries:
                idx.add(v["doc_id"], v["vector"], v.get("meta") or {})
    conn.close()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sqlite", required=True, help="source SQLite store (opened read-only)")
    ap.add_argument("--uri", default=None, help="MongoDB URI (default: MONGODB_URI from env or .env)")
    ap.add_argument("--db", default=None, help="Mongo database (default: MONGODB_DB_NAME, MONGODB_DB or 'proofread')")
    ap.add_argument("--no-vector-index", action="store_true", help="skip Atlas Vector Search entries")
    ap.add_argument("--dry-run", action="store_true", help="only count what would be copied")
    a = ap.parse_args(argv)
    uri = a.uri or mongodb_uri()
    if not uri and not a.dry_run:
        print("no MongoDB URI: pass --uri or set MONGODB_URI", file=sys.stderr)
        return 2
    rep = migrate(a.sqlite, uri or "", a.db or mongodb_db(), vector_index=not a.no_vector_index, dry_run=a.dry_run)
    print(json.dumps(rep, indent=2, sort_keys=True))
    return 1 if rep["event_conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
