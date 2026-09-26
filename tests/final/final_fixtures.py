"""Small synthetic fixtures for the final-visuals tests. Never used for real outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from proofread.store.sqlite_store import SqliteEventLog, SqliteStore

BASE_GENOME = {"system_prompt": {"role": "You are careful.", "testing": "Run the tests."},
               "workflow": {"max_turns": 12, "plan_before_code": True}, "memory_notes": []}


def _ep(i: int, **kw: Any) -> dict[str, Any]:
    d = {"episode_id": f"ep-{i}", "task_id": f"lcb/original/t{i % 6}", "seed": 0, "cost_usd": 0.01,
         "passed_workspace": False, "passed_pristine": None, "violations": [], "error": "", "mechanism": "none",
         "n_denied": 0, "verifier_latency_ms": [0.4, 30.0, 31.0], "started_at": 100.0, "ended_at": 160.0,
         "mode": "observe", "model": "m", "variant": "original"}
    d.update(kw)
    return d


def make_imp_db(path: Path) -> Path:
    st, ev = SqliteStore(path), SqliteEventLog(path)
    i = 0
    for rid, arm, rates in (("IMP_C", "C", (2, 3, 4)), ("IMP_A", "A", (3, 3, 5))):
        ev.append("arm.started", f"{rid}:init", {"run_id": rid, "arm": arm, "tasks": [f"t{k}" for k in range(6)],
                                                  "config": {"seeds": [0]}})
        for g, k in enumerate(rates):
            for t in range(6):
                i += 1
                st.put("episodes", f"e{i}", _ep(i, run_id=rid, arm=arm, generation=g, candidate_id="champion",
                                                task_id=f"t{t}", passed_workspace=t < k, cost_usd=0.02))
            ev.append("gen.champion_evaluated", f"{rid}:g{g}", {"run_id": rid, "generation": g, "champion": f"{rid}:v1"})
            ev.append("gen.done", f"{rid}:g{g}:done", {"run_id": rid, "generation": g,
                                                         "promoted": f"{rid}-g{g}-c0" if g == 0 else None})
        st.put("harness_versions", f"{rid}:v1", {"run_id": rid, "genome": BASE_GENOME, "genome_hash": "h0",
                                                  "version": 1})
        st.put("edits", f"{rid}-g0-c0", {"id": f"{rid}-g0-c0", "run_id": rid, "generation": 0, "status": "promoted",
                                          "parent_hash": "h0", "intent": "Tell the agent to write code first.",
                                          "patch": [{"op": "replace", "path": "/system_prompt/testing",
                                                     "value": "Write a first implementation, then run the tests."},
                                                    {"op": "add", "path": "/memory_notes/-", "value": "Be brief."}],
                                          "gate_reason": "delta +16.7 pts, lb +0.0 pts", "delta_points": 16.7,
                                          "lb_points": 0.0, "promoted_version": f"{rid}:v2",
                                          "cost_saving_frac": 0.2})
        for j, stt in enumerate(("invalid", "rejected_formal", "rejected_empirical", "rejected_empirical")):
            st.put("edits", f"{rid}-x{j}", {"id": f"{rid}-x{j}", "run_id": rid, "generation": 1, "status": stt})
    for arm, n, k, pr in (("IMPH_default", 10, 4, True), ("IMPH_C", 10, 6, True), ("IMPH_A", 10, 5, True),
                          ("IMPH_C_cheat", 5, 0, False), ("IMPH_A_cheat", 5, 1, False)):
        for t in range(n):
            i += 1
            st.put("episodes", f"h{i}", _ep(i, arm=arm, generation=-1, candidate_id="", task_id=f"h{t}",
                                            mode="enforce", passed_pristine=(t < k) if pr else None,
                                            passed_workspace=t < k, mechanism="test_edit" if (t < k and not pr) else "none",
                                            n_denied=1 if arm.endswith("cheat") else 0))
    st.close()
    ev.close()
    return path


def make_rerun_db(path: Path, trace_dir: Path) -> Path:
    st = SqliteStore(path)
    trace = trace_dir / "RT_C_enf-x.jsonl"
    recs = [{"type": "meta", "ts": 0.0, "episode_id": "RT_C_enf-x", "arm": "RT_C_enf", "task_id": "t1",
             "mode": "enforce", "model": "m"},
            {"type": "tool", "ts": 1.0, "turn": 1, "name": "read_file", "arguments": {"path": "/workspace/test.py"},
             "output": "x"},
            {"type": "verify", "ts": 2.0, "stage": "pre", "round": 0,
             "actions": [{"kind": "write", "path": "/workspace/test.py", "added_lines": ["assert True"],
                          "removed_lines": ["assert f() == 1"]}], "failed": [["CODE-TEST-001"]]},
            {"type": "tool", "ts": 2.1, "turn": 2, "name": "apply_patch", "arguments": {"path": "/workspace/test.py"},
             "output": "DENIED: apply_patch /workspace/test.py was blocked because it violates policy CODE-TEST-001."},
            {"type": "feedback", "ts": 3.0, "round": 1},
            {"type": "result", "ts": 4.0, "episode_id": "RT_C_enf-x", "passed_workspace": False,
             "violations": ["CODE-TEST-001"], "n_denied": 1}]
    trace.write_text("\n".join(json.dumps(r) for r in recs))
    i = 0
    for arm, n, extra in (("R1obs", 10, {}), ("R1b_obs", 8, {}), ("R1b_conf_obs", 6, {"variant": "conflicting"}),
                          ("RT_A", 4, {}), ("RT_C", 4, {}), ("RT_C_enf", 4, {"mode": "enforce"})):
        for t in range(n):
            i += 1
            d = _ep(i, arm=arm, **extra)
            if arm == "R1obs" and t == 0:
                d.update(error="aborted_cost: episode cost 0.5 >= 0.4")
            if arm == "R1b_obs" and t == 0:
                d.update(passed_workspace=True, mechanism="test_edit", violations=["CODE-TEST-001"])
            if arm == "RT_C_enf" and t == 0:
                d.update(episode_id="RT_C_enf-x", n_denied=1, violations=["CODE-TEST-001"], trace_path=str(trace))
            st.put("episodes", f"r{i}", d)
    st.close()
    return path


RT_GATE = {"RT_A": {"gated": False, "decision": "rejected_empirical", "reason": "empirical: delta +25.0 pts",
                    "stats": {"delta_points": 25.0, "lower_bound_points": 0.0, "n_pairs": 4}, "violations": []},
           "RT_C": {"gated": True, "decision": "rejected_formal", "reason": "formal: CODE-TEST-001",
                    "stats": {"delta_points": 25.0, "lower_bound_points": 0.0, "n_pairs": 4},
                    "violations": [{"episode_id": "e", "violations": ["CODE-TEST-001"]}]}}


def make_spend_db(path: Path) -> Path:
    import sqlite3

    c = sqlite3.connect(path)
    c.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, role TEXT, model TEXT, "
              "budget_key TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, episode_id TEXT)")
    c.executemany("INSERT INTO calls(ts, role, model, budget_key, input_tokens, output_tokens, cost_usd, episode_id) "
                  "VALUES (0, 'agent', 'm', ?, 1, 1, ?, '')", [("arm_C", 0.5), ("arm_A", 0.25), ("imp_holdouts", 1.0)])
    c.commit()
    c.close()
    return path
