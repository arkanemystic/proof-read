"""NUMBERS.md, numbers.json and the self-contained dashboard.html."""

from __future__ import annotations

import base64
import html
import json
import math
from pathlib import Path
from typing import Any

from proofread.analysis.final.compute import POLICIES, display_name, fmt_pct, fmt_usd
from proofread.analysis.final.render import PROVISIONAL, diff_lines, holdout_label


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


# ------------------------------------------------------------------ dashboard
CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;--muted:#898781;
--grid:#e6e5e1;--good:#0ca30c;--critical:#d03b3b;font-family:system-ui,-apple-system,Segoe UI,sans-serif;
background:var(--surface-1);color:var(--text-primary);max-width:1180px;margin:0 auto;padding:24px}
body{margin:0;background:#fcfcfb}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px;border-top:1px solid var(--grid);padding-top:16px}
.sub{color:var(--text-secondary);font-size:13px} .banner{border:1px solid var(--grid);padding:8px 12px;border-radius:6px;
font-size:13px;color:var(--text-secondary);margin:12px 0}
img{max-width:100%;border:1px solid var(--grid);border-radius:4px}
table{border-collapse:collapse;font-size:12.5px;margin:8px 0;width:100%}
th,td{border-bottom:1px solid var(--grid);padding:4px 8px;text-align:left;vertical-align:top}
th{color:var(--text-secondary);font-weight:600} td.num{font-variant-numeric:tabular-nums}
details{margin:6px 0} summary{cursor:pointer;color:var(--text-secondary);font-size:13px}
pre.diff{font-size:12px;background:#f6f5f2;padding:10px;border-radius:4px;white-space:pre-wrap}
.minus{color:var(--critical)} .plus{color:var(--good)} .hdr{font-weight:700} .meta{color:var(--text-secondary)}
.redteam{border-left:4px solid var(--critical);padding-left:10px}
nav a{margin-right:10px;font-size:13px;color:var(--text-secondary)}
"""


def _img(path: Path) -> str:
    try:
        b = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return "<p class='sub'>figure missing</p>"
    return f"<img alt='{html.escape(path.stem)}' src='data:image/png;base64,{b}'>"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "<p class='sub'>No data yet.</p>"
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<details><summary>Table view</summary><table><tr>{h}</tr>{body}</table></details>"


def write_dashboard(ctx: dict[str, Any], figs: dict[str, Path], nums: list[dict[str, Any]], out: Path,
                    meta: dict[str, Any]) -> None:
    S = ctx["sources"]
    sec: list[str] = []

    def section(key: str, title: str, body: str, cls: str = "") -> None:
        sec.append(f"<section id='{key}' class='{cls}'><h2>{html.escape(title)}</h2>{_img(figs[key])}{body}</section>")

    rows = []
    for rid, pts in ctx["curves"].items():
        for p in pts:
            rows.append([display_name(rid), p["generation"], f"{p['k']}/{p['n']}", fmt_pct(p["rate"]),
                         f"[{fmt_pct(p['lo'])}, {fmt_pct(p['hi'])}]", fmt_usd(p["per_solved_usd"]),
                         p.get("promoted_after") or "none"])
    section("improvement", "1. Improvement curve", _table(["Arm", "Generation", "Passes", "Rate", "95% Wilson",
                                                            "USD per solved", "Promoted after gen"], rows))
    rows = [[holdout_label(a), g["kind"], g["grader"], f"{g['k']}/{g['n']}", fmt_pct(g["rate"]),
             f"[{fmt_pct(g['lo'])}, {fmt_pct(g['hi'])}]", fmt_usd(g["per_solved_usd"]), fmt_usd(g["per_episode_usd"]),
             g["denied"]] for a, g in ctx["holdout"].items()]
    section("holdout", "2. Holdout comparison", _table(["Genome", "Split", "Grader", "Passes", "Rate", "95% Wilson",
                                                         "USD per solved", "USD per episode", "Denied"], rows))
    diff_html = []
    for kind, text in diff_lines(ctx["promoted"], width=140, max_lines_per_value=400):
        cls = {"header": "hdr", "meta": "meta", "minus": "minus", "plus": "plus"}.get(kind, "")
        diff_html.append(f"<span class='{cls}'>{html.escape(text)}</span>")
    body = (f"<pre class='diff'>{chr(10).join(diff_html)}</pre>" if ctx["promoted"]
            else "<p class='sub'>No promoted edits yet.</p>")
    section("learned", "3. What the harness learned (full text below)", body)
    rows = [[display_name(r)] + [v for v in row.values()] for r, row in ctx["ledger"].items()]
    hdr = ["Arm"] + (list(next(iter(ctx["ledger"].values())).keys()) if ctx["ledger"] else [])
    section("gate_ledger", "4. Gate ledger", _table(hdr, rows))
    rt = ctx["red_team"]
    rows = [[a, g["mode"], f"{g['k']}/{g['n']}", g["violating_episodes"], ", ".join(g["policies"]) or "none",
             g["denied"]] for a, g in rt["arms"].items()]
    rows2 = [[a, "gated" if g["gated"] else "ungated", g["decision"], ", ".join(g["policies"]) or "none", g["reason"]]
             for a, g in rt["gate"].items()]
    tl = ctx.get("timeline") or {}
    den = "".join(f"<li><code>{html.escape(str(d.get('kind')))} {html.escape(str(d.get('path')))}</code> denied by "
                  f"{html.escape(', '.join(d['policies']))}; tool said: <code>{html.escape(str(d.get('message', '')))}</code>"
                  f"</li>" for d in tl.get("denied", []))
    section("red_team", "5. RED-TEAM: arm A vs arm C (injected cheating genome, not a natural result)",
            _table(["Run", "Mode", "Workspace passes", "Violating episodes", "Policies", "Denied"], rows)
            + _table(["Arm", "Formal gate", "Decision", "Policies fired", "Reason"], rows2)
            + (f"<p class='sub'>Timeline episode {html.escape(str(tl.get('episode_id')))}: exact blocked actions</p>"
               f"<ul>{den}</ul>" if den else ""), cls="redteam")
    nat = ctx["natural"]
    rows = [[r["label"], ", ".join(r["variants"]), ", ".join(r["modes"]), r["episodes"], r["valid"], r["aborted"],
             f"{r['cheat_pass']['k']}/{r['cheat_pass']['n']}", fmt_pct(r["cheat_pass"]["hi"]),
             f"{r['file_attempt']['k']}/{r['file_attempt']['n']}"] for r in nat["rows"] + ([nat["pooled"]] if nat["rows"] else [])]
    section("natural", "6. Natural cheating (GPT-5)", _table(["Run", "Variant", "Mode", "Episodes", "Valid",
                                                              "aborted_cost", "Cheat passes", "95% upper bound",
                                                              "File-level attempts"], rows))
    rows = [[r["name"], r["expect"], " ".join(r["ops"])] for r in ctx["coverage"]["rows"]]
    section("coverage", "7. Coverage matrix", _table(["Mechanism", "Policy", "Scripted ops"], rows))
    lat = ctx["latency"]
    rows = [[k, v] for k, v in lat["sources"].items()]
    section("latency", "8. Verifier latency", _table(["Episode source", "Episodes with latency"], rows))
    num_rows = "".join(f"<tr><td>{html.escape(n['label'])}</td><td>{html.escape(n['value'])}</td><td class='num'>"
                       f"{'' if n['n'] is None else n['n']}</td><td>{html.escape(n['ci'] or '')}</td><td><code>"
                       f"{html.escape(n['source'])}</code></td></tr>" for n in nums)
    srcs = "".join(f"<li>{html.escape(k)}: <code>{html.escape(v)}</code></li>" for k, v in S.items())
    nav = "".join(f"<a href='#{k}'>{i + 1}</a>" for i, k in enumerate(
        ["improvement", "holdout", "learned", "gate_ledger", "red_team", "natural", "coverage", "latency"]))
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Proofread final results</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head>
<body><div class="viz-root"><h1>Proofread: final results</h1>
<div class="sub">Generated {html.escape(meta['generated_utc'])} from stored data. Figures: <nav style="display:inline">{nav}</nav></div>
<div class="banner">{html.escape(PROVISIONAL)}. All numbers are recomputed at build time; panels marked NO DATA YET had no
stored source when this page was built.</div>
<h2>Headline numbers</h2><table><tr><th>Headline</th><th>Value</th><th>n</th><th>CI</th><th>Source</th></tr>{num_rows}</table>
{''.join(sec)}
<h2>Sources</h2><ul class="sub">{srcs}</ul></div></body></html>"""
    out.write_text(doc, encoding="utf-8")
