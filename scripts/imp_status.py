"""Progress of the section 9 IMP runs (data/imp.sqlite, read-only use). usage: uv run python scripts/imp_status.py"""

import collections
import sqlite3

from proofread.store.sqlite_store import SqliteEventLog, SqliteStore

import sys

db = sys.argv[1] if len(sys.argv) > 1 else "data/imp3.sqlite"
s, log = SqliteStore(db), SqliteEventLog(db)
eps = s.find("episodes", {})
by = collections.defaultdict(list)
for e in eps:
    key = e.get("run_id") or e.get("arm")
    cand = e.get("candidate_id") or ""
    by[(key, e.get("arm"), e.get("generation"), cand)].append(e)
for k in sorted(by, key=str):
    v = by[k]
    n = len(v)
    pw = sum(bool(e.get("passed_workspace")) for e in v)
    pp = sum(bool(e.get("passed_pristine")) for e in v)
    ab = sum("aborted_cost" in (e.get("error") or "") for e in v)
    er = sum(bool(e.get("error")) for e in v) - ab
    vi = sum(bool(e.get("violations")) for e in v)
    c = sum(e.get("cost_usd") or 0 for e in v)
    print(f"{str(k):70s} n={n:3d} ws={pw:3d} pr={pp:3d} viol={vi} abort={ab} err={er} usd={c:.3f}")
for e in s.find("edits", {}):
    print(e["id"], e.get("status"), (e.get("reason") or e.get("gate_reason") or "")[:150])
    print("   intent:", (e.get("intent") or "")[:160])
c = sqlite3.connect("file:data/final_spend.sqlite?mode=ro", uri=True)
for r in c.execute("select budget_key, role, round(sum(cost_usd),3), count(*) from calls group by budget_key, role"):
    print("spend", r)
