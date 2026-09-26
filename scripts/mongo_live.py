"""Section 10c live loop on MongoDB Atlas: IMP-C settings, Atlas as the only store, change-stream timeline.

    source results/final/env_mongo_live.sh
    uv run python scripts/mongo_live.py --deadline 2026-09-26T18:10:00Z

- Store, EventLog and VectorIndex come from proofread.store.factory, which selects Mongo because .env has
  MONGODB_URI (MONGODB_DB=proofread_runs). PROOFREAD_STORE_BACKEND=sqlite is refused.
- Arm C (gated), 1 generation, 2 candidates, first 6 imp_training tasks in lock order, seed 0,
  cost rule on, champion_IMP_C genome, run id IMP_C_atlas_r2.
- Spend: every model call is charged to budget key "mongo_live" (ArmConfig.budget_key is overridden in
  this process only); the cap comes from PROOFREAD_BUDGET_CAPS (1 USD) and is enforced by the ledger.
- Sandboxes: at most 3 concurrent episodes in this process, and a new sandbox waits while 12 or more
  pf-* containers already run on the box (global cap shared with the replication runs).
- A change-stream consumer (EditsChangeFeed, consumer "mongo_live_timeline") runs in a thread, records
  every edit status transition of this run, appends a derived EventLog entry per transition (idempotent
  key), and is restarted once from its persisted resume token to demonstrate resumption.
- Output: results/final/mongo_live.json. No URI or host is ever written.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "results/final/mongo_live.json"
RUN_ID = "IMP_C_atlas_r2"  # r1 (IMP_C_atlas) died after writing only its champion version; see notes/W-ATLAS.md
CONSUMER = "mongo_live_timeline"
MAX_LOCAL = 3
GLOBAL_CAP = 12


def _iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(t if t is not None else time.time(), timezone.utc).isoformat(timespec="seconds")


def _tok_digest(tok: Any) -> str | None:
    """Short digest of a resume token (the token itself is opaque; the digest shows it changed)."""
    if tok is None:
        return None
    data = tok.get("_data") if isinstance(tok, dict) else str(tok)
    return hashlib.sha1(str(data).encode()).hexdigest()[:12]


def _pf_count() -> int:
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=20)
        return sum(1 for n in out.stdout.split() if n.startswith("pf-"))
    except Exception:
        return GLOBAL_CAP  # fail closed: treat as full


class Timeline:
    def __init__(self, uri: str, db: str) -> None:
        self.uri, self.db = uri, db
        self.rows: list[dict[str, Any]] = []
        self.restarts: list[dict[str, Any]] = []
        self.seen: set[tuple[str, str]] = set()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.error: str | None = None

    def _saved_token(self, raw_db) -> Any:
        d = raw_db["stream_tokens"].find_one({"_id": f"{CONSUMER}:edits"})
        return d.get("token") if d else None

    def run(self) -> None:
        from proofread.store.mongo_store import EditsChangeFeed, MongoEventLog, get_client

        raw_db = get_client(self.uri)[self.db]
        log = MongoEventLog(self.uri, self.db)
        feed = EditsChangeFeed(CONSUMER, self.uri, self.db)
        restarted = False

        def handle(item: dict[str, Any]) -> None:
            doc = item.get("doc") or {}
            if doc.get("run_id") != RUN_ID:
                return
            status = str(doc.get("status"))
            row = {"observed_at": _iso(), "op": item["op"], "edit_id": item["id"], "status": status,
                   "generation": doc.get("generation"), "delta_points": doc.get("delta_points"),
                   "reason": (doc.get("reason") or doc.get("gate_reason") or "")[:300],
                   "consumer_instance": 2 if restarted else 1}
            key = f"feed:{RUN_ID}:{item['id']}:{status}"
            seq = log.append("edit.status_observed", key, {k: row[k] for k in ("edit_id", "status", "op")})
            row["eventlog_seq"] = seq
            with self.lock:
                row["duplicate"] = (item["id"], status) in self.seen
                self.seen.add((item["id"], status))
                self.rows.append(row)

        try:
            while not self.stop.is_set():
                feed.poll(handle, max_events=50, max_wait_s=2.0)
                with self.lock:
                    n = len([r for r in self.rows if not r["duplicate"]])
                if not restarted and n >= 1:
                    before = _tok_digest(self._saved_token(raw_db))
                    feed.close()
                    t0 = time.time()
                    time.sleep(3.0)  # changes made while the consumer is down must be delivered after resume
                    feed = EditsChangeFeed(CONSUMER, self.uri, self.db)
                    restarted = True
                    self.restarts.append({"closed_at": _iso(t0), "reopened_at": _iso(),
                                          "persisted_token_digest_at_close": before,
                                          "rows_before_restart": n})
            # drain after the run ended
            feed.poll(handle, max_events=200, max_wait_s=5.0)
            self.final_token = _tok_digest(self._saved_token(raw_db))
            feed.close()
        except Exception as e:  # record type only (messages could carry the host)
            self.error = type(e).__name__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deadline", default="2026-09-26T18:10:00Z")
    ap.add_argument("--tasks", type=int, default=6)
    ap.add_argument("--candidates", type=int, default=2)
    ap.add_argument("--generations", type=int, default=1)
    a = ap.parse_args(argv)

    if (os.environ.get("PROOFREAD_STORE_BACKEND") or "").lower() == "sqlite":
        print("refusing: PROOFREAD_STORE_BACKEND=sqlite would not use Atlas", file=sys.stderr)
        return 2
    caps = json.loads(os.environ.get("PROOFREAD_BUDGET_CAPS") or "{}")
    if float(caps.get("mongo_live", 0)) <= 0 or float(caps["mongo_live"]) > 1.0:
        print("refusing: PROOFREAD_BUDGET_CAPS must contain mongo_live <= 1", file=sys.stderr)
        return 2

    from proofread.evolve import config as ecfg
    from proofread.evolve import orchestrator as orch
    from proofread.sandbox import runner as sbx
    from proofread.store import factory

    if factory.backend() != "mongo":
        print("refusing: store backend is not mongo", file=sys.stderr)
        return 2
    uri, db = factory.mongodb_uri(), factory.mongodb_db()
    if db != "proofread_runs":
        print("refusing: MONGODB_DB must be proofread_runs", file=sys.stderr)
        return 2

    ecfg.ArmConfig.budget_key = property(lambda self: "mongo_live")  # this process only

    orig_slot = sbx.sandbox_slot

    @contextlib.asynccontextmanager
    async def slot():
        while _pf_count() >= GLOBAL_CAP:
            await asyncio.sleep(5)
        async with orig_slot():
            yield

    sbx.sandbox_slot = slot

    tl = Timeline(uri, db)
    th = threading.Thread(target=tl.run, daemon=True)
    th.start()
    time.sleep(2.0)

    args = argparse.Namespace(arm="C", db="data/unused_mongo_live.sqlite", generations=a.generations,
                              candidates=a.candidates, model=os.environ["IMP_AGENT"],
                              proposer_model=os.environ["IMP_PROPOSER"], tasks=a.tasks, seeds="0",
                              split="imp_training", concurrency=MAX_LOCAL, run_id=RUN_ID,
                              trace_dir="traces/mongo_live", deadline=a.deadline,
                              genome=str(ROOT / "results/final/champion_IMP_C.json"), cost_rule=True)

    async def go() -> dict[str, Any]:
        sbx.SANDBOX_SEMAPHORE = asyncio.Semaphore(MAX_LOCAL)  # bound to this loop
        return await orch._amain(args)

    started = time.time()
    result: dict[str, Any] | None = None
    err = None
    try:
        result = asyncio.run(go())
    except Exception as e:
        err = f"{type(e).__name__}"
    ended = time.time()
    time.sleep(4.0)
    tl.stop.set()
    th.join(timeout=30)

    from proofread.store.mongo_store import get_client

    raw = get_client(uri)[db]
    edits = list(raw["edits"].find({"run_id": RUN_ID}, {"_id": 1, "status": 1, "reason": 1, "delta_points": 1,
                                                       "generation": 1, "intent": 1}))
    n_eps = raw["episodes"].count_documents({"arm": {"$regex": "^IMP_C_atlas_r2"}})
    n_eps_any = raw["episodes"].count_documents({"budget_key": "mongo_live"})
    spend = None
    try:
        import sqlite3

        c = sqlite3.connect(f"file:{ROOT / os.environ['PROOFREAD_SPEND_DB']}?mode=ro", uri=True)
        spend = c.execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM calls WHERE budget_key='mongo_live'").fetchone()
    except Exception:
        pass
    out = {
        "section": "10c live loop on Atlas",
        "run_id": RUN_ID, "database": db, "store_backend": "mongo (Atlas)",
        "settings": {"arm": "C (gated)", "generations": a.generations, "candidates": a.candidates,
                     "tasks": a.tasks, "split": "imp_training (first 6 in lock order)", "seeds": [0],
                     "agent_model": args.model, "proposer_model": args.proposer_model,
                     "genome": "results/final/champion_IMP_C.json", "cost_rule": True,
                     "max_concurrent_episodes": MAX_LOCAL, "budget_key": "mongo_live",
                     "budget_cap_usd": float(caps["mongo_live"]), "deadline": a.deadline},
        "started_at": _iso(started), "ended_at": _iso(ended), "wall_s": round(ended - started, 1),
        "orchestrator_error": err, "orchestrator_result": result,
        "edits_final": [{"edit_id": e["_id"], "status": e.get("status"), "delta_points": e.get("delta_points"),
                         "reason": (e.get("reason") or "")[:300]} for e in edits],
        "episodes_written_budget_key_mongo_live": n_eps_any,
        "spend_usd": round(float(spend[0]), 4) if spend else None, "model_calls": spend[1] if spend else None,
        "change_stream": {"consumer": CONSUMER, "collection": "edits", "delivery": "at-least-once",
                          "timeline": tl.rows, "restarts": tl.restarts,
                          "final_persisted_token_digest": getattr(tl, "final_token", None),
                          "consumer_error": tl.error,
                          "note": "token digests are sha1 prefixes of the opaque resume token _data, stored in "
                                  "proofread_runs.stream_tokens; rows with consumer_instance 2 were delivered "
                                  "after the consumer was closed and reopened from its persisted token"},
    }
    _ = n_eps
    OUT.write_text(json.dumps(out, indent=1, default=str) + "\n")
    print(json.dumps({"edits": out["edits_final"], "timeline_rows": len(tl.rows), "spend": out["spend_usd"],
                      "error": err}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
