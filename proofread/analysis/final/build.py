"""Build all final visuals: `uv run python scripts/build_final.py [--imp-db ...] [--rerun-db ...]`.

Reads every source read-only. Writes only under --out (default results/final/).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from proofread.analysis.final import compute as C
from proofread.analysis.final import render as R
from proofread.analysis.final.io import load_golden, load_json, load_spend, load_store, load_trace
from proofread.analysis.final.report import build_numbers, write_numbers

REPO = Path(__file__).resolve().parents[3]


def _unit_cheats(repo: Path) -> list[tuple[str, Any, str]]:
    p = repo / "tests" / "golden_cheats" / "test_reference_policies.py"
    try:
        spec = importlib.util.spec_from_file_location("_final_ref_cheats", p)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return list(getattr(mod, "CHEATS", []))
    except Exception:  # noqa: BLE001
        return []


def _split_size(split: str) -> int:
    try:
        from proofread.benchmarks.impossiblebench import list_tasks

        return len(list_tasks(split))
    except Exception:  # noqa: BLE001
        return 0


def compute_all(imp_db: str, rerun_db: str, spend_db: str, rt_gate_path: str, repo: Path = REPO) -> dict[str, Any]:
    imp = load_store(imp_db)
    rer = load_store(rerun_db)
    imp_eps = imp.coll("episodes")
    rer_eps = rer.coll("episodes")
    rids = C.imp_run_ids(imp.docs, imp.events)
    curves = {rid: C.improvement_curve(imp_eps, imp.events, rid) for rid in rids}
    holdout = C.holdout_groups(imp_eps)
    C.mark_planned(holdout, _split_size)
    holdout_diffs = C.holdout_diffs(holdout)
    rep = C.replication(imp_eps)
    rep["seed0_diff"] = holdout_diffs.get("IMPH_C")
    tl_ep = C.pick_timeline_episode(rer_eps)
    tl = None
    if tl_ep:
        tp = tl_ep.get("trace_path") or str(repo / "traces" / f"{tl_ep.get('episode_id')}.jsonl")
        if not Path(tp).exists():
            tp = str(repo / "traces" / f"{tl_ep.get('episode_id')}.jsonl")
        tl = C.timeline(load_trace(tp))
        tl["trace_path"] = tp
    golden = load_golden()
    cheats = _unit_cheats(repo)
    rel = lambda p: str(Path(p).resolve().relative_to(repo)) if Path(p).resolve().is_relative_to(repo) else str(p)  # noqa: E731
    sources = {
        "imp": f"{rel(imp_db)} (docs: episodes, edits, harness_versions; events)"
               + ("" if not imp.error else f" [{imp.error}]"),
        "rerun": f"{rel(rerun_db)} (episodes)" + ("" if not rer.error else f" [{rer.error}]"),
        "rt_gate": rel(rt_gate_path),
        "spend": rel(spend_db),
        "golden": "proofread/sandbox/scenarios.py GOLDEN (expect), enforced by tests/golden_cheats/test_golden_docker.py",
        "unit": "tests/golden_cheats/test_reference_policies.py CHEATS through proofread/policies/reference.py",
        "latency": f"EpisodeResult.verifier_latency_ms in {rel(rerun_db)} and {rel(imp_db)}",
        "trace": rel(tl["trace_path"]) if tl else "traces/ (none selected)",
    }
    return {
        "sources": sources, "imp_run_ids": rids, "curves": curves,
        "ledger": C.gate_ledger(imp.coll("edits"), rids),
        "promoted": C.promoted_edits(imp.docs, imp.events, rids),
        "holdout": holdout, "holdout_diffs": holdout_diffs, "replication": rep,
        "lap": load_json(str(repo / "results" / "final" / "lap_replay.json")),
        "mongo": {k: load_json(str(repo / "results" / "final" / f"mongo_{k}.json"))
                  for k in ("migration", "indexes", "numbers", "vector_demo", "live", "actions", "tests")},
        "red_team": C.red_team(rer_eps, load_json(rt_gate_path)), "timeline": tl,
        "natural": C.natural_cheating(rer_eps),
        "coverage": C.coverage(golden), "unit_check": C.reference_unit_check(cheats),
        "latency": C.latency({"rerun": rer_eps, "imp": imp_eps}),
        "spend": C.spend_by_key(load_spend(spend_db)),
        "counts": {"imp_episodes": len(imp_eps), "imp_edits": len(imp.coll("edits")), "imp_events": len(imp.events),
                   "rerun_episodes": len(rer_eps)},
    }


def render_all(ctx: dict[str, Any], fig_dir: Path) -> dict[str, Path]:
    S = ctx["sources"]
    jobs = {
        "improvement": lambda p: R.fig_improvement(ctx["curves"], p, S["imp"]),
        "holdout": lambda p: R.fig_holdout(ctx["holdout"], ctx["holdout_diffs"], p, S["imp"],
                                             ctx.get("replication")),
        "learned": lambda p: R.fig_learned(ctx["promoted"], p, S["imp"]),
        "gate_ledger": lambda p: R.fig_gate_ledger(ctx["ledger"], p, S["imp"]),
        "red_team": lambda p: R.fig_red_team(ctx["red_team"], ctx["timeline"], p,
                                             f"{S['rerun']}; {S['rt_gate']}; {S['trace']}"),
        "natural": lambda p: R.fig_natural(ctx["natural"], p, S["rerun"]),
        "coverage": lambda p: R.fig_coverage(ctx["coverage"], p, S["golden"]),
        "latency": lambda p: R.fig_latency(ctx["latency"], p, S["latency"]),
    }
    out = {}
    for i, (key, fn) in enumerate(jobs.items(), 1):
        p = fig_dir / f"{i}_{key}.png"
        try:
            out[key] = fn(p)
        except Exception as e:  # noqa: BLE001 - one broken figure must not sink the build
            print(f"[build_final] figure {key} failed: {type(e).__name__}: {e}; writing placeholder", file=sys.stderr)
            out[key] = R.nodata_figure(p, f"{i}. {key}", f"render error: {type(e).__name__}: {e}", "n/a")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--imp-db", default=str(REPO / "data/imp.sqlite"))
    ap.add_argument("--rerun-db", default=str(REPO / "data/rerun.sqlite"))
    ap.add_argument("--spend-db", default=str(REPO / "data/final_spend.sqlite"))
    ap.add_argument("--rt-gate", default=str(REPO / "results/rerun/rt_gate.json"))
    ap.add_argument("--out", default=str(REPO / "results/final"))
    a = ap.parse_args(argv)
    t0 = time.time()
    out = Path(a.out)
    fig_dir = out / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    ctx = compute_all(a.imp_db, a.rerun_db, a.spend_db, a.rt_gate)
    figs = render_all(ctx, fig_dir)
    meta = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": ctx["sources"],
            "counts": ctx["counts"], "imp_run_ids": ctx["imp_run_ids"],
            "figures": {k: str(v) for k, v in figs.items()}}
    nums = build_numbers(ctx)
    write_numbers(nums, out, meta)
    print(f"[build_final] {len(figs)} figures, {len(nums)} numbers in {time.time() - t0:.1f}s -> {out}")
    print(f"[build_final] counts: {ctx['counts']}; IMP run ids: {ctx['imp_run_ids'] or 'none'}; "
          f"holdout arms: {list(ctx['holdout']) or 'none'}")
    return 0
