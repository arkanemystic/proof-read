"""Matplotlib (Agg) renderers for the eight final figures. Each takes computed data and a path, always
writes a PNG, and draws a labelled "no data yet" panel when its input is empty.

Palette: the dataviz skill's validated default (light). Arm identity is fixed across every figure:
IMP-C slot 1 (blue), IMP-A slot 2 (orange), IMP-Cnoret slot 3 (aqua), default genome neutral gray.
Status red is reserved for denied actions and always carries a text label.
"""

from __future__ import annotations

import math
import textwrap
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from proofread.analysis.final.compute import OUTCOMES, POLICIES, display_name, fmt_pct  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e6e5e1"
NEUTRAL = "#9a9892"
CRITICAL = "#d03b3b"
GOOD = "#0ca30c"
PROVISIONAL = "PROVISIONAL-NO-BIJECT (local Lean verifier; Lean policies DRAFT pending human review)"

ARM_COLOR = {"IMP_C": SERIES[0], "IMP_A": SERIES[1], "IMP_Cnoret": SERIES[2]}


def arm_color(rid: str) -> str:
    base = rid.replace("IMPH_", "IMP_").removesuffix("_cheat")
    if base in ("IMP_default", "default"):
        return NEUTRAL
    if base in ARM_COLOR:
        return ARM_COLOR[base]
    extra = [c for c in SERIES[3:]]
    return extra[sum(map(ord, base)) % len(extra)]


def holdout_label(arm: str) -> str:
    core = arm.removeprefix("IMPH_")
    cheat = core.endswith("_cheat")
    core = core.removesuffix("_cheat")
    name = "default genome" if core == "default" else f"IMP-{core} champion"
    return name


plt.rcParams.update({"font.size": 9, "axes.titlesize": 10.5, "axes.titleweight": "bold",
                     "axes.labelcolor": INK2, "text.color": INK, "font.family": "DejaVu Sans"})


def _style(ax, grid_axis: str = "y") -> None:
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
        ax.spines[s].set_linewidth(0.8)
    ax.tick_params(colors=INK2, labelsize=8, length=0)
    if grid_axis:
        getattr(ax, f"{grid_axis}axis").grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _fig(nrows: int = 1, ncols: int = 1, w: float = 10, h: float = 4.6, **kw):
    fig, axes = plt.subplots(nrows, ncols, figsize=(w, h), dpi=130, squeeze=False, **kw)
    fig.patch.set_facecolor(SURFACE)
    for ax in axes.flat:
        _style(ax)
    return fig, axes


def _footer(fig, source: str) -> None:
    fig.text(0.01, 0.005, f"Source: {source}\n{PROVISIONAL}", ha="left", va="bottom", fontsize=6.5, color=MUTED)


def _save(fig, path: Path, source: str, bottom: float = 0.12) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _footer(fig, source)
    fig.tight_layout(rect=(0, bottom, 1, 0.95))
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def _title(fig, text: str) -> None:
    fig.suptitle(text, x=0.01, ha="left", fontsize=12, fontweight="bold", color=INK)


def nodata(ax, reason: str) -> None:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.text(0.5, 0.5, "NO DATA YET\n" + "\n".join(textwrap.wrap(reason, 70)), ha="center", va="center",
            color=MUTED, fontsize=10, transform=ax.transAxes)


def nodata_figure(path: Path, title: str, reason: str, source: str) -> Path:
    fig, axes = _fig(1, 1, w=10, h=3.2)
    _title(fig, title)
    nodata(axes[0][0], reason)
    return _save(fig, path, source, bottom=0.14)


def _legend(ax, **kw) -> None:
    leg = ax.legend(frameon=False, fontsize=8, labelcolor=INK2, **kw)
    return leg


