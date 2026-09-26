"""Paired bootstrap for the empirical gate.

Pairs are (task_id, seed). Each side is 1.0 (workspace grader passed) or 0.0. Delta is measured in
pass-rate points (x100). The gate promotes iff the one-sided `confidence` paired-bootstrap lower
bound on the mean delta is > 0 AND the observed mean delta >= min_delta_points.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class GateStats:
    n_pairs: int
    delta_points: float
    lower_bound_points: float
    candidate_rate: float
    champion_rate: float
    promote: bool

    def as_dict(self) -> dict:
        return asdict(self)


def paired_bootstrap_lower_bound(deltas: list[float], confidence: float = 0.80, resamples: int = 10_000,
                                 seed: int = 12345) -> float:
    """One-sided lower bound on the mean of `deltas` (same units as deltas)."""
    d = np.asarray(deltas, dtype=np.float64)
    if d.size == 0:
        return float("-inf")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, d.size, size=(resamples, d.size))
    means = d[idx].mean(axis=1)
    return float(np.quantile(means, 1.0 - confidence, method="lower"))


def empirical_gate(candidate: dict[tuple[str, int], bool], champion: dict[tuple[str, int], bool], *,
                   confidence: float = 0.80, resamples: int = 10_000, seed: int = 12345,
                   min_delta_points: float = 2.0) -> GateStats:
    keys = sorted(set(candidate) & set(champion))
    if not keys:
        return GateStats(0, 0.0, float("-inf"), 0.0, 0.0, False)
    c = np.array([1.0 if candidate[k] else 0.0 for k in keys])
    h = np.array([1.0 if champion[k] else 0.0 for k in keys])
    deltas = (c - h) * 100.0
    mean = float(deltas.mean())
    lb = paired_bootstrap_lower_bound(deltas.tolist(), confidence, resamples, seed)
    return GateStats(len(keys), mean, lb, float(c.mean()), float(h.mean()), bool(lb > 0 and mean >= min_delta_points))
