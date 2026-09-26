"""RED-TEAM (RT) gate decisions: the adversarial candidate vs the default-genome champion, per arm.

Champion side: R1b observe episodes of the default genome on the same tasks (D-X05). Candidate side: arm RT_A
(formal gate off) and RT_C (formal gate on) observe episodes. Uses the production gates.decide().
"""
import json
import sqlite3
from pathlib import Path

from proofread.contracts import EpisodeResult
from proofread.evolve.config import ArmConfig
from proofread.evolve.gates import decide

c = sqlite3.connect("data/rerun.sqlite")
eps = [EpisodeResult.model_validate(json.loads(d)) for (d,) in c.execute("SELECT doc FROM docs WHERE collection='episodes'")]


def ok(e: EpisodeResult) -> bool:
    return not e.error.startswith(("aborted_cost", "BudgetExceeded", "episode error"))


out = {}
for arm, gated in (("RT_A", False), ("RT_C", True)):
    cand = [e for e in eps if e.arm == arm]
    tasks = {e.task_id for e in cand}
    champ = [e for e in eps if e.arm == "R1b_obs" and e.task_id in tasks]
    # Aborted episodes stay in the gate inputs: an abort is a fail (no pass) and its violations still count.
    d = decide(ArmConfig(name=arm[-1], gated=gated), cand, champ)
    out[arm] = {"gated": gated, "n_candidate": len(cand), "n_candidate_valid": sum(ok(e) for e in cand),
                "n_champion": len(champ), "decision": "promoted" if d.status == "passed_gates" else d.status,
                **d.as_dict()}
Path("results/rerun/rt_gate.json").write_text(json.dumps(out, indent=2, default=str))
print(json.dumps({k: {kk: v[kk] for kk in ("n_candidate", "n_champion", "decision", "reason")} for k, v in out.items()},
                 indent=1))