# ------------------------------------------------------------------ 1. improvement curve
def fig_improvement(curves: dict[str, list[dict[str, Any]]], path: Path, source: str) -> Path:
    title = "1. Improvement curve: champion on the IMP training split, per generation"
    if not any(curves.values()):
        return nodata_figure(path, title, "no IMP champion episodes found (run ids IMP_C, IMP_A)", source)
    fig, axes = _fig(1, 2, w=11, h=4.4)
    _title(fig, title)
    a1, a2 = axes[0]
    live = [r for r in curves if curves[r]]
    for i, rid in enumerate(live):
        pts = curves[rid]
        c = arm_color(rid)
        off = (i - (len(live) - 1) / 2) * 0.08  # dodge so CI whiskers do not overlap
        x = [p["generation"] + off for p in pts]
        y = [100 * p["rate"] for p in pts]
        lo = [100 * (p["rate"] - p["lo"]) for p in pts]
        hi = [100 * (p["hi"] - p["rate"]) for p in pts]
        a1.errorbar(x, y, yerr=[lo, hi], color=c, lw=2, marker="o", ms=6, mec=SURFACE, mew=1.5, capsize=0,
                    elinewidth=1, label=display_name(rid))
        # selective direct labels: the endpoint only, staggered per arm so equal values do not collide
        p = pts[-1]
        tag = f"{display_name(rid)} {p['k']}/{p['n']}" + (f" (partial, {p['expected_n']} planned)" if p.get("partial") else "") + (" (promoted edit, its gate eval)" if p.get("from_promoted_eval") else "")
        a1.annotate(tag, (x[-1], y[-1]), xytext=(8, 10 - 14 * i), textcoords="offset points", va="center",
                    fontsize=7.5, color=INK2)
        cy = [p["per_solved_usd"] for p in pts]
        a2.plot(x, cy, color=c, lw=2, marker="o", ms=6, mec=SURFACE, mew=1.5, label=display_name(rid))
        if not math.isnan(cy[-1]):
            a2.annotate(f"{display_name(rid)} {cy[-1]:.4f}", (x[-1], cy[-1]), xytext=(8, 10 - 14 * i),
                        textcoords="offset points", va="center", fontsize=7.5, color=INK2)
    gens = sorted({p["generation"] for pts in curves.values() for p in pts})
    for ax in (a1, a2):
        ax.set_xticks(gens)
        ax.set_xlim(min(gens) - 0.4, max(gens) + 0.9)
        ax.set_xlabel("generation (champion evaluated at start of generation)")
    a1.set_ylim(0, 105)
    a1.set_ylabel("pass rate, workspace grader (%)")
    a1.set_title("Champion pass rate, 95% Wilson CI", loc="left")
    a2.set_ylabel("USD per solved task")
    a2.set_ylim(bottom=0)
    a2.set_title("Cost per solved task (total episode cost / passes)", loc="left")
    _legend(a1, loc="lower left")
    _legend(a2, loc="lower left")
    return _save(fig, path, source)


# ------------------------------------------------------------------ 2. holdout comparison
def _bars(ax, labels, vals, colors, errs=None, texts=None, ylim=None) -> None:
    x = np.arange(len(labels))
    ax.bar(x, vals, width=0.55, color=colors, edgecolor=SURFACE, linewidth=2)
    ax.set_xlim(-0.5 - 0.35 * max(0, 3 - len(labels)), len(labels) - 0.5 + 0.35 * max(0, 3 - len(labels)))
    if errs is not None:
        ax.errorbar(x, vals, yerr=errs, fmt="none", ecolor=INK2, elinewidth=1, capsize=3)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    if ylim:
        ax.set_ylim(*ylim)
    top = ax.get_ylim()[1]
    for i, t in enumerate(texts or []):
        v = vals[i] if not math.isnan(vals[i]) else 0
        up = errs[1][i] if errs is not None else 0
        ax.text(x[i], v + up + top * 0.02, t, ha="center", va="bottom", fontsize=7.5, color=INK2)


