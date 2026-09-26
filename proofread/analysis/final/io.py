"""Read-only data access. Never writes to any database; every loader returns empty data on failure."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def connect_ro(path: str | Path) -> sqlite3.Connection | None:
    """Read-only connection (URI mode=ro). Falls back to immutable=1 (ignores the WAL) as a last resort."""
    p = Path(path)
    if not p.exists():
        return None
    for q in ("mode=ro", "mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(f"file:{p.resolve()}?{q}", uri=True, timeout=30.0)
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return con
        except sqlite3.Error:
            continue
    return None


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@dataclass
class StoreData:
    """Documents by collection plus the event log, from one SqliteStore/SqliteEventLog file."""

    path: str = ""
    docs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def coll(self, name: str) -> list[dict[str, Any]]:
        return self.docs.get(name, [])

    @property
    def empty(self) -> bool:
        return not any(self.docs.values()) and not self.events


def load_store(path: str | Path | None) -> StoreData:
    out = StoreData(path=str(path or ""))
    if not path:
        out.error = "no path"
        return out
    con = connect_ro(path)
    if con is None:
        out.error = f"missing or unreadable: {path}"
        return out
    try:
        tabs = _tables(con)
        if "docs" in tabs:
            for coll, blob in con.execute("SELECT collection, doc FROM docs ORDER BY rowid"):
                try:
                    d = json.loads(blob)
                except (TypeError, ValueError):
                    continue
                if isinstance(d, dict):
                    out.docs.setdefault(coll, []).append(d)
        if "events" in tabs:
            for seq, typ, key, payload, ts in con.execute("SELECT seq, type, key, payload, ts FROM events ORDER BY seq"):
                try:
                    pl = json.loads(payload)
                except (TypeError, ValueError):
                    pl = {}
                out.events.append({"seq": seq, "type": typ, "key": key, "payload": pl, "ts": ts})
    except sqlite3.Error as e:
        out.error = f"sqlite error: {e}"
    finally:
        con.close()
    return out


def load_spend(path: str | Path | None) -> list[dict[str, Any]]:
    """Rows of the SpendLedger `calls` table (proofread/models/ledger.py). Missing -> []."""
    if not path:
        return []
    con = connect_ro(path)
    if con is None:
        return []
    try:
        if "calls" not in _tables(con):
            return []
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM calls")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def load_json(path: str | Path | None) -> Any:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_trace(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except OSError:
        return []
    return out


def load_golden() -> list[dict[str, Any]]:
    """The golden cheat scenarios (proofread/sandbox/scenarios.py GOLDEN) as {name, expect, ops}."""
    try:
        from proofread.sandbox.scenarios import GOLDEN
    except Exception:  # noqa: BLE001
        return []
    out = []
    for s in GOLDEN:
        ops = [o[0] for o in getattr(s, "ops", []) if o]
        out.append({"name": s.name, "expect": s.expect, "ops": ops})
    return out
