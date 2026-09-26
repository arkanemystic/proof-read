"""Section 10c: headline numbers as MongoDB aggregation pipelines over the migrated runs, cross-checked
against results/final/numbers.json, plus the $vectorSearch demo over rejected edits.

    uv run python scripts/mongo_numbers.py            # writes results/final/mongo_numbers.json
    uv run python scripts/mongo_numbers.py --vector-demo "intent text"   # writes results/final/mongo_vector_demo.json

Importable (W-DEMO):
    from scripts.mongo_numbers import get_db, summary, similar_rejected
    db = get_db(); summary(db) -> dict; similar_rejected(db, "intent", k=3) -> list[dict]

All grouping, counting, summing and percentiles run server-side ($match/$group/$unwind/$percentile); Python
only formats the returned rows and compares them with numbers.json. Conventions mirror
proofread/analysis/final/compute.py: errored = error set and not starting with "aborted_cost"; natural-cheat
valid = not aborted_cost and not infra (BudgetExceeded / episode error); holdout capability arms use the
pristine grader, *_cheat arms the workspace grader. Never prints the URI.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "results/final/mongo_numbers.json"
VEC_OUT = ROOT / "results/final/mongo_vector_demo.json"
NUMBERS = ROOT / "results/final/numbers.json"
VECTOR_INDEX = "rejected_edits_embedding"

ERRORED = {"$and": [{"$gt": [{"$strLenCP": {"$ifNull": ["$error", ""]}}, 0]},
                    {"$ne": [{"$indexOfCP": [{"$ifNull": ["$error", ""]}, "aborted_cost"]}, 0]}]}
ABORTED = {"$eq": [{"$indexOfCP": [{"$ifNull": ["$error", ""]}, "aborted_cost"]}, 0]}
INFRA = {"$or": [{"$eq": [{"$indexOfCP": [{"$ifNull": ["$error", ""]}, "BudgetExceeded"]}, 0]},
                 {"$eq": [{"$indexOfCP": [{"$ifNull": ["$error", ""]}, "episode error"]}, 0]}]}
PRISTINE = {"$cond": [{"$eq": ["$passed_pristine", True]}, 1, 0]}
WORKSPACE = {"$cond": [{"$eq": ["$passed_workspace", True]}, 1, 0]}
COST = {"$ifNull": ["$cost_usd", 0]}
NATURAL = ["R1obs", "R1obs_high", "R1enf", "R1b_obs", "R1b_enf", "R1b_conf_obs"]


def get_db():
    from proofread.store import factory
    from proofread.store.mongo_store import get_client

    uri, dbn = factory.mongodb_uri(), factory.mongodb_db()
    if not uri:
        raise RuntimeError("MONGODB_URI not set (env or .env)")
    return get_client(uri)[dbn]


# ------------------------------------------------------------------ pipelines
def pass_rate_per_run(db, source_store: str | None = None) -> list[dict[str, Any]]:
    """One row per (source_store, arm): episodes, pristine and workspace passes, errored, cost, cost per solved."""
    match: dict[str, Any] = {"migrated": True}
    if source_store:
        match["source_store"] = source_store
    return list(db.episodes.aggregate([
        {"$match": match},
        {"$group": {"_id": {"store": "$source_store", "arm": "$arm"}, "n": {"$sum": 1},
                    "pristine_pass": {"$sum": PRISTINE}, "workspace_pass": {"$sum": WORKSPACE},
                    "errored": {"$sum": {"$cond": [ERRORED, 1, 0]}}, "cost_usd": {"$sum": COST},
                    "denied": {"$sum": {"$ifNull": ["$n_denied", 0]}},
                    "violating": {"$sum": {"$cond": [{"$gt": [{"$size": {"$ifNull": ["$violations", []]}}, 0]}, 1, 0]}}}},
        {"$addFields": {"cheat_arm": {"$regexMatch": {"input": "$_id.arm", "regex": "_cheat$"}}}},
        {"$addFields": {"solved": {"$cond": ["$cheat_arm", "$workspace_pass", "$pristine_pass"]}}},
        {"$addFields": {"rate": {"$cond": [{"$gt": ["$n", 0]}, {"$divide": ["$solved", "$n"]}, None]},
                        "cost_per_solved_usd": {"$cond": [{"$gt": ["$solved", 0]},
                                                          {"$divide": ["$cost_usd", "$solved"]}, None]},
                        "cost_per_episode_usd": {"$divide": ["$cost_usd", "$n"]}}},
        {"$sort": {"_id.store": 1, "_id.arm": 1}}]))


def pooled_replication(db) -> list[dict[str, Any]]:
    """Pooled seeds 0..2 per harness over valid (non-errored) episodes, from imp3."""
    return list(db.episodes.aggregate([
        {"$match": {"source_store": "imp3", "arm": {"$regex": r"^IMPH_(default|C)(_s\d+)?$"}}},
        {"$match": {"$expr": {"$not": [ERRORED]}}},
        {"$group": {"_id": {"$arrayElemAt": [{"$split": ["$arm", "_"]}, 1]}, "n": {"$sum": 1},
                    "k": {"$sum": PRISTINE}, "cost_usd": {"$sum": COST}}},
        {"$addFields": {"rate": {"$divide": ["$k", "$n"]},
                        "cost_per_solved_usd": {"$cond": [{"$gt": ["$k", 0]}, {"$divide": ["$cost_usd", "$k"]}, None]}}},
        {"$sort": {"_id": 1}}]))


def cheat_passes_by_mechanism(db) -> list[dict[str, Any]]:
    """Workspace passes on impossible tasks by mechanism: natural-cheating runs (valid episodes), red-team runs,
    and the IMPH_*_cheat holdouts."""
    return list(db.episodes.aggregate([
        {"$match": {"migrated": True, "passed_workspace": True, "$or": [
            {"source_store": "rerun", "arm": {"$in": NATURAL}}, {"source_store": "rerun", "arm": {"$regex": "^RT"}},
            {"source_store": "imp3", "arm": {"$regex": "_cheat$"}}]}},
        {"$match": {"$expr": {"$not": [{"$or": [ABORTED, INFRA]}]}}},
        {"$group": {"_id": {"store": "$source_store", "arm": "$arm", "mechanism": "$mechanism"}, "n": {"$sum": 1}}},
        {"$sort": {"_id.store": 1, "_id.arm": 1, "_id.mechanism": 1}}]))


def natural_cheating(db) -> list[dict[str, Any]]:
    """Per natural-cheating run and pooled: valid episodes and cheat passes (any workspace pass on an impossible task)."""
    return list(db.episodes.aggregate([
        {"$match": {"source_store": "rerun", "arm": {"$in": NATURAL}}},
        {"$match": {"$expr": {"$not": [{"$or": [ABORTED, INFRA]}]}}},
        {"$facet": {
            "per_run": [{"$group": {"_id": "$arm", "valid": {"$sum": 1}, "cheat": {"$sum": WORKSPACE}}},
                        {"$sort": {"_id": 1}}],
            "pooled": [{"$group": {"_id": "pooled", "valid": {"$sum": 1}, "cheat": {"$sum": WORKSPACE}}}]}}]))[0]


def gate_ledger(db) -> list[dict[str, Any]]:
    """Edits by run and outcome (all migrated runs)."""
    return list(db.edits.aggregate([
        {"$match": {"migrated": True}},
        {"$group": {"_id": {"store": "$source_store", "run_id": "$run_id", "status": "$status"}, "n": {"$sum": 1}}},
        {"$group": {"_id": {"store": "$_id.store", "run_id": "$_id.run_id"},
                    "outcomes": {"$push": {"k": "$_id.status", "v": "$n"}}, "total": {"$sum": "$n"}}},
        {"$project": {"_id": 1, "total": 1, "outcomes": {"$arrayToObject": "$outcomes"}}},
        {"$sort": {"_id.store": 1, "_id.run_id": 1}}]))


def verifier_latency(db, stores: tuple[str, ...] = ("rerun", "imp3")) -> dict[str, Any]:
    """Per-action verifier latency p50/p95/max over EpisodeResult.verifier_latency_ms (same sources as NUMBERS)."""
    base = [{"$match": {"source_store": {"$in": list(stores)}}},
            {"$unwind": "$verifier_latency_ms"},
            {"$match": {"verifier_latency_ms": {"$type": "number"}}}]
    out: dict[str, Any] = {}
    for method in ("discrete", "approximate"):
        try:
            r = list(db.episodes.aggregate(base + [{"$group": {
                "_id": None, "n": {"$sum": 1}, "max": {"$max": "$verifier_latency_ms"},
                "pct": {"$percentile": {"input": "$verifier_latency_ms", "p": [0.5, 0.95], "method": method}}}}]))
            if r:
                out = {"n_actions": r[0]["n"], "p50_ms": r[0]["pct"][0], "p95_ms": r[0]["pct"][1],
                       "max_ms": r[0]["max"], "percentile_method": method}
            break
        except Exception as e:  # older servers only support "approximate"
            out = {"error": type(e).__name__}
    return out


def spend_live(db) -> list[dict[str, Any]]:
    """Episodes and cost of the live Atlas loop (not migrated; written directly by the orchestrator)."""
    return list(db.episodes.aggregate([
        {"$match": {"migrated": {"$ne": True}}},
        {"$group": {"_id": "$run_id", "n": {"$sum": 1}, "cost_usd": {"$sum": COST},
                    "workspace_pass": {"$sum": WORKSPACE}}}]))


def summary(db) -> dict[str, Any]:
    """All section 10c aggregates (plain dicts, JSON-serialisable)."""
    return {"pass_rate_per_run": pass_rate_per_run(db), "pooled_replication": pooled_replication(db),
            "cheat_passes_by_mechanism": cheat_passes_by_mechanism(db), "natural_cheating": natural_cheating(db),
            "gate_ledger": gate_ledger(db), "verifier_latency": verifier_latency(db), "live_runs": spend_live(db)}


# ------------------------------------------------------------------ vector search
def similar_rejected(db, intent_text: str, k: int = 3, exact: bool = True) -> list[dict[str, Any]]:
    """Top-k rejected edits most similar to `intent_text` via Atlas $vectorSearch on rejected_edits.embedding.
    The query vector is proofread.store.vector.embed(intent_text), the same embedding the proposer uses."""
    from proofread.store.vector import embed

    stage: dict[str, Any] = {"index": VECTOR_INDEX, "path": "embedding", "queryVector": embed(intent_text),
                             "limit": int(k)}
    if exact:
        stage["exact"] = True
    else:
        stage["numCandidates"] = max(100, 10 * int(k))
    rows = db.rejected_edits.aggregate([
        {"$vectorSearch": stage},
        {"$project": {"_id": 0, "edit_id": {"$ifNull": ["$orig_id", "$id"]}, "run_id": 1, "source_store": 1,
                      "status": 1, "intent": 1, "reason": 1, "gate_reason": 1, "delta_points": 1, "generation": 1,
                      "violations": {"$slice": [{"$ifNull": ["$violations", []]}, 3]},
                      "score": {"$meta": "vectorSearchScore"}}}])
    out = []
    for r in rows:
        r["cosine"] = round(2.0 * float(r.pop("score")) - 1.0, 4)  # Atlas cosine score is (1 + cos) / 2
        r["rejection_reason"] = r.pop("reason", None) or r.pop("gate_reason", None)
        r.pop("gate_reason", None)
        out.append(r)
    return out


# ------------------------------------------------------------------ cross-check
_FRAC = re.compile(r"(\d+)/(\d+)")
_USD = re.compile(r"([\d.]+) USD")


def _frac(s: str) -> tuple[int, int] | None:
    m = _FRAC.search(s)
    return (int(m.group(1)), int(m.group(2))) if m else None


def cross_check(s: dict[str, Any], numbers: dict[str, Any]) -> list[dict[str, Any]]:
    ref = {r["id"]: r["value"] for r in numbers["numbers"]}
    rows = []

    def add(nid: str, mongo: Any, ref_val: Any, ok: bool, note: str = "") -> None:
        rows.append({"id": nid, "mongo": mongo, "numbers_json": ref_val, "match": bool(ok), "note": note})

    runs = {(r["_id"]["store"], r["_id"]["arm"]): r for r in s["pass_rate_per_run"]}
    for arm in ("IMPH_C", "IMPH_default", "IMPH_C_cheat", "IMPH_default_cheat"):
        r = runs.get(("imp3", arm))
        nid = f"holdout.{arm}.rate"
        if r and nid in ref:
            add(nid, f"{r['solved']}/{r['n']}", ref[nid], _frac(ref[nid]) == (r["solved"], r["n"]))
        nid = f"holdout.{arm}.cost_per_solved"
        if r and nid in ref:
            m = _USD.findall(ref[nid])
            mv = f"{r['cost_per_solved_usd']:.4f} USD ({r['cost_per_episode_usd']:.4f} USD per episode)"
            add(nid, mv, ref[nid], m[:2] == [f"{r['cost_per_solved_usd']:.4f}", f"{r['cost_per_episode_usd']:.4f}"])
        nid = f"holdout.{arm}.denied"
        if r and nid in ref:
            add(nid, r["denied"], ref[nid], str(r["denied"]) == ref[nid].strip())
    for h in ("default", "C"):
        for sd in (1, 2):
            r, nid = runs.get(("imp3", f"IMPH_{h}_s{sd}")), f"replication.{h}.s{sd}.rate"
            if r and nid in ref:
                mv = f"{r['pristine_pass']}/{r['n']}; {r['cost_per_solved_usd']:.4f} USD per solved task; {r['errored']} errored"
                ok = (_frac(ref[nid]) == (r["pristine_pass"], r["n"]) and f"{r['cost_per_solved_usd']:.4f} USD" in ref[nid]
                      and f"{r['errored']} errored" in ref[nid])
                add(nid, mv, ref[nid], ok, "" if ok else "imp3 still growing (replication running) after NUMBERS build")
    for p in s["pooled_replication"]:
        nid = f"replication.{p['_id']}.pooled"
        if nid in ref:
            mv = f"{p['k']}/{p['n']}; {p['cost_per_solved_usd']:.4f} USD per solved task"
            ok = _frac(ref[nid]) == (p["k"], p["n"]) and f"{p['cost_per_solved_usd']:.4f} USD" in ref[nid]
            add(nid, mv, ref[nid], ok, "" if ok else "imp3 still growing (replication running) after NUMBERS build")
    led = {r["_id"]["run_id"]: r["outcomes"] for r in s["gate_ledger"] if r["_id"]["store"] == "imp3"}
    for rid in ("IMP_C", "IMP_A"):
        nid = f"imp.{rid}.ledger"
        if nid in ref:
            o = led.get(rid, {})
            mv = ", ".join(f"{k} {o.get(k, 0)}" for k in ("invalid", "rejected_formal", "rejected_empirical", "promoted"))
            add(nid, mv, ref[nid], mv == ref[nid])
    nat = s["natural_cheating"]
    per = {r["_id"]: r for r in nat["per_run"]}
    for arm in NATURAL:
        nid = f"natural.{arm}"
        if nid in ref and arm in per:
            add(nid, f"{per[arm]['cheat']}/{per[arm]['valid']}", ref[nid],
                _frac(ref[nid]) == (per[arm]["cheat"], per[arm]["valid"]))
    if nat["pooled"] and "natural.pooled" in ref:
        p = nat["pooled"][0]
        add("natural.pooled", f"{p['cheat']}/{p['valid']}", ref["natural.pooled"],
            _frac(ref["natural.pooled"]) == (p["cheat"], p["valid"]))
    lat = s["verifier_latency"]
    if "latency.per_action" in ref and "p50_ms" in lat:
        mv = f"p50 {lat['p50_ms']:.2f} ms, p95 {lat['p95_ms']:.2f} ms, max {lat['max_ms']:.1f} ms"
        rn = next(r.get("n") for r in numbers["numbers"] if r["id"] == "latency.per_action")
        ok = mv == ref["latency.per_action"] and rn == lat["n_actions"]
        add("latency.per_action", f"{mv}; n {lat['n_actions']}", f"{ref['latency.per_action']}; n {rn}", ok,
            "" if ok else "n differs when imp3 grew after the NUMBERS build; percentile method " + lat["percentile_method"])
    return rows


def snapshot_check(s: dict[str, Any]) -> list[dict[str, Any]]:
    """Same-snapshot check: recompute the growing imp3 values with proofread/analysis/final/compute.py from the
    SQLite files now (numbers.json was built earlier, before the replication finished). Run right after
    re-migrating imp3 so both sides see the same rows."""
    import sqlite3

    import numpy as np

    from proofread.analysis.final import compute as C

    def eps(name: str) -> list[dict[str, Any]]:
        c = sqlite3.connect(f"file:{ROOT / 'data' / (name + '.sqlite')}?mode=ro", uri=True)
        return [json.loads(b) for (b,) in c.execute("SELECT doc FROM docs WHERE collection='episodes' ORDER BY rowid")]

    imp3, rerun = eps("imp3"), eps("rerun")
    rep = C.replication(imp3, n_boot=200)
    rows = []
    runs = {(r["_id"]["store"], r["_id"]["arm"]): r for r in s["pass_rate_per_run"]}
    for p in rep["per_seed"]:
        r = runs.get(("imp3", p["arm"]))
        mv = (r["pristine_pass"], r["n"], r["errored"], round(r["cost_usd"], 6)) if r else None
        pv = (p["k"], p["n"], p["errors"], round(p["total_usd"], 6))
        rows.append({"id": f"snapshot.replication.{p['harness']}.s{p['seed']}", "mongo": mv, "compute_py": pv,
                     "match": mv == pv})
    pooled = {p["_id"]: p for p in s["pooled_replication"]}
    for h, p in rep["pooled"].items():
        m = pooled.get(h)
        mv = (m["k"], m["n"], round(m["cost_usd"], 6)) if m else None
        pv = (p["k"], p["n"], round(p["total_usd"], 6))
        rows.append({"id": f"snapshot.replication.{h}.pooled", "mongo": mv, "compute_py": pv, "match": mv == pv})
    lat = C.latency({"rerun": rerun, "imp3": imp3})
    ml = s["verifier_latency"]
    vals = np.asarray(lat["values"])
    rows.append({"id": "snapshot.latency.per_action",
                 "mongo": {"n": ml.get("n_actions"), "p50": ml.get("p50_ms"), "p95": ml.get("p95_ms"),
                           "max": ml.get("max_ms"), "method": ml.get("percentile_method")},
                 "compute_py": {"n": lat["n_actions"], "p50": lat["p50"], "p95": lat["p95"], "max": lat["max"],
                                "method": "numpy inverted_cdf"},
                 "match": ml.get("n_actions") == lat["n_actions"] and ml.get("max_ms") == lat["max"]
                 and abs(ml.get("p50_ms", 0) - lat["p50"]) < 0.01,
                 "note": "Atlas $percentile supports only method 'approximate' (t-digest); p95 differs from numpy "
                         f"inverted_cdf by {abs(ml.get('p95_ms', 0) - lat['p95']):.3f} ms; nearest observed ranks: "
                         f"{float(np.percentile(vals, 94.9, method='inverted_cdf')):.3f} to "
                         f"{float(np.percentile(vals, 95.1, method='inverted_cdf')):.3f} ms"})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vector-demo", default=None, help="intent text for the $vectorSearch demo")
    ap.add_argument("--k", type=int, default=3)
    a = ap.parse_args(argv)
    db = get_db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if a.vector_demo:
        t0 = time.time()
        hits = similar_rejected(db, a.vector_demo, k=a.k)
        exact_ms = round(1000 * (time.time() - t0), 1)
        t0 = time.time()
        ann = similar_rejected(db, a.vector_demo, k=a.k, exact=False)
        out = {"generated_utc": now, "database": db.name, "collection": "rejected_edits", "index": VECTOR_INDEX,
               "embedding": "proofread.store.vector.embed (512-d hashed bag of words, L2-normalised, cosine), same "
                            "as the proposer's retrieval",
               "n_rejected_with_embedding": db.rejected_edits.count_documents({"embedding": {"$exists": True}}),
               "intent": a.vector_demo, "k": a.k, "exact_enn": hits, "exact_latency_ms": exact_ms,
               "ann_same_ids": [h["edit_id"] for h in ann] == [h["edit_id"] for h in hits],
               "ann_latency_ms": round(1000 * (time.time() - t0), 1)}
        VEC_OUT.write_text(json.dumps(out, indent=1, default=str) + "\n")
        print(json.dumps(out, indent=1, default=str))
        return 0
    t0 = time.time()
    s = summary(db)
    numbers = json.loads(NUMBERS.read_text())
    checks = cross_check(s, numbers)
    snap = snapshot_check(s)
    out = {"snapshot_checks": snap, "snapshot_matched": sum(c["match"] for c in snap),"generated_utc": now, "database": db.name, "aggregation_seconds": round(time.time() - t0, 2),
           "numbers_json_generated_utc": numbers["meta"].get("generated_utc"),
           "checked": len(checks), "matched": sum(c["match"] for c in checks),
           "mismatches": [c for c in checks if not c["match"]], "checks": checks,
           "not_cross_checked": ["imp.*.train_first/train_last (improvement curve needs event ordering)",
                                 "replication.delta_pooled (bootstrap)", "lap.* (lap_replay.json, not in stores)",
                                 "rt.* decisions (rt_gate.json)", "coverage.* (golden scenarios)",
                                 "imp.spend_total (data/final_spend.sqlite, not migrated)"],
           "summary": s}
    OUT.write_text(json.dumps(out, indent=1, default=str) + "\n")
    print(json.dumps({k: out[k] for k in ("checked", "matched", "mismatches", "snapshot_checks")}, indent=1,
                     default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
