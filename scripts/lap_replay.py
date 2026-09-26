"""Kernel-checked replay: re-verify every recorded action with the Lean-Agent Protocol lean-worker.

Reads (read-only, from a snapshot copy in /tmp) every episode of data/*.sqlite, follows each episode's
trace_path, takes every `verify` record (actions plus the stored per-action failed policies), sends the
actions through LapVerifier (typed facts from reference.py classification, decision by the Lean kernel
via `decide`), and writes results/final/lap_replay.json.

No experiment is run and no model API is called. Idempotent: output is fully rewritten each run.

    uv run python scripts/lap_replay.py                      # all dbs
    uv run python scripts/lap_replay.py --dbs data/rerun.sqlite data/imp3.sqlite
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import sqlite3
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from proofread.contracts import BASE_POLICY_IDS, FAILCLOSED_POLICY, Action  # noqa: E402
from proofread.policies import reference  # noqa: E402
from proofread.verify.facts import POLICY_PREDICATES, conjecture, extract_facts  # noqa: E402
import httpx  # noqa: E402

from proofread.verify.facts import LEAN_MODULE  # noqa: E402
from proofread.verify.lap_client import LAP_URL, LapVerifier, lap_available  # noqa: E402

RT_ARMS = ("RT_C", "RT_C_enf")
RT_DB = "rerun.sqlite"
SNAP_DIR = Path("/tmp/lap_replay_snapshot")


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot(db: Path) -> tuple[Path, str]:
    """Consistent copy via the SQLite online backup API (safe while a WAL writer is active)."""
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    dst = SNAP_DIR / db.name
    if dst.exists():
        dst.unlink()
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    out = sqlite3.connect(str(dst))
    t = time.time()
    src.backup(out)
    out.close()
    src.close()
    return dst, _utc(t)


def load_records(snap: Path) -> tuple[list[dict], int]:
    con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, doc FROM docs WHERE collection='episodes' ORDER BY id").fetchall()
    except sqlite3.DatabaseError:
        return [], 0
    finally:
        con.close()
    recs: list[dict] = []
    missing = 0
    for _id, doc in rows:
        ep = json.loads(doc)
        tp = Path(ep.get("trace_path") or "")
        if not tp.is_file():
            missing += 1
            continue
        for ln, line in enumerate(tp.read_text(encoding="utf-8").splitlines()):
            if '"verify"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # a line still being written by a live run
            if ev.get("type") != "verify":
                continue
            failed = ev.get("failed") or []
            for i, raw in enumerate(ev.get("actions") or []):
                recs.append({
                    "episode_id": ep.get("episode_id"), "arm": ep.get("arm"), "mode": ep.get("mode"),
                    "stage": ev.get("stage"), "round": ev.get("round"), "trace": str(tp), "trace_line": ln + 1,
                    "action": raw, "stored_failed": failed[i] if i < len(failed) else None,
                })
    return recs, missing


def bits_claim(a: Action, stored_base: list[str]) -> str:
    f = extract_facts(a).lean_literal()
    bits = ", ".join("false" if pid in stored_base else "true" for pid in BASE_POLICY_IDS)
    return f"example : verdictBits {f} = [{bits}] := by decide"


async def kernel_confirm(v: LapVerifier, items: list[tuple[int, Action, list[str]]], stats: dict) -> set[int]:
    """One kernel-checked `example` per recorded action, stating its stored per-policy verdict.

    A file is proved only if every example in it is proved, so a proved batch confirms each action
    individually. Any other result is bisected down to single actions. Returns ids NOT confirmed."""
    if not items:
        return set()
    head = f"import {LEAN_MODULE}\nopen PolicyEnv.Proofread\n"
    src = head + "\n".join(bits_claim(a, sb) for _, a, sb in items) + "\n"
    t0 = time.perf_counter()
    async with httpx.AsyncClient() as c:
        try:
            r = (await c.post(v.url + "/verify", json={"conjecture": src}, timeout=120)).json()
            res = r.get("result")
        except Exception:  # noqa: BLE001
            res = "error"
    stats["files"] += 1
    stats["file_ms"].append((time.perf_counter() - t0) * 1000.0)
    if res == "proved":
        return set()
    if len(items) == 1:
        return {items[0][0]}
    mid = len(items) // 2
    return (await kernel_confirm(v, items[:mid], stats)) | (await kernel_confirm(v, items[mid:], stats))


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return round(xs[k], 3)


async def replay(dbs: list[Path], out: Path, batch: int, concurrency: int, url: str,
                 per_action_kernel: bool = True) -> dict:
    if not lap_available(url):
        raise SystemExit(f"LAP lean-worker not reachable at {url}")
    v = LapVerifier(url=url, concurrency=concurrency, cache=True)
    t_start = time.time()
    per_db: dict[str, dict] = {}
    disagreements: list[dict] = []
    ref_disagreements = 0
    policy_lap: Counter = Counter()
    policy_stored: Counter = Counter()
    action_ms: list[float] = []
    decision_ms: list[float] = []
    kernel_us: list[float] = []
    batch_ms: list[float] = []
    stored_failclosed: list[dict] = []
    lap_failclosed = 0
    rt_blocked: list[dict] = []
    n_conj = 0
    uniq_facts: set = set()
    claim_meta: list[tuple[str, dict]] = []
    kstats: dict = {"files": 0, "file_ms": [], "claims": 0, "unconfirmed": []}

    for db in dbs:
        snap, snap_time = snapshot(db)
        recs, missing = load_records(snap)
        st = {"snapshot": str(snap), "snapshot_utc": snap_time, "records": len(recs), "missing_traces": missing,
              "invalid_actions": 0, "compared": 0, "agree": 0, "disagree": 0, "stored_failclosed": 0,
              "lap_failclosed": 0, "lap_violations": 0, "stored_violations": 0}
        valid: list[tuple[dict, Action]] = []
        for r in recs:
            try:
                valid.append((r, Action.model_validate(r["action"])))
            except Exception as e:  # noqa: BLE001
                st["invalid_actions"] += 1
                disagreements.append({"db": db.name, **r, "reason": f"invalid Action: {e}"[:300]})
        for i in range(0, len(valid), batch):
            chunk = valid[i : i + batch]
            t0 = time.perf_counter()
            verdicts = await v.check_batch([a for _, a in chunk])
            batch_ms.append((time.perf_counter() - t0) * 1000.0)
            claims: list[tuple[int, Action, list[str]]] = []
            for (r, a), vd, dec in zip(chunk, verdicts, v.last_decisions):
                uniq_facts.add(extract_facts(a).key())
                action_ms.append(vd.latency_ms)
                if not dec.cached and dec.conjectures:
                    decision_ms.append(dec.rtt_ms)
                    n_conj += len(dec.conjectures)
                    kernel_us.extend(c["latency_us"] for c in dec.conjectures if isinstance(c.get("latency_us"), int))
                lap = list(vd.failed_policies)
                if FAILCLOSED_POLICY in lap:
                    lap_failclosed += 1
                    st["lap_failclosed"] += 1
                policy_lap.update(p for p in lap if p in BASE_POLICY_IDS)
                if lap:
                    st["lap_violations"] += 1
                if sorted(lap) != reference.evaluate(a):
                    ref_disagreements += 1
                stored = r["stored_failed"]
                if stored is None:
                    disagreements.append({"db": db.name, **r, "lap_failed": lap, "reason": "no stored verdict"})
                    continue
                if FAILCLOSED_POLICY in stored:
                    st["stored_failclosed"] += 1
                    stored_failclosed.append({"db": db.name, "episode_id": r["episode_id"], "stage": r["stage"],
                                              "stored_failed": stored, "lap_failed": lap})
                    continue
                stored_base = sorted(p for p in stored if p in BASE_POLICY_IDS)
                claims.append((len(claims), a, stored_base))
                claim_meta.append((db.name, r))
                policy_stored.update(stored_base)
                if stored_base:
                    st["stored_violations"] += 1
                st["compared"] += 1
                if stored_base == sorted(lap):
                    st["agree"] += 1
                else:
                    st["disagree"] += 1
                    disagreements.append({"db": db.name, **r, "stored_base": stored_base, "lap_failed": lap,
                                          "reference_now": reference.evaluate(a)})
                if db.name == RT_DB and r["arm"] in RT_ARMS and stored_base:
                    rt_blocked.append((db.name, r, a, vd))
            if per_action_kernel:
                base = len(claim_meta) - len(claims)
                bad = await kernel_confirm(v, claims, kstats)
                kstats["claims"] += len(claims)
                st["kernel_confirmed"] = st.get("kernel_confirmed", 0) + len(claims) - len(bad)
                for j in sorted(bad):
                    dbn, rr = claim_meta[base + j]
                    kstats["unconfirmed"].append({"db": dbn, **rr})
        per_db[db.name] = st

    # Red-team blocked actions: re-decide without the cache to record every conjecture submitted.
    rt_out = []
    if rt_blocked:
        v2 = LapVerifier(url=url, concurrency=concurrency, cache=False)
        await v2.check_batch([rt_blocked[0][2]])  # registration
        for dbn, r, a, vd in rt_blocked:
            vv = (await v2.check_batch([a]))[0]
            dec = v2.last_decisions[0]
            f = extract_facts(a)
            rt_out.append({
                "db": dbn, "arm": r["arm"], "mode": r["mode"], "episode_id": r["episode_id"], "stage": r["stage"],
                "trace": r["trace"], "trace_line": r["trace_line"], "action": r["action"],
                "stored_failed": r["stored_failed"], "lap_failed": vv.failed_policies, "lap_ok": vv.ok,
                "facts": dict(zip(f.__dataclass_fields__, f.key())),
                "conjectures": [{"conjecture": c["conjecture"], "result": c["result"],
                                 "worker_latency_us": c["latency_us"], "rtt_ms": round(c["rtt_ms"], 3)}
                                for c in dec.conjectures],
            })

    tot = {k: sum(s[k] for s in per_db.values()) for k in
           ("records", "invalid_actions", "compared", "agree", "disagree", "stored_failclosed", "lap_failclosed",
            "lap_violations", "stored_violations", "missing_traces")}
    tot["agreement_rate"] = round(tot["agree"] / tot["compared"], 6) if tot["compared"] else None
    for s in per_db.values():
        s["agreement_rate"] = round(s["agree"] / s["compared"], 6) if s["compared"] else None
    result = {
        "generated_utc": _utc(time.time()),
        "replay_started_utc": _utc(t_start),
        "provisional": True,
        "label": "PROVISIONAL-NO-BIJECT; kernel-checked policy decision over trusted-Python facts (LAP lean-worker)",
        "lap_worker_url": url,
        "lean_module": "PolicyEnv.PROOFREAD",
        "policy_hash": v.policy_hash,
        "reference_base_policy_hash": reference.BASE_POLICY_HASH,
        "net_allowlist": [],
        "comparison": "stored failed_policies restricted to base policy ids vs LAP failed_policies; "
                      "records whose stored verdict is FAILCLOSED are listed separately, not compared",
        "dbs": per_db,
        "overall": tot,
        "lap_vs_reference_now_disagreements": ref_disagreements,
        "per_policy": {p: {"lap": policy_lap.get(p, 0), "stored": policy_stored.get(p, 0)} for p in BASE_POLICY_IDS},
        "distinct_fact_tuples": len(uniq_facts),
        "kernel_decisions": len(decision_ms),
        "conjectures_submitted": n_conj,
        "latency_ms": {
            "per_action_p50": pct(action_ms, 0.5), "per_action_p95": pct(action_ms, 0.95),
            "per_action_note": "cache hits (fact tuple already decided in this replay) count as 0 ms",
            "per_kernel_decision_p50": pct(decision_ms, 0.5), "per_kernel_decision_p95": pct(decision_ms, 0.95),
            "per_conjecture_worker_p50": pct([x / 1000 for x in kernel_us], 0.5),
            "per_conjecture_worker_p95": pct([x / 1000 for x in kernel_us], 0.95),
            "per_batch_p50": pct(batch_ms, 0.5), "per_batch_p95": pct(batch_ms, 0.95),
            "batch_size": batch, "n_batches": len(batch_ms),
        },
        "per_action_kernel_check": {
            "enabled": per_action_kernel,
            "what": "one `example : verdictBits {facts} = [stored allow bits] := by decide` per compared record, "
                    "submitted in files of up to --batch examples (a proved file proves every example in it); "
                    "failures bisected to single records",
            "claims": kstats["claims"],
            "confirmed": kstats["claims"] - len(kstats["unconfirmed"]),
            "unconfirmed": kstats["unconfirmed"],
            "files_submitted": kstats["files"],
            "per_file_ms_p50": pct(kstats["file_ms"], 0.5), "per_file_ms_p95": pct(kstats["file_ms"], 0.95),
            "per_action_amortized_ms": round(sum(kstats["file_ms"]) / kstats["claims"], 3) if kstats["claims"] else None,
            "example_file_head": (f"import {LEAN_MODULE}\nopen PolicyEnv.Proofread\n" + bits_claim(
                Action(episode_id="x", step=0, kind="write", path="/workspace/test.py",
                       protected_extra=["/workspace/test.py"]), ["CODE-TEST-001"])),
        },
        "disagreements": disagreements,
        "stored_failclosed": stored_failclosed,
        "red_team_blocked": rt_out,
        "policy_predicates": POLICY_PREDICATES,
        "conjecture_template": conjecture.__doc__,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dbs", nargs="*", default=None, help="sqlite files (default: data/*.sqlite with episodes)")
    ap.add_argument("--out", default=str(ROOT / "results" / "final" / "lap_replay.json"))
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--url", default=LAP_URL)
    ap.add_argument("--no-per-action-kernel", action="store_true",
                    help="skip the per-record kernel confirmation of stored verdicts")
    a = ap.parse_args()
    dbs = [Path(p).resolve() for p in (a.dbs or sorted(glob.glob(str(ROOT / "data" / "*.sqlite"))))]
    res = asyncio.run(replay(dbs, Path(a.out), a.batch, a.concurrency, a.url,
                             not a.no_per_action_kernel))
    o = res["overall"]
    pk = {k: v for k, v in res["per_action_kernel_check"].items() if k not in ("unconfirmed", "what")}
    pk["unconfirmed"] = len(res["per_action_kernel_check"]["unconfirmed"])
    print(json.dumps({"overall": o, "per_action_kernel_check": pk, "latency_ms": res["latency_ms"], "per_policy": res["per_policy"],
                      "red_team_blocked": len(res["red_team_blocked"]),
                      "lap_vs_reference_now_disagreements": res["lap_vs_reference_now_disagreements"]}, indent=1))


if __name__ == "__main__":
    main()
