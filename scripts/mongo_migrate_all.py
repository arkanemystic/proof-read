"""Section 10c: migrate every run store into Atlas proofread_runs, tagged by source store and run label.

    uv run python scripts/mongo_migrate_all.py [--only imp3] [--out results/final/mongo_migration.json]

Why not plain scripts/migrate_sqlite_to_mongo.py: that script copies one store into one database with the
original ids and event seqs. Six stores in one database collide (baseline episode ids repeat across stores,
every store's event seq starts at 1, and the live Atlas loop appends to the same `events` counter). So this
wrapper reuses its read-only SQLite reader but writes namespaced documents:

- docs (episodes, edits, harness_versions, rejected_edits): `_id = "<store>::<orig id>"`, all original fields,
  plus `orig_id`, `source_store` (e.g. "imp3"), `run_label` (doc run_id, else "<store>:<arm>"), `migrated`.
  The doc's own `_id` field (if any) is kept as `orig_id`.
- vectors: written onto the matching rejected_edits doc as `embedding`, `_vec_ns = "mig:<store>:<ns>"`,
  `_vec_meta` (the Atlas Vector Search index `rejected_edits_embedding` covers them; the live loop's namespaces
  are "rejected:<run_id>" so it never retrieves migrated vectors). Also kept in `vectors` with a prefixed id.
- events: collection `migrated_events`, `_id = "<store>::<seq>"` (the live EventLog keeps its own seq space).
- cursors: collection `migrated_cursors`, `_id = "<store>::<consumer>"`.
Idempotent (replace_one upsert by _id). SQLite opened read-only. Counts verified per collection by
count_documents({source_store}) after the write. Never prints the URI.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pymongo import ReplaceOne  # noqa: E402

from proofread.store import factory  # noqa: E402
from proofread.store.mongo_store import get_client  # noqa: E402
from scripts.migrate_sqlite_to_mongo import _ro, _tables  # noqa: E402

STORES = ["rerun", "proofread", "imp", "imp2", "smoke", "imp3"]  # imp3 last (still growing)
OUT = ROOT / "results/final/mongo_migration.json"
DOC_COLLS = ("episodes", "edits", "harness_versions", "rejected_edits", "vectors")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def migrate_store(db, name: str) -> dict:
    t0 = time.time()
    path = ROOT / "data" / f"{name}.sqlite"
    conn = _ro(path)
    tables = _tables(conn)
    ops: dict[str, list] = {}
    sqlite_counts: dict[str, int] = {}
    vecs = []
    rows = list(conn.execute("SELECT collection, id, doc FROM docs ORDER BY rowid")) if "docs" in tables else []
    for coll, doc_id, blob in rows:
        doc = json.loads(blob)
        sqlite_counts[coll] = sqlite_counts.get(coll, 0) + 1
        if coll == "vectors":
            vecs.append(doc)
        doc.pop("_id", None)
        label = doc.get("run_id") or f"{name}:{doc.get('arm') or doc.get('namespace') or 'none'}"
        new = {**doc, "_id": f"{name}::{doc_id}", "orig_id": doc_id, "source_store": name, "run_label": label,
               "migrated": True}
        ops.setdefault(coll, []).append(new)
    # embed vectors on their rejected_edits docs
    rej = {d["orig_id"]: d for d in ops.get("rejected_edits", [])}
    for v in vecs:
        d = rej.get(v["doc_id"])
        if d is not None:
            d["embedding"] = [float(x) for x in v["vector"]]
            d["_vec_ns"] = f"mig:{name}:{v.get('namespace', 'default')}"
            d["_vec_meta"] = v.get("meta") or {}
    if "events" in tables:
        evs = list(conn.execute("SELECT seq, type, key, payload, ts FROM events ORDER BY seq"))
        sqlite_counts["events"] = len(evs)
        ops["migrated_events"] = [{"_id": f"{name}::{s}", "seq": int(s), "type": t, "key": k,
                                   "payload": json.loads(p), "ts": float(ts), "source_store": name,
                                   "run_label": (json.loads(p) or {}).get("run_id") or f"{name}:events",
                                   "migrated": True} for s, t, k, p, ts in evs]
    if "cursors" in tables:
        cur = list(conn.execute("SELECT consumer, seq FROM cursors"))
        sqlite_counts["cursors"] = len(cur)
        ops["migrated_cursors"] = [{"_id": f"{name}::{c}", "consumer": c, "seq": int(s), "source_store": name,
                                    "migrated": True} for c, s in cur]
    conn.close()
    for coll, docs in ops.items():
        for i in range(0, len(docs), 500):
            db[coll].bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs[i:i + 500]],
                                ordered=False)
    mongo_counts = {}
    for coll in sqlite_counts:
        target = {"events": "migrated_events", "cursors": "migrated_cursors"}.get(coll, coll)
        mongo_counts[coll] = db[target].count_documents({"source_store": name})
    embedded = db["rejected_edits"].count_documents({"source_store": name, "embedding": {"$exists": True}})
    return {"sqlite": str(path.relative_to(ROOT)), "sqlite_counts": sqlite_counts, "mongo_counts": mongo_counts,
            "equal": sqlite_counts == mongo_counts, "rejected_edits_with_embedding": embedded,
            "vectors_in_sqlite": len(vecs), "seconds": round(time.time() - t0, 2), "finished_at": _iso(time.time())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="comma list of store names (default: all six)")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    uri, dbn = factory.mongodb_uri(), factory.mongodb_db()
    assert uri and dbn == "proofread_runs", "needs MONGODB_URI and MONGODB_DB=proofread_runs"
    db = get_client(uri)[dbn]
    names = a.only.split(",") if a.only else STORES
    out_p = Path(a.out)
    rep = json.loads(out_p.read_text()) if out_p.exists() else {"database": dbn, "stores": {}, "passes": []}
    t0 = time.time()
    for n in names:
        r = migrate_store(db, n)
        prev = rep["stores"].get(n)
        if prev:
            r["previous_pass"] = {k: prev.get(k) for k in ("sqlite_counts", "finished_at", "seconds")}
        rep["stores"][n] = r
        print(n, json.dumps({k: r[k] for k in ("sqlite_counts", "equal", "seconds")}), flush=True)
    rep["passes"].append({"stores": names, "started_at": _iso(t0), "seconds": round(time.time() - t0, 2),
                          "all_equal": all(rep["stores"][n]["equal"] for n in names)})
    rep["all_equal"] = all(s["equal"] for s in rep["stores"].values())
    rep["method"] = ("scripts/mongo_migrate_all.py: read-only SQLite, _id '<store>::<id>', tags source_store and "
                     "run_label, events to migrated_events, idempotent upserts; counts are count_documents per "
                     "source_store after the write")
    out_p.write_text(json.dumps(rep, indent=1) + "\n")
    return 0 if rep["all_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
