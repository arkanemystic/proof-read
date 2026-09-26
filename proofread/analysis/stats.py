"""Small statistics helpers: Wilson score intervals and bootstrap CIs (numpy only)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

Z95 = 1.959963984540054


def wilson_ci(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes out of n. Returns (nan, nan) when n == 0."""
    if n <= 0:
        return (math.nan, math.nan)
    if k < 0 or k > n:
        raise ValueError(f"k={k} outside [0, n={n}]")
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(k: int, n: int) -> float:
    return k / n if n > 0 else math.nan


def fmt_rate(k: int, n: int, digits: int = 1) -> str:
    """'12.5% [8.1, 18.7] (5/40)' or 'no data (n=0)'."""
    if n <= 0:
        return "no data (n=0)"
    lo, hi = wilson_ci(k, n)
    return f"{100 * k / n:.{digits}f}% [{100 * lo:.{digits}f}, {100 * hi:.{digits}f}] ({k}/{n})"


def bootstrap_mean_ci(values: Sequence[float], n_boot: int = 2000, alpha: float = 0.05,
                      seed: int = 0) -> tuple[float, float, float]:
    """Percentile bootstrap CI of the mean. Returns (mean, lo, hi); nan triple when empty."""
    x = np.asarray(list(values), dtype=float)
    if x.size == 0:
        return (math.nan, math.nan, math.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, x.size, size=(n_boot, x.size))
    means = x[idx].mean(axis=1)
    return (float(x.mean()), float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def paired_bootstrap(a: Sequence[float], b: Sequence[float], n_boot: int = 2000, alpha: float = 0.05,
                     seed: int = 0) -> tuple[float, float, float]:
    """Paired bootstrap CI of mean(a - b) over matched units (e.g. tasks). Lengths must match."""
    if len(a) != len(b):
        raise ValueError("paired_bootstrap needs equal-length paired samples")
    d = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    return bootstrap_mean_ci(d, n_boot=n_boot, alpha=alpha, seed=seed)


def pair_by_key(a: dict[str, float], b: dict[str, float]) -> tuple[list[float], list[float]]:
    """Align two {unit_key: value} maps on their shared keys (sorted)."""
    keys = sorted(set(a) & set(b))
    return [a[k] for k in keys], [b[k] for k in keys]


def percentiles(values: Sequence[float], qs: Sequence[float] = (50, 95, 99)) -> dict[float, float]:
    x = np.asarray(list(values), dtype=float)
    if x.size == 0:
        return {q: math.nan for q in qs}
    return {q: float(np.percentile(x, q)) for q in qs}
