"""Section 10c: load per-action records (trace `verify` records) of every migrated episode into Atlas `actions`.

    uv run python scripts/mongo_actions.py

One document per checked Action: `_id = "<store>::<episode_id>:<verify_index>:<i>"`, episode_id, step, kind, path,
dst, host, cmd (first 300 chars), n_added_lines, n_removed_lines, failed (policy ids that fired for this action),
stage, round, ts, plus source_store, run_label, arm, task_id from the episode. Line contents are not copied (they stay
in the trace files; the collection is the queryable index of effects). Idempotent (upsert by _id). Traces that are
missing on disk are counted, not fatal. Writes counts to results/final/mongo_actions.json. Never prints the URI.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pymongo import ReplaceOne  # noqa: E402

from scripts.mongo_numbers import get_db  # noqa: E402

OUT = ROOT / "results/final/mongo_actions.json"


def main() -> int:
    db = get_db()
    t0 = time.time()
    rep: dict = {"per_store": {}, "missing_traces": 0}
    for ep in db.episodes.find({"migrated": True}, {"source_store": 1, "run_label": 1, "arm": 1, "task_id": 1,
                                                     "episode_id": 1, "trace_path": 1, "n_actions": 1}):
        src = ep["source_store"]
        st = rep["per_store"].setdefault(src, {"episodes": 0, "episodes_with_trace": 0, "actions": 0,
                                               "n_actions_field_sum": 0})
        st["episodes"] += 1
        st["n_actions_field_sum"] += int(ep.get("n_actions") or 0)
        p = Path(str(ep.get("trace_path") or ""))
        if not p.is_file():
            rep["missing_traces"] += 1
            continue
        st["episodes_with_trace"] += 1
        ops = []
        vi = 0
        for line in p.open():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") != "verify":
                continue
            failed = r.get("failed") or []
            for i, a in enumerate(r.get("actions") or []):
                ops.append(ReplaceOne({"_id": f"{src}::{ep.get('episode_id')}:{vi}:{i}"}, {
                    "_id": f"{src}::{ep.get('episode_id')}:{vi}:{i}", "episode_id": ep.get("episode_id"),
                    "step": a.get("step"), "kind": a.get("kind"), "path": a.get("path"), "dst": a.get("dst"),
                    "host": a.get("host"), "cmd": str(a.get("cmd") or "")[:300],
                    "n_added_lines": len(a.get("added_lines") or []), "n_removed_lines": len(a.get("removed_lines") or []),
                    "attributed": a.get("attributed"), "failed": failed[i] if i < len(failed) else [],
                    "stage": r.get("stage"), "round": r.get("round"), "ts": r.get("ts"),
                    "source_store": src, "run_label": ep.get("run_label"), "arm": ep.get("arm"),
                    "task_id": ep.get("task_id"), "migrated": True}, upsert=True))
            vi += 1
        if ops:
            db.actions.bulk_write(ops, ordered=False)
        st["actions"] += len(ops)
    for src, st in rep["per_store"].items():
        st["mongo_count"] = db.actions.count_documents({"source_store": src})
    rep["total_actions"] = db.actions.count_documents({"migrated": True})
    rep["seconds"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(rep, indent=1) + "\n")
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