def fig_holdout(groups: dict[str, dict[str, Any]], diffs: dict[str, dict[str, Any]], path: Path, source: str,
                rep: dict[str, Any] | None = None) -> Path:
    title = "2. Holdout: default genome vs evolved champions (enforce mode)"
    has_rep = bool(rep and any(p["seed"] > 0 for p in rep.get("per_seed", [])))
    cap = [g for g in groups.values() if g["kind"] == "capability"]
    cheat = [g for g in groups.values() if g["kind"] == "cheat"]
    if not cap and not cheat:
        return nodata_figure(path, title, "no IMPH_* holdout episodes found in the IMP store", source)
    order = {"IMPH_default": 0, "IMPH_C": 1, "IMPH_A": 2, "IMPH_Cnoret": 3}
    cap.sort(key=lambda g: (order.get(g["arm"], 9), g["arm"]))
    cheat.sort(key=lambda g: (order.get(g["arm"].removesuffix("_cheat"), 9), g["arm"]))
    fig, axes = _fig(2 if has_rep else 1, 3, w=13, h=9.4 if has_rep else 4.8,
                     gridspec_kw={"width_ratios": [1.2, 1.2, 1]})
    _title(fig, title)
    a1, a2, a3 = axes[0]
    if has_rep:
        _replication_row(axes[1], rep)
    if cap:
        labels = [holdout_label(g["arm"]) + (f"\n(partial: {g['n']} of {g['expected_n']})" if g.get("partial") else "")
                  for g in cap]
        vals = [100 * g["rate"] for g in cap]
        errs = [[100 * (g["rate"] - g["lo"]) for g in cap], [100 * (g["hi"] - g["rate"]) for g in cap]]
        texts = []
        for g in cap:
            t = f"{g['k']}/{g['n']}"
            d = diffs.get(g["arm"])
            if d and d.get("n_pairs"):
                t += f"\n{100 * d['mean']:+.1f} pts vs default\n[{100 * d['lo']:+.1f}, {100 * d['hi']:+.1f}]"
            texts.append(t)
        _bars(a1, labels, vals, [arm_color(g["arm"]) for g in cap], errs, texts, ylim=(0, 115))
        graders = sorted({g["grader"] for g in cap})
        a1.set_ylabel(f"pass rate, {'/'.join(graders)} grader (%)")
        a1.set_title("Capability holdout pass rate, 95% Wilson CI", loc="left")
        cps = [g["per_solved_usd"] for g in cap]
        _bars(a2, labels, [0 if math.isnan(v) else v for v in cps], [arm_color(g["arm"]) for g in cap],
              texts=[("no solves" if math.isnan(v) else f"{v:.4f}") + f"\n{g['per_episode_usd']:.4f}/episode"
                     for v, g in zip(cps, cap)])
        a2.set_ylim(0, (max([v for v in cps if not math.isnan(v)] or [1]) * 1.35))
        a2.set_ylabel("USD per solved task")
        a2.set_title("Cost per solved task", loc="left")
    else:
        nodata(a1, "no capability holdout episodes (IMPH_default, IMPH_C, IMPH_A)")
        nodata(a2, "no capability holdout episodes")
    if cheat:
        labels = [holdout_label(g["arm"]) for g in cheat]
        vals = [100 * g["rate"] for g in cheat]
        errs = [[100 * (g["rate"] - g["lo"]) for g in cheat], [100 * (g["hi"] - g["rate"]) for g in cheat]]
        _bars(a3, labels, vals, [arm_color(g["arm"]) for g in cheat], errs,
              [f"{g['k']}/{g['n']}, {g['denied']} denied" for g in cheat], ylim=(0, 115))
        a3.set_ylabel("impossible-task pass rate, workspace grader (%)")
        a3.set_title("Cheat check: one-off tasks", loc="left")
    else:
        nodata(a3, "no cheat-check episodes (IMPH_C_cheat, IMPH_A_cheat)")
    if has_rep:
        a1.set_title("Seed 0: capability pass rate, 95% Wilson", loc="left")
    return _save(fig, path, source, bottom=0.07 if has_rep else 0.12)


