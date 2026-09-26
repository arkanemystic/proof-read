"""Add integrator-facing keys to results/final/mongo_live.json (written by scripts/mongo_live.py).

Adds, without touching existing keys:
- "transitions": change-stream arrival order, [{ts, edit_id, arm, old, new, resume_token_persisted, op,
  consumer_instance, duplicate}]. `old` is the previous status observed for that edit through the stream
  ("none" for the first). EditsChangeFeed saves the resume token (write concern majority) right after each
  handler returns, so every delivered row is persisted, except the last row when the consumer stopped with an error.
- "edits": [{edit_id, status, reason}] final state read from Atlas by mongo_live.py (edits_final).
No URI or host is read or written here; this only rewrites the JSON file.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "results/final/mongo_live.json"


def from_atlas() -> dict:
    """Fallback when mongo_live.py did not write its file: rebuild the timeline from the EventLog rows the
    change-stream consumer appended (edit.status_observed) plus the final edits and spend."""
    import os
    import sqlite3
    import sys
    from datetime import datetime, timezone

    sys.path.insert(0, str(OUT.parents[2]))
    from proofread.store import factory
    from proofread.store.mongo_store import get_client

    db = get_client(factory.mongodb_uri())[factory.mongodb_db()]
    run = "IMP_C_atlas_r2"
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    rows = [{"observed_at": iso(e["ts"]), "op": e["payload"]["op"], "edit_id": e["payload"]["edit_id"],
             "status": e["payload"]["status"], "eventlog_seq": e["seq"], "duplicate": False}
            for e in db["events"].find({"type": "edit.status_observed", "key": {"$regex": f"^feed:{run}:"}}).sort("seq", 1)]
    edits = [{"edit_id": e["_id"], "status": e.get("status"), "delta_points": e.get("delta_points"),
              "reason": (e.get("reason") or "")[:300]} for e in db["edits"].find({"run_id": run})]
    tok = db["stream_tokens"].find_one({"_id": "mongo_live_timeline:edits"}) or {}
    spend = None
    try:
        c = sqlite3.connect(f"file:{OUT.parents[2] / os.environ.get('PROOFREAD_SPEND_DB', 'data/final_spend.sqlite')}?mode=ro", uri=True)
        spend = c.execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM calls WHERE budget_key='mongo_live'").fetchone()
    except Exception:
        pass
    return {"section": "10c live loop on Atlas", "run_id": run, "database": db.name,
            "store_backend": "mongo (Atlas)", "source": "rebuilt by scripts/mongo_live_post.py --from-atlas from "
            "the EventLog rows appended by the change-stream consumer (mongo_live.py had not written its file)",
            "edits_final": edits, "spend_usd": round(float(spend[0]), 4) if spend else None,
            "model_calls": spend[1] if spend else None,
            "episodes_in_atlas": db["episodes"].count_documents({"_id": {"$regex": f"^{run}:"}}),
            "change_stream": {"consumer": "mongo_live_timeline", "collection": "edits", "delivery": "at-least-once",
                              "timeline": rows, "token_persisted_at": iso(tok["ts"]) if tok.get("ts") else None,
                              "consumer_error": None}}


def main() -> int:
    import sys

    if "--from-atlas" in sys.argv:
        OUT.write_text(json.dumps(from_atlas(), indent=1, default=str) + "\n")
    d = json.loads(OUT.read_text())
    cs = d.get("change_stream") or {}
    rows = cs.get("timeline") or []
    err = cs.get("consumer_error")
    last: dict[str, str] = {}
    trans = []
    for i, r in enumerate(rows):
        eid, new = r["edit_id"], r["status"]
        trans.append({"ts": r["observed_at"], "edit_id": eid, "arm": "C", "old": last.get(eid, "none"), "new": new,
                      "resume_token_persisted": not (err and i == len(rows) - 1), "op": r.get("op"),
                      "consumer_instance": r.get("consumer_instance"), "duplicate": r.get("duplicate", False),
                      "eventlog_seq": r.get("eventlog_seq")})
        last[eid] = new
    d["transitions"] = trans
    d["edits"] = [{"edit_id": e["edit_id"], "status": e.get("status"), "reason": e.get("reason") or ""}
                  for e in d.get("edits_final") or []]
    d.setdefault("spend_usd", None)
    OUT.write_text(json.dumps(d, indent=1, default=str) + "\n")
    print(json.dumps({"transitions": len(trans), "edits": d["edits"], "spend_usd": d["spend_usd"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
