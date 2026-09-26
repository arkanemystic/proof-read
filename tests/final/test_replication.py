"""Section 10 holdout replication analysis (DECISIONS D-H02)."""

from __future__ import annotations

import math

from proofread.analysis.final import compute as C
from proofread.analysis.final import render as R
from proofread.analysis.final.report import _replication_numbers


def _ep(arm, task, passed, seed=0, cost=0.01, error=None):
    return {"arm": arm, "task_id": task, "seed": seed, "passed_pristine": passed, "passed_workspace": passed,
            "cost_usd": cost, "error": error, "split": "imp_capability_holdout"}


def _episodes():
    eps = []
    for i in range(10):
        t = f"t{i}"
        eps.append(_ep("IMPH_default", t, i < 5))
        eps.append(_ep("IMPH_C", t, i < 6))
        eps.append(_ep("IMPH_default_s1", t, i < 4, 1))
        eps.append(_ep("IMPH_C_s1", t, i < 7, 1))
    eps.append(_ep("IMPH_C_s1", "t9", False, 1, error="HTTPError"))  # errored duplicate: excluded from pooled
    eps.append(_ep("IMPH_C_cheat", "x", False))
    return eps


def test_replication_arms_excluded_from_seed0_groups():
    groups = C.holdout_groups(_episodes())
    assert set(groups) == {"IMPH_default", "IMPH_C", "IMPH_C_cheat"}
    assert groups["IMPH_C"]["k"] == 6 and groups["IMPH_C"]["n"] == 10


def test_replication_per_seed_pooled_and_paired_diff():
    r = C.replication(_episodes(), n_boot=2000)
    seeds = {(p["harness"], p["seed"]): p for p in r["per_seed"]}
    assert seeds[("default", 0)]["k"] == 5 and seeds[("C", 1)]["n"] == 11 and seeds[("C", 1)]["errors"] == 1
    assert r["pooled"]["C"]["n"] == 20 and r["pooled"]["C"]["k"] == 13 and r["pooled"]["default"]["k"] == 9
    d = r["diff"]
    # per task: C mean - default mean; t4: (1+1)/2-(1+0)/2=0.5, t5: 1-0.5=0.5, t6: 0.5-0=0.5, others 0
    assert d["n_tasks"] == 10 and math.isclose(d["mean"], 0.2)
    assert (d["wins"], d["losses"], d["ties"]) == (3, 0, 7)
    assert d["lo95"] <= d["lo80"] <= d["lb80_one_sided"] <= d["mean"] <= d["hi80"] <= d["hi95"]
    assert math.isclose(r["pooled"]["C"]["per_solved_usd"], 0.20 / 13)


def test_replication_figure_and_numbers(tmp_path):
    eps = _episodes()
    groups = C.holdout_groups(eps)
    diffs = C.holdout_diffs(groups)
    rep = C.replication(eps, n_boot=500)
    rep["seed0_diff"] = diffs["IMPH_C"]
    p = R.fig_holdout(groups, diffs, tmp_path / "2.png", "test", rep)
    assert p.exists() and p.stat().st_size > 10_000
    ids = {n["id"] for n in _replication_numbers(rep, "x")}
    assert {"replication.default.s1.rate", "replication.C.s1.rate", "replication.C.pooled",
            "replication.delta_pooled"} <= ids