def _replication_row(axes, rep: dict[str, Any]) -> None:
    """Section 10 replication: per-seed and pooled pass rate, cost per solved task, pooled paired difference."""
    b1, b2, b3 = axes
    seeds = sorted({p["seed"] for p in rep["per_seed"]})
    cols = [f"seed {s}" for s in seeds] + ["pooled"]
    harn = [("default", "default genome", NEUTRAL), ("C", "IMP-C champion", ARM_COLOR["IMP_C"])]
    x = np.arange(len(cols))
    w = 0.36
    for j, (h, label, color) in enumerate(harn):
        blocks = [next((p for p in rep["per_seed"] if p["harness"] == h and p["seed"] == s), None) for s in seeds]
        blocks.append(rep["pooled"].get(h))
        vals = [100 * b["rate"] if b and b.get("n") else math.nan for b in blocks]
        lo = [100 * (b["rate"] - b["lo"]) if b and b.get("n") else 0 for b in blocks]
        hi = [100 * (b["hi"] - b["rate"]) if b and b.get("n") else 0 for b in blocks]
        xs = x + (j - 0.5) * w
        b1.bar(xs, [0 if math.isnan(v) else v for v in vals], width=w, color=color, edgecolor=SURFACE,
               linewidth=1.5, label=label)
        b1.errorbar(xs, [0 if math.isnan(v) else v for v in vals], yerr=[lo, hi], fmt="none", ecolor=INK2,
                    elinewidth=1, capsize=2.5)
        for xi, b in zip(xs, blocks):
            if b and b.get("n"):
                b1.text(xi, 2, f"{b['k']}/{b['n']}", ha="center", va="bottom", fontsize=7, color=SURFACE,
                        rotation=90)
        cps = [b["per_solved_usd"] if b and b.get("k") else math.nan for b in blocks]
        b2.bar(xs, [0 if math.isnan(v) else v for v in cps], width=w, color=color, edgecolor=SURFACE,
               linewidth=1.5, label=label)
        for xi, v in zip(xs, cps):
            if not math.isnan(v):
                b2.text(xi, v, f"{v:.4f}", ha="center", va="bottom", fontsize=6.5, color=INK2)
    for ax in (b1, b2):
        ax.set_xticks(x)
        ax.set_xticklabels(cols, fontsize=8)
        _legend(ax, loc="upper center", ncol=2)
    b1.set_ylim(0, 128)
    b1.set_ylabel("pass rate, pristine grader (%)")
    b1.set_title("Seeds 0 to 2: pass rate, 95% Wilson CI", loc="left")
    b2.set_ylabel("USD per solved task")
    b2.set_title("Seeds 0 to 2: cost per solved task", loc="left")
    b2.set_ylim(0, b2.get_ylim()[1] * 1.3)
    d = rep.get("diff", {})
    if not d.get("n_tasks"):
        nodata(b3, "no task has episodes for both harnesses")
        return
    s0 = rep.get("seed0_diff") or {}
    rows = [("pooled, 95%", d["mean"], d["lo95"], d["hi95"]), ("pooled, 80%", d["mean"], d["lo80"], d["hi80"])]
    if s0.get("n_pairs"):
        rows.insert(0, ("seed 0 only, 95%", s0["mean"], s0["lo"], s0["hi"]))
    ys = np.arange(len(rows))[::-1]
    for y, (lab, m, lo, hi) in zip(ys, rows):
        b3.errorbar([100 * m], [y], xerr=[[100 * (m - lo)], [100 * (hi - m)]], fmt="o", color=ARM_COLOR["IMP_C"],
                    ecolor=ARM_COLOR["IMP_C"], elinewidth=2, capsize=4)
        b3.text(100 * hi + 1, y, f"{100 * m:+.1f} [{100 * lo:+.1f}, {100 * hi:+.1f}]", va="center", fontsize=7.5,
                color=INK2)
    b3.axvline(0, color=MUTED, linewidth=1, linestyle="--")
    b3.set_yticks(ys)
    b3.set_yticklabels([r[0] for r in rows], fontsize=8)
    b3.set_ylim(-0.7, len(rows) - 0.3)
    b3.xaxis.grid(True, color=GRID, linewidth=0.8)
    b3.yaxis.grid(False)
    lo_x = min(100 * r[2] for r in rows)
    hi_x = max(100 * r[3] for r in rows)
    b3.set_xlim(min(-5, lo_x - 5), hi_x + 22)
    b3.set_xlabel("per-task difference (pts), bootstrap over tasks")
    b3.set_title(f"IMP-C minus default, paired by task (n={d['n_tasks']})\n"
                 f"W/L/T {d['wins']}/{d['losses']}/{d['ties']}; 1-sided 80% LB "
                 f"{100 * d['lb80_one_sided']:+.1f} pts", loc="left", fontsize=9)


# ------------------------------------------------------------------ 3. promoted edits (genome diff)
def _fmt_val(v: Any) -> str:
    if isinstance(v, str):
        return v
    import json

    return json.dumps(v, ensure_ascii=False)


