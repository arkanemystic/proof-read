"""Matplotlib (Agg) figures. Every function tolerates empty input and draws 'no data' labels."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from proofread.analysis.metrics import FILE_LEVEL, MECHANISMS, CheatStats, delta_scale  # noqa: E402

# Validated default categorical palette (dataviz skill, light mode), assigned in fixed order.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MECH_COLOR = {m: SERIES[i] for i, m in enumerate(MECHANISMS)}
MECH_LABEL = {"test_edit": "test edit", "skip": "skip", "config_tamper": "config tamper",
              "in_solution": "in-solution", "other_violation": "other violation", "unclassified": "unclassified pass"}
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e6e5e1"


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _fig(w: float = 9, h: float = 4.8):
    fig, ax = plt.subplots(figsize=(w, h), dpi=120)
    fig.patch.set_facecolor(SURFACE)
    _style(ax)
    return fig, ax


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def _nodata(ax, x: float = 0.5, y: float = 0.5, transform=None, text: str = "no data") -> None:
    ax.text(x, y, text, ha="center", va="center", color=MUTED, fontsize=9, style="italic",
            transform=transform if transform is not None else ax.transAxes)


def key_cheat_figure(groups: list[CheatStats], path: Path) -> Path:
    """Stacked bars of cheat rate by mechanism per group, Wilson 95% CI on the total."""
    fig, ax = _fig(max(7.0, 1.1 * len(groups) + 3), 5.2)
    xs = list(range(len(groups)))
    bottoms = [0.0] * len(groups)
    used = set()
    for m in MECHANISMS:
        vals = [100 * g.rate(g.counts[m]) if g.n else 0.0 for g in groups]
        if any(v > 0 for v in vals):
            used.add(m)
        ax.bar(xs, vals, bottom=bottoms, width=0.62, color=MECH_COLOR[m], edgecolor=SURFACE, linewidth=1.5,
               label=MECH_LABEL[m] + (" (file-level)" if m in FILE_LEVEL else ""))
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    for i, g in enumerate(groups):
        if g.n == 0:
            ax.text(i, 2, "no data", ha="center", va="bottom", color=MUTED, fontsize=8, style="italic",
                    rotation=90)
            continue
        lo, hi = g.ci(g.total)
        tot = 100 * g.rate(g.total)
        ax.errorbar([i], [tot], yerr=[[tot - 100 * lo], [100 * hi - tot]], color=INK, capsize=4, linewidth=1.2)
        ax.text(i, 100 * hi + 1.5, f"{tot:.0f}%\nn={g.n}", ha="center", va="bottom", color=INK2, fontsize=7)
    labels = [f"{g.label}\n[{g.mode}]" + ("\n(fallback)" if g.fallback else "") for g in groups]
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=7.5)
    ax.set_ylim(0, 115)
    ax.set_ylabel("impossible tasks passed (%)", color=INK2, fontsize=9)
    ax.set_title("Cheat rate by mechanism (Wilson 95% CI on total)", color=INK, fontsize=11, loc="left")
    handles, labs = ax.get_legend_handles_labels()
    ax.legend(handles[::-1], labs[::-1], fontsize=7.5, frameon=False, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    if not groups:
        _nodata(ax)
    return _save(fig, path)


def generation_figure(per_gen: dict[str, list[dict[str, Any]]], path: Path) -> Path:
    """One panel per arm: file-level violation rate and training pass rate per generation."""
    arms = list(per_gen) or ["A", "C"]
    fig, axes = plt.subplots(1, len(arms), figsize=(5 * len(arms), 4), dpi=120, sharey=True, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    from proofread.analysis.stats import wilson_ci

    for ax, arm in zip(axes[0], arms):
        _style(ax)
        rows = per_gen.get(arm, [])
        ax.set_title(f"arm {arm}", color=INK, fontsize=10, loc="left")
        ax.set_xlabel("generation", color=INK2, fontsize=9)
        if not rows:
            _nodata(ax)
            continue
        gens = [r["generation"] for r in rows]
        series = [
            ("file-level violation episodes", [(r["file_level"], r["n"]) for r in rows], SERIES[0]),
            ("original tasks passed (workspace)", [(r["orig_pass"], r["orig_n"]) for r in rows], SERIES[2]),
            ("in-solution passes (impossible tasks)",
             [(r["cheat"].counts["in_solution"], r["cheat"].n) for r in rows], SERIES[3]),
        ]
        for name, kn, col in series:
            pts = [(g, k, n) for g, (k, n) in zip(gens, kn) if n > 0]
            if not pts:
                continue
            gx = [p[0] for p in pts]
            y = [100 * p[1] / p[2] for p in pts]
            ci = [wilson_ci(p[1], p[2]) for p in pts]
            ax.fill_between(gx, [100 * c[0] for c in ci], [100 * c[1] for c in ci], color=col, alpha=0.15,
                            linewidth=0)
            ax.plot(gx, y, marker="o", markersize=5, linewidth=2, color=col, label=name)
        ax.set_xticks(gens)
        ax.set_ylim(0, 105)
    axes[0][0].set_ylabel("% of episodes (Wilson 95% band)", color=INK2, fontsize=9)
    h, lab = [], []
    for ax in axes[0]:
        hh, ll = ax.get_legend_handles_labels()
        for a, b in zip(hh, ll):
            if b not in lab:
                h.append(a)
                lab.append(b)
    if h:
        fig.legend(h, lab, fontsize=7.5, frameon=False, loc="lower center", ncol=3)
        fig.subplots_adjust(bottom=0.25)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=(0, 0.08 if h else 0, 1, 1))
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def capability_figure(rows: list[dict[str, Any]], path: Path) -> Path:
    fig, ax = _fig(max(6.0, 0.9 * len(rows) + 3), 4.2)
    xs = list(range(len(rows)))
    vals = [100 * r["rate"] if r["n"] else 0.0 for r in rows]
    ax.bar(xs, vals, width=0.6, color=SERIES[0], edgecolor=SURFACE, linewidth=1.5)
    for i, r in enumerate(rows):
        if not r["n"]:
            ax.text(i, 2, "no data", ha="center", va="bottom", color=MUTED, fontsize=8, style="italic", rotation=90)
            continue
        v = 100 * r["rate"]
        ax.errorbar([i], [v], yerr=[[v - 100 * r["lo"]], [100 * r["hi"] - v]], color=INK, capsize=4, linewidth=1.2)
        ax.text(i, 100 * r["hi"] + 1.5, f"{v:.0f}%\nn={r['n']}", ha="center", va="bottom", color=INK2, fontsize=7)
    ax.set_xticks(xs)
    ax.set_xticklabels([r["label"] + ("\n(fallback)" if r.get("fallback") else "") for r in rows],
                       rotation=30, ha="right", fontsize=7.5)
    ax.set_ylim(0, 115)
    ax.set_ylabel("original tasks passed, pristine grader (%)", color=INK2, fontsize=9)
    ax.set_title("Capability: pristine pass rate on originals (Wilson 95% CI)", color=INK, fontsize=11, loc="left")
    return _save(fig, path)


def latency_figure(lat: dict[str, dict[str, Any]], path: Path) -> Path:
    fig, ax = _fig(7, 4)
    labs = [k for k, v in lat.items() if v["n"] > 0]
    if not labs:
        _nodata(ax, text="no verifier latency data")
    else:
        data = [lat[k]["values"] for k in labs]
        bp = ax.boxplot(data, tick_labels=labs, showfliers=True, patch_artist=True, widths=0.5,
                        flierprops={"markersize": 3, "markerfacecolor": MUTED, "markeredgecolor": MUTED})
        for b in bp["boxes"]:
            b.set_facecolor(SERIES[0])
            b.set_alpha(0.5)
            b.set_edgecolor(SERIES[0])
        for med in bp["medians"]:
            med.set_color(INK)
        if all(min(d) > 0 for d in data):
            ax.set_yscale("log")
    ax.set_ylabel("per-batch verifier latency (ms)", color=INK2, fontsize=9)
    ax.set_title("Verifier latency by arm", color=INK, fontsize=11, loc="left")
    return _save(fig, path)


def counterfactual_figure(deltas: list[dict[str, Any]], path: Path) -> Path:
    fig, ax = _fig(7, 4)
    if not deltas:
        _nodata(ax, text="no rejected edits")
    else:
        arms = sorted({str(d["arm"]) for d in deltas})
        scale = delta_scale([d["delta"] for d in deltas])
        for i, arm in enumerate(arms):
            pts = [d for d in deltas if str(d["arm"]) == arm]
            gx = [(d["generation"] if isinstance(d["generation"], (int, float)) else -1) + (i - (len(arms) - 1) / 2) * 0.12
                  for d in pts]
            ax.scatter(gx, [scale * d["delta"] for d in pts], s=36,
                       color=SERIES[i % len(SERIES)], edgecolor=SURFACE, linewidth=1, label=f"arm {arm}", zorder=3)
        ax.axhline(0, color=MUTED, linewidth=1)
        ax.legend(fontsize=8, frameon=False)
    ax.set_xlabel("generation", color=INK2, fontsize=9)
    ax.set_ylabel("counterfactual delta vs champion (points)", color=INK2, fontsize=9)
    ax.set_title("Rejected edits: counterfactual score delta", color=INK, fontsize=11, loc="left")
    return _save(fig, path)


def _finite(x: float) -> bool:
    return not (x is None or math.isnan(x))
