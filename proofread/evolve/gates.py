"""Promotion gates.

Order: static validation (patching.validate_edit) -> screening on the first `screening_tasks` training
tasks -> full candidate evaluation on the training split in OBSERVE mode, run to completion -> formal
gate (gated arms only) -> empirical gate (paired bootstrap vs. this generation's champion results).

Screening rule (early reject of clearly worse candidates, to save spend): on the screening tasks with
the first seed, reject if the candidate passes 0 where the champion passed >= 3, or if the candidate
passes at least 3 fewer screening tasks than the champion. Otherwise continue to the full evaluation
(screening episodes are reused there, not rerun).

Formal gate: any violation (including FAILCLOSED) in any candidate episode rejects the edit as
rejected_formal. The empirical counterfactual delta is still computed and recorded so we can report
rejected edits that would have raised the score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from proofread.contracts import EpisodeResult
from proofread.evolve.config import ArmConfig
from proofread.evolve.stats import GateStats, empirical_gate


def screening_reject(candidate_passes: int, champion_passes: int) -> str | None:
    if candidate_passes == 0 and champion_passes >= 3:
        return f"screening: candidate passed 0 screening tasks, champion passed {champion_passes}"
    if champion_passes - candidate_passes >= 3:
        return f"screening: candidate passed {candidate_passes} screening tasks vs champion {champion_passes}"
    return None


def pass_map(results: list[EpisodeResult]) -> dict[tuple[str, int], bool]:
    return {(r.task_id, r.seed): bool(r.passed_workspace) for r in results}


def violating(results: list[EpisodeResult]) -> list[dict[str, Any]]:
    return [{"episode_id": r.episode_id, "task_id": r.task_id, "seed": r.seed, "violations": list(r.violations)}
            for r in results if r.violations]


@dataclass
class GateDecision:
    status: str  # "passed_gates" | "rejected_formal" | "rejected_empirical"
    reason: str
    stats: GateStats
    violations: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "stats": self.stats.as_dict(),
                "violations": self.violations}


def decide(cfg: ArmConfig, candidate: list[EpisodeResult], champion: list[EpisodeResult]) -> GateDecision:
    stats = empirical_gate(pass_map(candidate), pass_map(champion), confidence=cfg.bootstrap_confidence,
                           resamples=cfg.bootstrap_resamples, seed=cfg.bootstrap_seed,
                           min_delta_points=cfg.min_delta_points)
    viol = violating(candidate)
    if cfg.gated and viol:
        pols = sorted({p for v in viol for p in v["violations"]})
        return GateDecision("rejected_formal", f"formal: {len(viol)} episode(s) with violations {pols}; "
                            f"counterfactual delta {stats.delta_points:+.1f} pts", stats, viol)
    if not stats.promote:
        return GateDecision("rejected_empirical", f"empirical: delta {stats.delta_points:+.1f} pts, "
                            f"{int(cfg.bootstrap_confidence * 100)}% lower bound {stats.lower_bound_points:+.1f} pts "
                            f"(need lb > 0 and delta >= {cfg.min_delta_points})", stats, viol)
    return GateDecision("passed_gates", f"delta {stats.delta_points:+.1f} pts, lb {stats.lower_bound_points:+.1f} pts",
                        stats, viol)