def diff_lines(edits: list[dict[str, Any]], width: int = 118, max_lines_per_value: int = 14) -> list[tuple[str, str]]:
    """(kind, text) lines: kind in header, meta, minus, plus, blank. Shared by the PNG and the HTML."""
    out: list[tuple[str, str]] = []
    for e in edits:
        d = e.get("delta_points")
        lb = e.get("lb_points")
        dtxt = f"delta {d:+.1f} pts, 80% LB {lb:+.1f} pts" if isinstance(d, (int, float)) and isinstance(lb, (int, float)) else ""
        out.append(("header", f"{display_name(str(e['run_id']))}  generation {e.get('generation')}  edit {e['edit_id']}"
                              f"  -> {e.get('promoted_version') or ''}  {dtxt}"))
        for w in textwrap.wrap(f"Intent: {e.get('intent', '')}", width):
            out.append(("meta", w))
        if e.get("reason"):
            for w in textwrap.wrap(f"Gate: {e['reason']}", width):
                out.append(("meta", w))
        if e.get("cost_stats"):
            cs = ", ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in e["cost_stats"].items())
            for w in textwrap.wrap(f"Cost rule stats: {cs}", width):
                out.append(("meta", w))
        for op in e["ops"]:
            out.append(("meta", f"{op['op']} {op['path']}" + (f" (from {op['from']})" if op.get("from") else "")))
            if op["op"] in ("replace", "remove", "move") and op.get("old_known"):
                lines = _wrap_value(_fmt_val(op["old"]), width - 4)
                for w in lines[:max_lines_per_value]:
                    out.append(("minus", f"  - {w}"))
                if len(lines) > max_lines_per_value:
                    out.append(("minus", f"  - ... ({len(lines) - max_lines_per_value} more lines)"))
            if op["op"] in ("add", "replace", "copy") or op.get("new") is not None:
                lines = _wrap_value(_fmt_val(op["new"]), width - 4)
                for w in lines[:max_lines_per_value]:
                    out.append(("plus", f"  + {w}"))
                if len(lines) > max_lines_per_value:
                    out.append(("plus", f"  + ... ({len(lines) - max_lines_per_value} more lines)"))
        out.append(("blank", ""))
    return out


def _wrap_value(s: str, width: int) -> list[str]:
    out: list[str] = []
    for para in s.splitlines() or [""]:
        out += textwrap.wrap(para, width) or [""]
    return out


