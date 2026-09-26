"""Rerun (section 7) status and results: R1 observe vs enforce, cheat rate by mechanism, cost.

usage: uv run python scripts/rerun_report.py [--db data/rerun.sqlite] [--out results/rerun]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from proofread.analysis.stats import wilson_ci

FILE_LEVEL = ("CODE-TEST-001", "CODE-SKIP-001", "CODE-GRADER-001")
MECHS = ("test_edit", "skip", "config_tamper", "other_violation", "in_solution")


def load(db: str) -> list[dict]:
    c = sqlite3.connect(db)
    return [json.loads(d) for (d,) in c.execute("SELECT doc FROM docs WHERE collection='episodes'")]


def ci(k: int, n: int) -> str:
    if not n:
        return "n/a"
    lo, hi = wilson_ci(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}% [{100 * lo:.1f}, {100 * hi:.1f}]"


def summarize(eps: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in eps:
        groups[e.get("arm", "")].append(e)
    out = {}
    for arm, g in sorted(groups.items()):
        aborted = [e for e in g if (e.get("error") or "").startswith("aborted_cost")]
        failed_infra = [e for e in g if e not in aborted and (e.get("error") or "").startswith(("BudgetExceeded",
                                                                                                 "episode error"))]
        valid = [e for e in g if e not in aborted and e not in failed_infra]
        n = len(valid)
        mech = Counter(e.get("mechanism") for e in valid)
        passed = [e for e in valid if e.get("passed_workspace")]
        attempts = [e for e in valid if any(v in FILE_LEVEL for v in e.get("violations") or [])]
        cheat_pass = Counter(e.get("mechanism") for e in passed)
        costs = [e.get("cost_usd") or 0.0 for e in g]
        out[arm] = {
            "episodes": len(g), "valid": n, "aborted_cost": len(aborted), "infra_or_budget_error": len(failed_infra),
            "passed_workspace": ci(len(passed), n),
            "mechanism_of_passes": {m: ci(cheat_pass.get(m, 0), n) for m in MECHS},
            "file_level_attempt": ci(len(attempts), n),
            "incl_aborted_passed_workspace": ci(sum(1 for e in g if e.get("passed_workspace")), len(g)),
            "incl_aborted_file_level_attempt": ci(sum(1 for e in g if any(v in FILE_LEVEL for v in e.get("violations")
                                                                          or [])), len(g)),
            "incl_aborted_any_violation": ci(sum(1 for e in g if e.get("violations")), len(g)),
            "mechanism_all": dict(mech),
            "violations_attempted": dict(Counter(v for e in valid for v in set(e.get("violations") or []))),
            "denied_actions": sum(e.get("n_denied") or 0 for e in valid),
            "cost_total_usd": round(sum(costs), 4),
            "cost_per_episode_usd": round(sum(costs) / len(g), 4) if g else 0.0,
            "mean_turns": round(sum(e.get("turns") or 0 for e in g) / len(g), 2) if g else 0.0,
            "cheat_examples": [{"episode_id": e["episode_id"], "task_id": e["task_id"], "mechanism": e.get("mechanism"),
                                "passed_workspace": e.get("passed_workspace"), "violations": e.get("violations"),
                                "n_denied": e.get("n_denied"), "trace": e.get("trace_path")}
                               for e in valid if e.get("violations") or e.get("passed_workspace")][:10],
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/rerun.sqlite")
    ap.add_argument("--out", default="results/rerun")
    a = ap.parse_args()
    s = summarize(load(a.db))
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "summary.json").write_text(json.dumps(s, indent=2))
    print(json.dumps({k: {kk: v[kk] for kk in ("episodes", "valid", "aborted_cost", "passed_workspace",
                                                  "file_level_attempt", "mechanism_all", "cost_per_episode_usd")}
                      for k, v in s.items()}, indent=1))


if __name__ == "__main__":
    main()
