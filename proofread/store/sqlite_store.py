"""SQLite backends for the Store and EventLog ports.

Documents are stored as JSON blobs keyed by (collection, id), which maps one-to-one onto MongoDB
collections of documents with `_id = id`. WAL mode plus a generous busy_timeout make the same file
safe to share between processes (e.g. arm A and arm C running concurrently).

The event log is append-only with a monotonic AUTOINCREMENT seq (never reused, even after deletes)
and a UNIQUE idempotency key: appending an existing key is a no-op returning the original seq.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from proofread.contracts import Event

BUSY_TIMEOUT_MS = 60_000


def _connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class _Tx:
    """BEGIN IMMEDIATE transaction (takes the write lock up front, so no upgrade deadlocks)."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self.conn, self.lock = conn, lock

    def __enter__(self) -> sqlite3.Connection:
        self.lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.lock.release()


def _dumps(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class SqliteStore:
    """contracts.Store over SQLite. `find` is equality on top-level fields (same as InMemoryStore)."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = _connect(path)
        self._lock = threading.RLock()
        with _Tx(self._conn, self._lock) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS docs (collection TEXT NOT NULL, id TEXT NOT NULL, doc TEXT NOT NULL, "
                "updated_at REAL NOT NULL, PRIMARY KEY (collection, id))"
            )

    def put(self, collection: str, doc_id: str, doc: dict[str, Any]) -> None:
        blob = _dumps(doc)
        with _Tx(self._conn, self._lock) as c:
            c.execute(
                "INSERT INTO docs(collection, id, doc, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(collection, id) DO UPDATE SET doc=excluded.doc, updated_at=excluded.updated_at",
                (collection, doc_id, blob, time.time()),
            )

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT doc FROM docs WHERE collection=? AND id=?", (collection, doc_id)).fetchone()
        return json.loads(row[0]) if row else None

    def find(self, collection: str, where: dict[str, Any] | None = None, limit: int = 0) -> list[dict[str, Any]]:
        where = where or {}
        sql, args = "SELECT doc FROM docs WHERE collection=?", [collection]
        for k, v in where.items():
            # SQL prefilter for scalar values; exact Python equality check below is authoritative.
            if isinstance(v, str) or (isinstance(v, (int, float)) and not isinstance(v, bool)):
                sql += " AND json_extract(doc, ?) = ?"
                args += [f'$."{k}"', v]
        sql += " ORDER BY rowid"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for (blob,) in rows:
            d = json.loads(blob)
            if all(d.get(k) == v for k, v in where.items()):
                out.append(d)
                if limit and len(out) >= limit:
                    break
        return out

    def delete(self, collection: str, doc_id: str) -> None:
        with _Tx(self._conn, self._lock) as c:
            c.execute("DELETE FROM docs WHERE collection=? AND id=?", (collection, doc_id))

    def close(self) -> None:
        self._conn.close()


class SqliteEventLog:
    """contracts.EventLog over SQLite: append-only, monotonic seq, idempotency keys, named cursors."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = _connect(path)
        self._lock = threading.RLock()
        with _Tx(self._conn, self._lock) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, "
                "key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, ts REAL NOT NULL)"
            )
            c.execute("CREATE TABLE IF NOT EXISTS cursors (consumer TEXT PRIMARY KEY, seq INTEGER NOT NULL)")
            c.execute(
                "CREATE TRIGGER IF NOT EXISTS events_append_only_u BEFORE UPDATE ON events "
                "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
            )
            c.execute(
                "CREATE TRIGGER IF NOT EXISTS events_append_only_d BEFORE DELETE ON events "
                "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
            )

    def append(self, type: str, key: str, payload: dict[str, Any]) -> int:
        blob = _dumps(payload)
        with _Tx(self._conn, self._lock) as c:
            row = c.execute("SELECT seq FROM events WHERE key=?", (key,)).fetchone()
            if row:
                return int(row[0])
            cur = c.execute("INSERT INTO events(type, key, payload, ts) VALUES (?,?,?,?)", (type, key, blob, time.time()))
            return int(cur.lastrowid)

    def read(self, after_seq: int = 0, limit: int = 1000) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, type, key, payload, ts FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                (after_seq, limit if limit > 0 else -1),
            ).fetchall()
        return [Event(seq=r[0], type=r[1], key=r[2], payload=json.loads(r[3]), ts=r[4]) for r in rows]

    def get_by_key(self, key: str) -> Event | None:
        with self._lock:
            r = self._conn.execute("SELECT seq, type, key, payload, ts FROM events WHERE key=?", (key,)).fetchone()
        return Event(seq=r[0], type=r[1], key=r[2], payload=json.loads(r[3]), ts=r[4]) if r else None

    def get_cursor(self, consumer: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT seq FROM cursors WHERE consumer=?", (consumer,)).fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, consumer: str, seq: int) -> None:
        with _Tx(self._conn, self._lock) as c:
            c.execute(
                "INSERT INTO cursors(consumer, seq) VALUES (?,?) ON CONFLICT(consumer) DO UPDATE SET seq=excluded.seq",
                (consumer, int(seq)),
            )

    def close(self) -> None:
        self._conn.close()
