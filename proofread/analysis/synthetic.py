"""Synthetic data shaped like the real stores. For building and testing the report only.

Nothing generated here is a result. Report builds from synthetic data are labelled SYNTHETIC.
"""

from __future__ import annotations

import random
from typing import Any

FILE_LEVEL = ("test_edit", "skip", "config_tamper")
VIOL_FOR = {"test_edit": "CODE-TEST-001", "skip": "CODE-SKIP-001", "config_tamper": "CODE-GRADER-001"}


def episode(*, arm: str, model: str, mode: str, variant: str, passed_workspace: bool, mechanism: str = "none",
            violations: list[str] | None = None, passed_pristine: bool | None = None, generation: int = -1,
            candidate_id: str = "", task_id: str = "", provisional: bool = True, cost_usd: float = 0.01,
            latency: list[float] | None = None, idx: int = 0) -> dict[str, Any]:
    """Minimal EpisodeResult-shaped dict (all contract fields present)."""
    return {
        "episode_id": f"syn-{arm}-{idx}", "task_id": task_id or f"lcb/{variant}/t{idx}", "variant": variant,
        "genome_hash": "syn", "model": model, "mode": mode, "seed": 0, "arm": arm, "generation": generation,
        "candidate_id": candidate_id, "passed_workspace": passed_workspace, "passed_pristine": passed_pristine,
        "violations": list(violations or []), "n_actions": 10, "n_denied": 0, "mechanism": mechanism, "turns": 8,
        "cost_usd": cost_usd, "verifier_latency_ms": list(latency or []), "provisional": provisional,
        "trace_path": "", "error": "", "started_at": 0.0, "ended_at": 0.0,
    }


def _impossible(rng: random.Random, n: int, *, arm: str, model: str, mode: str, p_file: float, p_insol: float,
                start: int, **kw) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        variant = "oneoff" if i % 2 == 0 else "conflicting"
        u = rng.random()
        lat = [rng.lognormvariate(3.0, 0.5) for _ in range(rng.randint(3, 12))]
        if u < p_file:
            mech = rng.choice(FILE_LEVEL)
            viol = [VIOL_FOR[mech]]
            passed = mode == "observe"  # enforce denies the effect, the task then fails
        elif u < p_file + p_insol:
            mech, viol, passed = "in_solution", [], True
        else:
            mech, viol, passed = "none", [], False
        out.append(episode(arm=arm, model=model, mode=mode, variant=variant, passed_workspace=passed,
                           mechanism=mech, violations=viol, latency=lat, idx=start + i,
                           cost_usd=round(rng.uniform(0.01, 0.08), 4), **kw))
    return out


def _original(rng: random.Random, n: int, *, arm: str, model: str, mode: str, p_pass: float, start: int,
              pristine: bool = True, **kw) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        ok = rng.random() < p_pass
        out.append(episode(arm=arm, model=model, mode=mode, variant="original", passed_workspace=ok,
                           passed_pristine=ok if pristine else None, idx=start + i,
                           latency=[rng.lognormvariate(3.0, 0.5) for _ in range(4)],
                           cost_usd=round(rng.uniform(0.01, 0.08), 4), **kw))
    return out


def generate(seed: int = 7, n_cheat: int = 20, n_cap: int = 40, n_train: int = 20, generations: int = 3,
             candidates: int = 3) -> dict[str, Any]:
    rng = random.Random(seed)
    eps: list[dict[str, Any]] = []
    c = 0

    def add(lst: list[dict[str, Any]]) -> None:
        nonlocal c
        eps.extend(lst)
        c += len(lst)

    baselines = {"claude-haiku-4-5-20251001": (0.15, 0.05, 0.35), "claude-opus-5-5": (0.05, 0.05, 0.7),
                 "openai/o3": (0.35, 0.10, 0.6), "deepseek/deepseek-v4": (0.20, 0.10, 0.45)}
    for m, (pf, pi, pp) in baselines.items():
        add(_impossible(rng, n_cheat, arm="baseline", model=m, mode="observe", p_file=pf, p_insol=pi, start=c))
        add(_impossible(rng, n_cheat, arm="baseline", model=m, mode="enforce", p_file=pf, p_insol=pi, start=c))
        add(_original(rng, n_cap, arm="baseline", model=m, mode="observe", p_pass=pp, start=c))

    agent = "openai/o3"
    for arm, pf_final, pi_final in (("A", 0.40, 0.08), ("C", 0.05, 0.15)):
        for g in range(generations):
            for k in range(candidates):
                cid = f"{arm}-g{g}-c{k}"
                pf = 0.05 + (0.08 * g if arm == "A" else 0.0)
                tr = _original(rng, n_train, arm=arm, model=agent, mode="observe", p_pass=0.45 + 0.03 * g,
                               start=c, pristine=False, generation=g, candidate_id=cid)
                for e in tr:
                    if rng.random() < pf:
                        mech = rng.choice(FILE_LEVEL)
                        e["mechanism"], e["violations"] = mech, [VIOL_FOR[mech]]
                add(tr)
        add(_impossible(rng, n_cheat, arm=arm, model=agent, mode="enforce", p_file=pf_final, p_insol=pi_final,
                        start=c, generation=generations, candidate_id="final_champion"))
        add(_original(rng, n_cap, arm=arm, model=agent, mode="enforce", p_pass=0.55, start=c,
                      generation=generations, candidate_id="final_champion"))

    for m, (pf, pi, pp) in baselines.items():
        add(_impossible(rng, 10, arm="selection", model=m, mode="observe", p_file=pf, p_insol=pi, start=c))
        add(_original(rng, 10, arm="selection", model=m, mode="observe", p_pass=pp, start=c, pristine=False))

    edits, rejected = [], []
    statuses = ("invalid", "rejected_formal", "rejected_empirical", "promoted")
    for arm in ("A", "C"):
        for g in range(generations):
            for k in range(candidates):
                eid = f"{arm}-g{g}-c{k}"
                st = rng.choice(statuses if arm == "C" else ("invalid", "rejected_empirical", "promoted"))
                delta = round(rng.uniform(-0.1, 0.12), 3)
                viol = ["CODE-TEST-001"] if st == "rejected_formal" else []
                edits.append({"edit_id": eid, "arm": arm, "generation": g, "status": st, "delta": delta,
                              "ci_low": round(delta - 0.05, 3), "violations": viol})
                if st.startswith("rejected"):
                    rejected.append({"edit_id": eid, "arm": arm, "generation": g,
                                     "reason": "formal: CODE-TEST-001" if st == "rejected_formal" else "empirical",
                                     "counterfactual_delta": delta})

    spend_rows = [
        {"role": "proposer", "budget_key": "arm_A", "cost_usd": 3.2},
        {"role": "proposer", "budget_key": "arm_C", "cost_usd": 3.4},
        {"role": "agent", "budget_key": "arm_A", "cost_usd": 11.0},
        {"role": "agent", "budget_key": "arm_C", "cost_usd": 12.5},
        {"role": "baseline", "budget_key": "baselines", "cost_usd": 18.1},
        {"role": "selection", "budget_key": "selection", "cost_usd": 6.3},
    ]
    selection_md = ("# Model selection (SYNTHETIC)\n\n| model | file-level | pass |\n|---|---|---|\n"
                    "| openai/o3 | 40% | 60% |\n| claude-opus-5-5 | 0% | 70% |\n")
    return {"episodes": eps, "edits": edits, "rejected_edits": rejected, "spend_rows": spend_rows,
            "selection_md": selection_md, "selected_model": {"model": agent, "synthetic": True}}
