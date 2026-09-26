"""Regression: W5 stores counterfactual_delta_points; the report must read it (integrator fix, Phase V)."""

from proofread.analysis import metrics as M


def test_rejected_positive_reads_w5_key():
    rej = [{"edit_id": "e1", "arm": "C", "generation": 0, "status": "rejected_empirical",
            "counterfactual_delta_points": 12.5},
           {"edit_id": "e2", "arm": "C", "generation": 0, "status": "rejected_empirical",
            "counterfactual_delta_points": 0.0}]
    rows = M.rejected_positive(rej, [])
    assert [r["edit_id"] for r in rows] == ["e1"] and rows[0]["delta"] == 12.5