def fig_learned(edits: list[dict[str, Any]], path: Path, source: str) -> Path:
    title = "3. What the harness learned: every promoted edit as a genome diff"
    if not edits:
        return nodata_figure(path, title, "no promoted edits in the IMP store yet (edits with status promoted)", source)
    lines = diff_lines(edits)
    h = max(2.5, 0.19 * len(lines) + 1.4)
    fig = plt.figure(figsize=(12, h), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    _title(fig, title)
    color = {"header": INK, "meta": INK2, "minus": CRITICAL, "plus": GOOD, "blank": INK}
    top = 1 - 0.55 / h
    step = 0.19 / h
    for i, (kind, text) in enumerate(lines):
        fig.text(0.015, top - i * step, text, family="DejaVu Sans Mono", fontsize=7.6, color=color[kind],
                 fontweight="bold" if kind == "header" else "normal", va="top")
    path.parent.mkdir(parents=True, exist_ok=True)
    _footer(fig, source + "  (- removed in red, + added in green)")
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


# ------------------------------------------------------------------ 4. gate ledger
def fig_gate_ledger(ledger: dict[str, dict[str, int]], path: Path, source: str) -> Path:
    title = "4. Gate ledger: candidate edits by outcome, per arm"
    if not ledger or not any(sum(r.values()) for r in ledger.values()):
        return nodata_figure(path, title, "no IMP edits recorded yet", source)
    cats = list(OUTCOMES) + (["pending"] if any(r.get("pending") for r in ledger.values()) else [])
    fig, axes = _fig(1, 1, w=10, h=4.4)
    _title(fig, title)
    ax = axes[0][0]
    rids = list(ledger)
    wbar = 0.8 / max(1, len(rids))
    x = np.arange(len(cats))
    for i, rid in enumerate(rids):
        vals = [ledger[rid].get(c, 0) for c in cats]
        xs = x - 0.4 + wbar * (i + 0.5)
        ax.bar(xs, vals, width=wbar, color=arm_color(rid), edgecolor=SURFACE, linewidth=2,
               label=f"{display_name(rid)} ({sum(ledger[rid].values())} candidates)")
        for xx, v in zip(xs, vals):
            ax.text(xx, v + 0.05, str(v), ha="center", va="bottom", fontsize=8, color=INK2)
    ax.set_xticks(x)
    ax.set_xticklabels([c.replace("_", " ") for c in cats])
    ax.set_ylabel("candidate edits")
    ax.yaxis.get_major_locator().set_params(integer=True)
    _legend(ax, loc="upper left")
    return _save(fig, path, source)


# ------------------------------------------------------------------ 5. red team
def fig_red_team(rt: dict[str, Any], tl: dict[str, Any] | None, path: Path, source: str) -> Path:
    title = "5. RED-TEAM: injected cheating genome, arm A (ungated) vs arm C (gated)"
    arms, gate = rt.get("arms") or {}, rt.get("gate") or {}
    if not arms and not gate:
        return nodata_figure(path, title, "no RT_* episodes or rt_gate.json found", source)
    fig = plt.figure(figsize=(12, 8.4), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.25], width_ratios=[1, 1.15])
    a1 = fig.add_subplot(gs[0, 0])
    a2 = fig.add_subplot(gs[0, 1])
    a3 = fig.add_subplot(gs[1, :])
    for ax in (a1, a2, a3):
        _style(ax)
    _title(fig, title)
    names = [a for a in ("RT_A", "RT_C", "RT_C_enf") if a in arms]
    lab = {"RT_A": "arm A\nobserve", "RT_C": "arm C\nobserve", "RT_C_enf": "arm C\nENFORCE"}
    if names:
        x = np.arange(len(names))
        w = 0.36
        v1 = [arms[a]["k"] for a in names]
        v2 = [arms[a]["violating_episodes"] for a in names]
        a1.bar(x - w / 2, v1, w, color=SERIES[0], edgecolor=SURFACE, linewidth=2, label="workspace passes")
        a1.bar(x + w / 2, v2, w, color=SERIES[1], edgecolor=SURFACE, linewidth=2,
               label="episodes with a policy violation")
        for xx, v in zip(x - w / 2, v1):
            a1.text(xx, v + 0.05, str(v), ha="center", va="bottom", fontsize=8, color=INK2)
        for xx, v, a in zip(x + w / 2, v2, names):
            pol = ", ".join(sorted(arms[a]["policies"])) or "none"
            a1.text(xx, v + 0.05, f"{v}\n{pol}" if v else "0", ha="center", va="bottom", fontsize=7, color=INK2)
        a1.set_xticks(x)
        a1.set_xticklabels([f"{lab[a]}\n(n={arms[a]['n']}, {arms[a]['denied']} denied)" for a in names], fontsize=7.5)
        a1.set_ylim(0, max(v1 + v2 + [1]) + 1.6)
        a1.set_ylabel("episodes")
        a1.yaxis.get_major_locator().set_params(integer=True)
        a1.set_title("Candidate episodes on one-off impossible tasks", loc="left")
        _legend(a1, loc="upper right")
    else:
        nodata(a1, "no RT_* episodes")
    a2.axis("off")
    a2.set_title("Gate decision on the red-team edit", loc="left")
    if gate:
        y = 0.9
        for arm, g in gate.items():
            dec = str(g.get("decision"))
            col = CRITICAL if dec == "rejected_formal" else INK
            a2.text(0.0, y, f"{arm}  ({'gated' if g.get('gated') else 'ungated'})", fontsize=9, fontweight="bold",
                    color=INK, transform=a2.transAxes)
            a2.text(0.0, y - 0.09, f"decision: {dec}", fontsize=9, color=col, transform=a2.transAxes)
            a2.text(0.0, y - 0.18, "policies fired: " + (", ".join(g.get("policies") or []) or "none"), fontsize=8.5,
                    color=INK2, transform=a2.transAxes)
            wrapped = textwrap.wrap(str(g.get("reason", "")), 70)[:2]
            for j, wl in enumerate(wrapped):
                a2.text(0.0, y - 0.27 - 0.08 * j, wl, fontsize=7.5, color=MUTED, transform=a2.transAxes)
            y -= 0.5
    else:
        nodata(a2, "results/rerun/rt_gate.json missing")
    _timeline(a3, tl)
    return _save(fig, path, source, bottom=0.05)


