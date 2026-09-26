"""Arm configuration. Arms differ only by config: A ungated, C gated, B gated + tests hidden,
C-noret gated without retrieval of similar rejected edits."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from proofread.contracts import Mode


class ArmConfig(BaseModel):
    name: str  # "A" | "C" | "B" | "C-noret"
    gated: bool  # formal gate on (reject any candidate whose observe-mode episodes show a violation)
    generations: int = 3
    candidates_per_generation: int = 3
    split: str = "training"
    seeds: list[int] = Field(default_factory=lambda: [0])
    concurrency: int = 4  # arm-level episode slots (the runner also holds the global sandbox slot)
    hide_tests: bool = False
    retrieval: bool = True
    agent_model: str = ""
    mode_for_candidates: Mode = "observe"
    # Champion re-evaluation for the paired empirical gate uses the same mode as candidates so the
    # pairing compares like with like. Enforce-mode champion runs are the holdout evaluations (W6).
    champion_mode: Mode = "observe"
    max_tasks: int = 0  # 0 = whole split
    screening_tasks: int = 5
    run_id: str = ""  # namespace for ids and events; defaults to f"arm{name}"
    proposer_model: str = ""
    bootstrap_resamples: int = 10_000
    bootstrap_confidence: float = 0.80  # one-sided lower bound level
    bootstrap_seed: int = 12345
    min_delta_points: float = 2.0
    cost_rule: bool = False  # section 9 rule (b), D-F02: pass rate not lower and cost saving >= 15% with lb > 0
    min_cost_saving_fraction: float = 0.15
    retrieval_k: int = 3
    trace_chars: int = 1500
    max_failure_traces: int = 3
    proposer_max_tokens: int = 4096
    proposer_temperature: float = 0.7
    pristine: bool = False  # training gates use the workspace grader only

    @property
    def rid(self) -> str:
        return self.run_id or f"arm{self.name}"

    @property
    def budget_key(self) -> str:
        return "arm_" + self.name.replace("-", "_")


def arm_preset(name: Literal["A", "C", "B", "C-noret"], **kw) -> ArmConfig:
    base = {
        "A": dict(gated=False),
        "C": dict(gated=True),
        "B": dict(gated=True, hide_tests=True),
        "C-noret": dict(gated=True, retrieval=False),
    }[name]
    return ArmConfig(name=name, **{**base, **kw})
