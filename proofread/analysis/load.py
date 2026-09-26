"""Defensive data loaders. Everything downstream works on plain lists of dicts.

- Store collections come from proofread.store.sqlite_store.SqliteStore (imported lazily). If that
  import fails, a generic SQLite reader tries common layouts so analysis never hard-depends on W5.
- Spend rows come from W4's SpendLedger database; its schema is read by introspection.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

COLLECTIONS_USED = ("episodes", "edits", "rejected_edits", "harness_versions")

_ROLE_KEYS = ("role", "budget_role")
_BUCKET_KEYS = ("budget_key", "key", "bucket", "workload", "cap_key")
_COST_KEYS = ("cost_usd", "usd", "amount_usd", "amount", "cost", "spent_usd")


def _connect(db: Path) -> sqlite3.Connection:
    """Read-only connection; falls back to a normal one (WAL databases can refuse mode=ro without -shm)."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con
    except sqlite3.Error:
        return sqlite3.connect(str(db), timeout=30.0)


def _generic_find(db: Path, collection: str) -> list[dict[str, Any]]:
    """Best-effort reader for unknown document-store layouts."""
    out: list[dict[str, Any]] = []
    con = _connect(db)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            json_col = next((c for c in ("doc", "data", "json", "body", "value") if c in cols), None)
            if json_col is None:
                continue
            if "collection" in cols:
                rows = con.execute(f'SELECT "{json_col}" FROM "{t}" WHERE collection=?', (collection,))
            elif t == collection:
                rows = con.execute(f'SELECT "{json_col}" FROM "{t}"')
            else:
                continue
            for (blob,) in rows:
                try:
                    d = json.loads(blob)
                except (TypeError, ValueError):
                    continue
                if isinstance(d, dict):
                    out.append(d)
    finally:
        con.close()
    return out


def load_store(db_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Return {collection: [docs]} for the collections analysis uses. Missing db -> empty lists."""
    db = Path(db_path)
    data: dict[str, list[dict[str, Any]]] = {c: [] for c in COLLECTIONS_USED}
    if not db.exists():
        return data
    # Read-only generic reader first (handles W5's docs(collection, id, doc) layout without opening a
    # writable connection); fall back to SqliteStore for collections the generic reader cannot see.
    for c in COLLECTIONS_USED:
        try:
            data[c] = _generic_find(db, c)
        except Exception:  # noqa: BLE001
            data[c] = []
    missing = [c for c in COLLECTIONS_USED if not data[c]]
    if missing:
        try:
            from proofread.store.sqlite_store import SqliteStore  # type: ignore

            store = SqliteStore(str(db))
            for c in missing:
                try:
                    data[c] = list(store.find(c))
                except Exception:  # noqa: BLE001
                    data[c] = []
        except Exception:  # noqa: BLE001 - W5 module may be absent or differ
            pass
    return data


def load_spend(path: str | Path | None) -> list[dict[str, Any]]:
    """All rows of every table in the spend db as dicts (each tagged with _table). Absent -> []."""
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        con = _connect(p)
        con.row_factory = sqlite3.Row
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tables:
                if t.startswith("sqlite_"):
                    continue
                for r in con.execute(f'SELECT * FROM "{t}"'):
                    d = dict(r)
                    d["_table"] = t
                    rows.append(d)
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return rows


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def role_of_bucket(bucket: str) -> str:
    b = (bucket or "").lower()
    if b.startswith("arm_") or b in ("agent", "smoke"):
        return "agent"
    if b.startswith("baseline"):
        return "baseline"
    if b.startswith("selection"):
        return "selection"
    if b.startswith("propos"):
        return "proposer"
    return b or "unknown"


def spend_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """{"by_role": {...}, "by_bucket": {...}} in USD. Rows without a cost field are ignored.

    Tables that look like cap/config tables (no cost column) contribute nothing.
    """
    by_role: dict[str, float] = {}
    by_bucket: dict[str, float] = {}
    for r in rows:
        cost = _first(r, _COST_KEYS)
        try:
            c = float(cost)
        except (TypeError, ValueError):
            continue
        bucket = str(_first(r, _BUCKET_KEYS) or "")
        role = str(_first(r, _ROLE_KEYS) or role_of_bucket(bucket))
        by_role[role] = by_role.get(role, 0.0) + c
        by_bucket[bucket or "(none)"] = by_bucket.get(bucket or "(none)", 0.0) + c
    return {"by_role": by_role, "by_bucket": by_bucket}


def read_text(path: str | Path | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None