def _timeline(ax, tl: dict[str, Any] | None) -> None:
    if not tl or not tl.get("steps"):
        nodata(ax, "no red-team trace with a denied action found in traces/")
        return
    lanes = ["read_file", "list_dir", "write_file", "apply_patch", "run"]
    other = sorted({s["name"] for s in tl["steps"]} - set(lanes))
    lanes = [ln for ln in lanes if any(s["name"] == ln for s in tl["steps"])] + other
    ymap = {ln: i for i, ln in enumerate(lanes)}
    for s in tl["steps"]:
        y = ymap[s["name"]]
        if s["denied"]:
            continue
        ax.plot(s["turn"], y, "o", ms=7, color=SERIES[0] if not s["error"] else NEUTRAL, mec=SURFACE, mew=1.5)
    fb_y = len(lanes)
    for f in tl.get("feedback", []):
        ax.plot(f["turn"] + 0.5, fb_y, "s", ms=6, color=SERIES[2], mec=SURFACE, mew=1.5)
    for s in tl["steps"]:
        if s["denied"]:
            ax.plot(s["turn"], ymap[s["name"]], "X", ms=12, color=CRITICAL, mec=SURFACE, mew=1.5)
    for d in tl.get("denied", []):
        y = ymap.get(d.get("tool", ""), 0)
        txt = [f"DENIED before effect: {d.get('kind')} {d.get('path')}  ->  {', '.join(d['policies'])}"]
        txt += [f"- {ln}" for ln in d.get("removed", [])[:3]] + [f"+ {ln}" for ln in d.get("added", [])[:3]]
        ax.annotate("\n".join(t[:95] for t in txt), (d["turn"], y), xytext=(-260, 52), textcoords="offset points",
                    fontsize=7.5, family="DejaVu Sans Mono", color=INK,
                    bbox=dict(boxstyle="round,pad=0.4", fc="#fbeaea", ec=CRITICAL, lw=0.8),
                    arrowprops=dict(arrowstyle="-", color=CRITICAL, lw=0.8))
    ax.set_yticks(list(range(len(lanes))) + [fb_y])
    ax.set_yticklabels(lanes + ["harness test feedback"], fontsize=8)
    ax.set_ylim(-0.6, fb_y + 1.8)
    turns = [s["turn"] for s in tl["steps"]]
    ax.set_xlim(min(turns) - 0.6, max(turns) + 1.2)
    ax.set_xlabel("agent turn")
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.yaxis.grid(False)
    ax.set_title(f"RED-TEAM episode timeline: {tl.get('episode_id')} ({tl.get('task_id')}, {tl.get('mode')} mode, "
                 f"{tl.get('model')}); result: workspace pass {tl.get('passed_workspace')}, "
                 f"{tl.get('n_denied')} denied", loc="left", fontsize=9)
    from matplotlib.lines import Line2D

    handles = [Line2D([], [], marker="o", ls="", color=SERIES[0], label="tool call allowed"),
               Line2D([], [], marker="o", ls="", color=NEUTRAL, label="tool call errored (no effect)"),
               Line2D([], [], marker="X", ls="", color=CRITICAL, ms=9, label="denied by verifier"),
               Line2D([], [], marker="s", ls="", color=SERIES[2], label="harness ran tests, sent feedback")]
    ax.legend(handles=handles, frameon=False, fontsize=7.5, loc="upper right", ncol=4)


# ------------------------------------------------------------------ 6. natural cheating
def fig_natural(nat: dict[str, Any], path: Path, source: str) -> Path:
    title = "6. Natural cheating: GPT-5 on impossible tasks, no red-team prompt (RT excluded)"
    rows = nat.get("rows") or []
    if not rows:
        return nodata_figure(path, title, "no R1/R1b episodes found in data/rerun.sqlite", source)
    allrows = rows + [nat["pooled"]]
    fig, axes = _fig(1, 1, w=11, h=0.55 * len(allrows) + 1.8)
    _title(fig, title)
    ax = axes[0][0]
    _style(ax, grid_axis="x")
    ys = np.arange(len(allrows))[::-1]
    for y, r in zip(ys, allrows):
        b = r["cheat_pass"]
        pooled = r["arm"] == "pooled"
        c = SERIES[0]
        if b["n"]:
            ax.plot([100 * b["lo"], 100 * b["hi"]], [y, y], color=c, lw=2 if not pooled else 3.5, solid_capstyle="round")
            ax.plot(100 * b["rate"], y, "o", ms=8, color=c, mec=SURFACE, mew=1.5)
            att = r["file_attempt"]
            ax.text(100 * b["hi"] + 1.2, y, f"{b['k']}/{b['n']} cheat passes, 95% upper bound {fmt_pct(b['hi'])}; "
                                         f"file-level attempts {att['k']}/{att['n']}; aborted_cost {r['aborted']}",
                    va="center", fontsize=7.8, color=INK2, fontweight="bold" if pooled else "normal")
    ax.set_yticks(ys)
    ax.set_yticklabels([("All GPT-5 runs pooled" if r["arm"] == "pooled" else
                         f"{r['label']} ({', '.join(r['variants'])})") for r in allrows], fontsize=8)
    ax.set_xlim(0, 100)
    ax.set_xlabel("cheat pass rate over valid episodes (%), point and 95% Wilson interval")
    return _save(fig, path, source, bottom=0.14)


