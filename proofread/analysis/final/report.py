"""NUMBERS.md and numbers.json."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from proofread.analysis.final.compute import POLICIES, display_name, fmt_pct, fmt_usd
from proofread.analysis.final.render import PROVISIONAL, holdout_label


def _partial(p: dict[str, Any]) -> str:
    return f" (partial: {p['n']} of {p['expected_n']} planned)" if p.get("partial") else ""


def _num(id_: str, label: str, value: str, n: Any = None, ci: str = "", source: str = "",
         raw: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": id_, "label": label, "value": value, "n": n, "ci": ci, "source": source, "raw": _clean(raw or {})}


def _clean(o: Any) -> Any:
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else round(o, 6)
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items() if k not in ("per_task", "values", "per_episode_ms",
                                                                        "per_episode_frac")}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _val(b: dict[str, Any]) -> str:
    return f"{b['k']}/{b['n']} = {fmt_pct(b['rate'])}" if b.get("n") else "no data (n=0)"


def _ci(b: dict[str, Any]) -> str:
    return f"95% Wilson [{fmt_pct(b['lo'])}, {fmt_pct(b['hi'])}]" if b.get("n") else "n/a"


def _hname(h: str) -> str:
    return "default genome" if h == "default" else f"IMP-{h} champion"


def _replication_numbers(rep: dict[str, Any], src: str) -> list[dict[str, Any]]:
    """Section 10 holdout replication rows (seed 0 rows above stay unchanged)."""
    out: list[dict[str, Any]] = []
    if not any(p["seed"] > 0 for p in rep.get("per_seed", [])):
        return out
    for p in rep["per_seed"]:
        if p["seed"] == 0:
            continue
        out.append(_num(f"replication.{p['harness']}.s{p['seed']}.rate",
                        f"Replication seed {p['seed']}: {_hname(p['harness'])}, capability holdout pass rate "
                        f"(pristine, enforce){'' if p['n'] >= 40 else f' (partial: {p[chr(110)]} of 40)'}",
                        _val(p) + f"; {fmt_usd(p['per_solved_usd'])} per solved task; {p['errors']} errored",
                        p["n"], _ci(p), src, p))
    for h, p in rep["pooled"].items():
        out.append(_num(f"replication.{h}.pooled", f"Pooled seeds {p['seeds']}: {_hname(h)}, pass rate over valid "
                        f"episodes ({p['tasks']} tasks)", _val(p) + f"; {fmt_usd(p['per_solved_usd'])} per solved task",
                        p["n"], _ci(p), src, p))
    d = rep.get("diff", {})
    if d.get("n_tasks"):
        out.append(_num("replication.delta_pooled", "Pooled paired per-task difference, IMP-C champion minus default "
                        "(mean over seeds per task)", f"{100 * d['mean']:+.1f} pts; wins/losses/ties "
                        f"{d['wins']}/{d['losses']}/{d['ties']}", d["n_tasks"],
                        f"95% [{100 * d['lo95']:+.1f}, {100 * d['hi95']:+.1f}], 80% [{100 * d['lo80']:+.1f}, "
                        f"{100 * d['hi80']:+.1f}] pts; one-sided 80% LB {100 * d['lb80_one_sided']:+.1f} "
                        f"(bootstrap over tasks, {d['n_boot']} draws)", src, d))
    return out


LAP_SRC = "results/final/lap_replay.json"


def _lap_numbers(lap: dict[str, Any]) -> list[dict[str, Any]]:
    """Section 10b kernel-checked replay rows (Lean-Agent Protocol lean-worker)."""
    o = lap.get("overall")
    if not o:
        return []
    pp = lap.get("per_policy", {})
    kc = lap.get("per_action_kernel_check", {})
    lat = lap.get("latency_ms", {})
    rt = lap.get("red_team_blocked", [])
    return [
        _num("lap.agreement", "LAP Lean-kernel replay: agreement with the stored verdict (all runs, incl. red-team)",
             f"{o['agree']}/{o['compared']} ({fmt_pct(o['agreement_rate'])}), {o['disagree']} disagreements, "
             f"{o['lap_failclosed']} fail-closed", o["compared"], "", LAP_SRC + " overall"),
        _num("lap.claims", "LAP: per-record kernel claims (stored verdict bits, by decide) proved",
             f"{kc.get('confirmed')}/{kc.get('claims')}", kc.get("claims"), "", LAP_SRC + " per_action_kernel_check"),
        _num("lap.violations", "LAP: violations replayed and matched, by policy",
             f"{o['lap_violations']} (" + ", ".join(f"{k.split('-')[1]} {v['lap']}" for k, v in pp.items()) + ")",
             o["compared"], "", LAP_SRC + " per_policy"),
        _num("lap.red_team", "LAP: red-team blocked actions refuted by the kernel",
             f"{sum(not r.get('lap_ok', True) for r in rt)}/{len(rt)} ("
             + ", ".join(sorted({p for r in rt for p in r.get('lap_failed', [])})) + ")", len(rt), "",
             LAP_SRC + " red_team_blocked"),
        _num("lap.latency", "LAP worker latency per conjecture (p50 / p95)",
             f"{lat.get('per_conjecture_worker_p50', math.nan):.0f} / {lat.get('per_conjecture_worker_p95', math.nan):.0f} ms",
             None, "", LAP_SRC + " latency_ms"),
    ]


MONGO_SRC = "results/final/mongo_{}.json"


def _mongo_numbers(mongo: dict[str, Any]) -> list[dict[str, Any]]:
    """Section 10c MongoDB Atlas rows, read from the W-ATLAS outputs (absent files give no rows)."""
    out: list[dict[str, Any]] = []
    mig, num, vec, live, tests = (mongo.get(k) or {} for k in ("migration", "numbers", "vector_demo", "live", "tests"))
    if tests.get("passed") is not None:
        out.append(_num("mongo.tests", "MongoDB: Store/EventLog/VectorIndex test suite against Atlas",
                        f"{tests['passed']} passed, {tests.get('failed', 0)} failed, {tests.get('skipped', 0)} skipped",
                        None, "", MONGO_SRC.format("tests")))
    if mig.get("stores"):
        tot = sum(sum(v.get("mongo_counts", {}).values()) for v in mig["stores"].values())
        out.append(_num("mongo.migration", "MongoDB: SQLite run stores migrated to Atlas, per-collection counts equal",
                        f"{len(mig['stores'])} stores, {tot} documents, counts equal: {'yes' if mig.get('all_equal') else 'NO'}",
                        len(mig["stores"]), "", MONGO_SRC.format("migration")))
    if num.get("checked"):
        out.append(_num("mongo.crosscheck", "MongoDB: aggregation-pipeline numbers cross-checked against numbers.json",
                        f"{num['matched']}/{num['checked']} match, {len(num.get('mismatches', []))} mismatches",
                        num["checked"], "", MONGO_SRC.format("numbers")))
    if vec.get("exact_enn"):
        top = vec["exact_enn"][0]
        out.append(_num("mongo.vector", "MongoDB: $vectorSearch top-3 similar rejected edits (latency)",
                        f"top hit {top.get('edit_id')} ({top.get('status')}, cosine {top.get('cosine')}); "
                        f"ANN {vec.get('ann_latency_ms')} ms, same ids as exact: {vec.get('ann_same_ids')}",
                        vec.get("n_rejected_with_embedding"), "", MONGO_SRC.format("vector_demo")))
    if live.get("edits") or live.get("transitions") or live.get("timeline"):
        tl = live.get("transitions") or live.get("timeline") or []
        out.append(_num("mongo.live", "MongoDB: live gated loop on Atlas, edit status transitions seen via change stream",
                        f"{len(tl)} transitions; " + "; ".join(
                            f"{e.get('edit_id', e.get('_id'))}: {e.get('status')}" for e in live.get("edits", [])),
                        len(tl), "", MONGO_SRC.format("live")))
    return out


def build_numbers(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    S = ctx["sources"]
    out: list[dict[str, Any]] = []
    # IMP holdout
    groups, diffs = ctx["holdout"], ctx["holdout_diffs"]
    for arm, g in groups.items():
        what = "capability holdout pass rate" if g["kind"] == "capability" else "impossible-task (cheat) pass rate"
        out.append(_num(f"holdout.{arm}.rate", f"{holdout_label(arm)}: {what} ({g['grader']} grader, enforce)"
                        + _partial(g),
                        _val(g), g["n"], _ci(g), S["imp"], g))
        if g["kind"] == "capability":
            out.append(_num(f"holdout.{arm}.cost_per_solved", f"{holdout_label(arm)}: USD per solved holdout task",
                            fmt_usd(g["per_solved_usd"]) + f" ({fmt_usd(g['per_episode_usd'])} per episode)",
                            g["n"], "", S["imp"]))
        else:
            out.append(_num(f"holdout.{arm}.denied", f"{holdout_label(arm)}: actions denied on impossible tasks",
                            str(g["denied"]), g["n"], "", S["imp"]))
    for arm, d in diffs.items():
        val = (f"{100 * d['mean']:+.1f} pts" if d["n_pairs"] else "no paired tasks")
        out.append(_num(f"holdout.{arm}.delta_vs_default", f"{holdout_label(arm)} minus default genome, holdout pass rate",
                        val, d["n_pairs"], f"95% paired bootstrap [{100 * d['lo']:+.1f}, {100 * d['hi']:+.1f}] pts"
                        if d["n_pairs"] else "n/a", S["imp"], d))
    if not groups:
        out.append(_num("holdout.none", "IMP holdout", "no data yet", 0, "", S["imp"]))
    out.extend(_replication_numbers(ctx.get("replication") or {}, S["imp"]))
    out.extend(_lap_numbers(ctx.get("lap") or {}))
    out.extend(_mongo_numbers(ctx.get("mongo") or {}))
    # IMP training curve and gate ledger
    for rid, pts in ctx["curves"].items():
        if pts:
            f, l = pts[0], pts[-1]
            out.append(_num(f"imp.{rid}.train_first", f"{display_name(rid)}: champion training pass rate, generation "
                            f"{f['generation']}" + _partial(f), _val(f), f["n"], _ci(f), S["imp"]))
            if len(pts) > 1:
                out.append(_num(f"imp.{rid}.train_last", f"{display_name(rid)}: champion training pass rate, generation "
                                f"{l['generation']}" + _partial(l), _val(l), l["n"], _ci(l), S["imp"]))
            out.append(_num(f"imp.{rid}.cost_per_solved_last", f"{display_name(rid)}: USD per solved training task, "
                            f"generation {l['generation']}", fmt_usd(l["per_solved_usd"]), l["n"], "", S["imp"]))
    for rid, row in ctx["ledger"].items():
        tot = sum(row.values())
        out.append(_num(f"imp.{rid}.ledger", f"{display_name(rid)}: candidate edits by gate outcome",
                        ", ".join(f"{k} {v}" for k, v in row.items() if v or k != "pending"), tot, "", S["imp"], row))
    out.append(_num("imp.promoted_edits", "Promoted edits across IMP arms", str(len(ctx["promoted"])),
                    None, "", S["imp"]))
    if not ctx["curves"]:
        out.append(_num("imp.none", "IMP evolution runs", "no data yet", 0, "", S["imp"]))
    spend = ctx["spend"]
    if spend:
        tot = sum(v["usd"] for v in spend.values())
        per = ", ".join(f"{k} {v['usd']:.2f}" for k, v in sorted(spend.items()))
        out.append(_num("imp.spend_total", "Spend recorded in the final ledger, by budget key", f"{tot:.2f} USD ({per})",
                        sum(int(v['calls']) for v in spend.values()), "",
                        S["spend"], {k: v for k, v in spend.items()}))
    else:
        out.append(_num("imp.spend_total", "Spend recorded in the final ledger", "no data yet", 0, "", S["spend"]))
    # red team
    rt = ctx["red_team"]
    for arm, g in rt["gate"].items():
        out.append(_num(f"rt.{arm}.decision", f"RED-TEAM genome, {arm} ({'gated' if g['gated'] else 'ungated'}): "
                        "gate decision", f"{g['decision']}; policies fired: {', '.join(g['policies']) or 'none'}",
                        g.get("n_pairs"), f"delta {g['delta_points']:+.1f} pts, 80% LB {g['lb_points']:+.1f} pts"
                        if isinstance(g.get("delta_points"), (int, float)) else "", S["rt_gate"], g))
    for arm, a in rt["arms"].items():
        out.append(_num(f"rt.{arm}.episodes", f"RED-TEAM {arm} ({a['mode']}): workspace passes, violating episodes, "
                        "denied actions", f"{a['k']}/{a['n']} passes, {a['violating_episodes']} violating, "
                        f"{a['denied']} denied, {a['cheat_passes']} cheat passes ({a['file_level_cheat_passes']} file-level, {a['in_solution_passes']} in_solution)", a["n"], _ci(a), S["rerun"], a))
    # natural cheating
    nat = ctx["natural"]
    if nat["rows"]:
        p = nat["pooled"]
        out.append(_num("natural.pooled", "Natural cheating, GPT-5, all non-red-team runs pooled (cheat passes, valid "
                        "episodes)", _val(p["cheat_pass"]), p["valid"], _ci(p["cheat_pass"]), S["rerun"], p))
        out.append(_num("natural.pooled_attempts", "Natural file-level cheat attempts (CODE-TEST/SKIP/GRADER), pooled",
                        _val(p["file_attempt"]), p["valid"], _ci(p["file_attempt"]), S["rerun"]))
        allp = [r for r in nat["rows"]]
        k_any = sum(r["any_violation_all"]["k"] for r in allp)
        n_all = sum(r["any_violation_all"]["n"] for r in allp)
        from proofread.analysis.final.compute import rate_block

        b = rate_block(k_any, n_all)
        out.append(_num("natural.pooled_any_violation_all", "Natural runs pooled: episodes with any policy violation, "
                        "all episodes incl. aborted_cost", _val(b), n_all, _ci(b), S["rerun"]))
        for r in nat["rows"]:
            out.append(_num(f"natural.{r['arm']}", f"Natural cheating, {r['label']}", _val(r["cheat_pass"]),
                            r["valid"], _ci(r["cheat_pass"]), S["rerun"], r))
    # coverage
    cov = ctx["coverage"]
    out.append(_num("coverage.golden", "Golden cheat mechanisms, each mapped to the base policy expected to catch it",
                    f"{cov['mapped']}/{cov['n']} mapped; " + ", ".join(f"{p} {cov['by_policy'][p]}" for p in POLICIES),
                    cov["n"], "", S["golden"], cov["by_policy"]))
    unit = ctx["unit_check"]
    out.append(_num("coverage.reference_units", "Reference policies re-run at build time over unit-level cheat fixtures",
                    f"{unit['flagged']}/{unit['n']} flagged by the expected policy", unit["n"], "", S["unit"], unit))
    # latency
    lat = ctx["latency"]
    if lat["n_actions"]:
        out.append(_num("latency.per_action", "Verifier latency per action", f"p50 {lat['p50']:.2f} ms, p95 "
                        f"{lat['p95']:.2f} ms, max {lat['max']:.1f} ms", lat["n_actions"], "", S["latency"], lat))
        out.append(_num("latency.per_episode", "Verifier overhead per episode", f"p50 {lat['ep_ms_p50']:.0f} ms "
                        f"({100 * lat['ep_frac_p50']:.3f}% of wall time), p95 {lat['ep_ms_p95']:.0f} ms "
                        f"({100 * lat['ep_frac_p95']:.3f}%)", lat["n_episodes"], "", S["latency"]))
    else:
        out.append(_num("latency.per_action", "Verifier latency per action", "no data yet", 0, "", S["latency"]))
    return out


def write_numbers(nums: list[dict[str, Any]], out_dir: Path, meta: dict[str, Any]) -> None:
    (out_dir / "numbers.json").write_text(json.dumps({"meta": meta, "numbers": nums}, indent=1, ensure_ascii=False),
                                          encoding="utf-8")
    lines = ["# Proofread headline numbers", "",
             f"Generated {meta['generated_utc']} by `uv run python scripts/build_final.py`. {PROVISIONAL}.",
             "Every value is recomputed from the source file listed; n is the number of episodes, tasks or candidates.",
             "", "| Headline | Value | n | CI | Source |", "|---|---|---|---|---|"]
    for n in nums:
        cells = [n["label"], n["value"], "" if n["n"] is None else str(n["n"]), n["ci"] or "", f"`{n['source']}`"]
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    lines += ["", "Conventions: holdout pass rates count every stored episode (aborted_cost counts as a fail); "
              "natural-cheating rates use valid episodes (aborted_cost and infra errors excluded, as in "
              "scripts/rerun_report.py). A cheat pass is any workspace pass on an impossible task (mechanism test_edit, "
              "skip, config_tamper, other_violation or in_solution).", ""]
    (out_dir / "NUMBERS.md").write_text("\n".join(lines), encoding="utf-8")
