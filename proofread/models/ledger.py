"""SpendLedger: every model call is recorded in data/spend.sqlite (WAL, safe across processes).

Before each call `check(budget_key)` raises BudgetExceeded when the total cap (BUDGET_TOTAL_USD) or
the budget key's cap (BUDGET_CAPS_USD) has been reached. Keys not in BUDGET_CAPS_USD only count
toward the total.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from proofread.contracts import BUDGET_CAPS_USD, BUDGET_TOTAL_USD, BudgetExceeded

from .config import SPEND_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  role TEXT NOT NULL,
  model TEXT NOT NULL,
  budget_key TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  episode_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS calls_key ON calls(budget_key);
"""


class SpendLedger:
    def __init__(self, path: str | Path | None = None, total_cap: float = BUDGET_TOTAL_USD,
                 caps: dict[str, float] | None = None) -> None:
        self.path = Path(path or os.environ.get("PROOFREAD_SPEND_DB") or SPEND_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.total_cap = total_cap
        self.caps = dict(BUDGET_CAPS_USD if caps is None else caps)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            c.execute("PRAGMA busy_timeout=30000")
            yield c
        finally:
            c.close()

    def total(self) -> float:
        with self._conn() as c:
            return float(c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM calls").fetchone()[0])

    def spent(self, budget_key: str) -> float:
        with self._conn() as c:
            return float(c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE budget_key=?",
                                   (budget_key,)).fetchone()[0])

    def by_key(self) -> dict[str, float]:
        with self._conn() as c:
            return {k: float(v) for k, v in c.execute("SELECT budget_key, SUM(cost_usd) FROM calls GROUP BY budget_key")}

    def check(self, budget_key: str = "") -> None:
        tot = self.total()
        if tot >= self.total_cap:
            raise BudgetExceeded(f"total spend {tot:.4f} >= cap {self.total_cap:.2f}")
        cap = self.caps.get(budget_key)
        if cap is not None:
            s = self.spent(budget_key)
            if s >= cap:
                raise BudgetExceeded(f"budget {budget_key!r} spend {s:.4f} >= cap {cap:.2f}")

    def record(self, *, role: str, model: str, budget_key: str, input_tokens: int, output_tokens: int,
               cost_usd: float, episode_id: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO calls(ts, role, model, budget_key, input_tokens, output_tokens, cost_usd, episode_id)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (time.time(), role, model, budget_key or "", int(input_tokens), int(output_tokens),
                       float(cost_usd), episode_id))


_default: SpendLedger | None = None


def default_ledger() -> SpendLedger:
    global _default
    if _default is None:
        _default = SpendLedger()
    return _default