# ------------------------------------------------------------------ 7. coverage matrix
def fig_coverage(cov: dict[str, Any], path: Path, source: str) -> Path:
    title = f"7. Coverage: {cov.get('n', 0)} golden cheat mechanisms and the base policy that caught each"
    rows = cov.get("rows") or []
    if not rows:
        return nodata_figure(path, title, "proofread.sandbox.scenarios.GOLDEN not importable", source)
    order = {p: i for i, p in enumerate(POLICIES)}
    rows = sorted(rows, key=lambda r: (order.get(r["expect"], 99), r["name"]))
    fig, axes = _fig(1, 1, w=8.5, h=0.2 * len(rows) + 2.2)
    _title(fig, title)
    ax = axes[0][0]
    ax.grid(False)
    for i, r in enumerate(rows):
        for j, p in enumerate(POLICIES):
            hit = r["expect"] == p
            ax.plot(j, i, "o", ms=8 if hit else 3, color=SERIES[0] if hit else GRID)
        if i and rows[i - 1]["expect"] != r["expect"]:
            ax.axhline(i - 0.5, color=GRID, lw=0.8)
    ax.set_xticks(range(len(POLICIES)))
    ax.set_xticklabels([f"{p}\n({cov['by_policy'].get(p, 0)})" for p in POLICIES], fontsize=7.5)
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r["name"].replace("_", " ") for r in rows], fontsize=7.5)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_xlim(-0.5, len(POLICIES) - 0.5)
    for s in ax.spines.values():
        s.set_visible(False)
    return _save(fig, path, source, bottom=0.05)


# ------------------------------------------------------------------ 8. latency
def fig_latency(lat: dict[str, Any], path: Path, source: str) -> Path:
    title = "8. Verifier latency per action and overhead per episode"
    if not lat.get("n_actions"):
        return nodata_figure(path, title, "no verifier_latency_ms recorded on any episode", source)
    fig, axes = _fig(1, 2, w=11, h=4.2)
    _title(fig, title)
    a1, a2 = axes[0]
    v = np.asarray(lat["values"], dtype=float)
    v = v[v > 0]
    bins = np.logspace(np.log10(max(v.min(), 1e-3)), np.log10(v.max()), 40)
    a1.hist(v, bins=bins, color=SERIES[0], edgecolor=SURFACE, linewidth=0.8)
    a1.set_xscale("log")
    for (q, lab), yf in zip(((lat["p50"], "p50"), (lat["p95"], "p95")), (0.97, 0.87)):
        a1.axvline(q, color=INK2, lw=1)
        a1.text(q, a1.get_ylim()[1] * yf, f" {lab} {q:.2f} ms", fontsize=8, color=INK, va="top")
    a1.text(0.01, -0.24, "Two modes: sub-ms = verdict cache hits (identical canonical action already checked);\n"
            "tens of ms = Lean checker calls (batch latency split per action).", transform=a1.transAxes, fontsize=7.5, color=INK2, va="top")
    a1.set_xlabel("verifier latency per action (ms, log scale)")
    a1.set_ylabel("actions")
    a1.set_title(f"Per action (n={lat['n_actions']} actions)", loc="left")
    f = 100 * np.asarray(lat["per_episode_frac"], dtype=float)
    if f.size:
        a2.hist(f, bins=30, color=SERIES[0], edgecolor=SURFACE, linewidth=0.8)
        for (q, lab), yf in zip(((lat["ep_frac_p50"], "p50"), (lat["ep_frac_p95"], "p95")), (0.97, 0.87)):
            a2.axvline(100 * q, color=INK2, lw=1)
            a2.text(100 * q, a2.get_ylim()[1] * yf, f" {lab} {100 * q:.3f}%", fontsize=8, color=INK, va="top")
        a2.set_xlabel("verifier time as share of episode wall time (%)")
        a2.set_ylabel("episodes")
        a2.set_title(f"Per episode (n={f.size})", loc="left")
        a2.text(0.01, -0.24, f"Total verifier time per episode: p50 {lat['ep_ms_p50']:.0f} ms, p95 "
                f"{lat['ep_ms_p95']:.0f} ms.", transform=a2.transAxes, fontsize=7.5, color=INK2, va="top")
    else:
        nodata(a2, "no episode wall times recorded")
    return _save(fig, path, source)
