"""Final visuals (section 9): every figure function, NUMBERS.md/numbers.json and the dashboard, on synthetic
fixtures and on missing data."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from final_fixtures import RT_GATE, make_imp_db, make_rerun_db, make_spend_db
from proofread.analysis.final import compute as C
from proofread.analysis.final import render as R
from proofread.analysis.final.build import compute_all, main, render_all
from proofread.analysis.final.io import load_store
from proofread.analysis.final.report import build_numbers

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    traces = tmp_path / "traces"
    traces.mkdir()
    imp = make_imp_db(tmp_path / "imp.sqlite")
    rer = make_rerun_db(tmp_path / "rerun.sqlite", traces)
    spend = make_spend_db(tmp_path / "spend.sqlite")
    gate = tmp_path / "rt_gate.json"
    gate.write_text(json.dumps(RT_GATE))
    return {"imp": str(imp), "rerun": str(rer), "spend": str(spend), "gate": str(gate), "out": str(tmp_path / "out")}


@pytest.fixture()
def ctx(env):
    return compute_all(env["imp"], env["rerun"], env["spend"], env["gate"])


def _png(p: Path) -> None:
    assert p.exists() and p.read_bytes()[:8] == PNG and p.stat().st_size > 2000


# ------------------------------------------------------------------ compute
def test_improvement_curve(ctx):
    assert ctx["imp_run_ids"] == ["IMP_C", "IMP_A"]
    c = ctx["curves"]["IMP_C"]
    assert [p["generation"] for p in c] == [0, 1, 2]
    assert [(p["k"], p["n"]) for p in c] == [(2, 6), (3, 6), (4, 6)]
    assert c[0]["per_solved_usd"] == pytest.approx(6 * 0.02 / 2)
    assert c[0]["promoted_after"] == "IMP_C-g0-c0" and not c[0]["partial"]
    assert 0 <= c[0]["lo"] <= c[0]["rate"] <= c[0]["hi"] <= 1


def test_gate_ledger_and_promoted(ctx):
    assert ctx["ledger"]["IMP_C"] == {"invalid": 1, "rejected_formal": 1, "rejected_empirical": 2, "promoted": 1,
                                      "pending": 0}
    pe = ctx["promoted"]
    assert len(pe) == 2 and pe[0]["run_id"] == "IMP_C"
    op = pe[0]["ops"][0]
    assert op["old"] == "Run the tests." and op["new"].startswith("Write a first")
    assert pe[0]["cost_stats"] == {"cost_saving_frac": 0.2}
    lines = R.diff_lines(pe)
    kinds = [k for k, _ in lines]
    assert "minus" in kinds and "plus" in kinds and lines[0][0] == "header"


def test_holdout(ctx):
    h = ctx["holdout"]
    assert h["IMPH_default"]["grader"] == "pristine" and (h["IMPH_default"]["k"], h["IMPH_default"]["n"]) == (4, 10)
    assert h["IMPH_C_cheat"]["kind"] == "cheat" and h["IMPH_C_cheat"]["grader"] == "workspace"
    d = ctx["holdout_diffs"]["IMPH_C"]
    assert d["n_pairs"] == 10 and d["mean"] == pytest.approx(0.2) and d["lo"] <= 0.2 <= d["hi"]


def test_holdout_pristine_fallback():
    eps = [{"arm": "IMPH_default", "task_id": "t", "passed_workspace": True, "passed_pristine": None}]
    g = C.holdout_groups(eps)["IMPH_default"]
    assert g["grader"] == "workspace" and g["note"] and g["k"] == 1


def test_red_team_and_timeline(ctx):
    rt = ctx["red_team"]
    assert rt["gate"]["RT_C"]["decision"] == "rejected_formal" and rt["gate"]["RT_C"]["policies"] == ["CODE-TEST-001"]
    assert rt["arms"]["RT_C_enf"]["denied"] == 1
    tl = ctx["timeline"]
    assert tl["episode_id"] == "RT_C_enf-x"
    d = tl["denied"][0]
    assert d["policies"] == ["CODE-TEST-001"] and d["path"] == "/workspace/test.py" and d["turn"] == 2
    assert d["tool"] == "apply_patch" and d["message"].startswith("DENIED")


def test_natural_cheating_excludes_rt_and_aborted(ctx):
    nat = ctx["natural"]
    assert [r["arm"] for r in nat["rows"]] == ["R1obs", "R1b_obs", "R1b_conf_obs"]
    r1 = nat["rows"][0]
    assert r1["episodes"] == 10 and r1["valid"] == 9 and r1["aborted"] == 1
    p = nat["pooled"]
    assert (p["cheat_pass"]["k"], p["cheat_pass"]["n"]) == (1, 23)
    assert p["cheat_pass"]["hi"] < 0.25


def test_wilson_zero_upper_bound():
    b = C.rate_block(0, 64)
    assert b["rate"] == 0 and b["lo"] == 0 and b["hi"] == pytest.approx(0.0565, abs=1e-3)
    assert math.isnan(C.rate_block(0, 0)["hi"])


def test_coverage_and_unit_check(ctx):
    cov = ctx["coverage"]
    assert cov["n"] == 38 and cov["mapped"] == 38
    assert sum(cov["by_policy"].values()) == 38
    u = ctx["unit_check"]
    assert u["n"] > 0 and u["flagged"] == u["n"]


def test_latency(ctx):
    lat = ctx["latency"]
    assert lat["n_actions"] > 0 and lat["p50"] in lat["values"] and lat["p95"] in lat["values"]
    assert lat["ep_frac_p50"] == pytest.approx(61.4 / 60000)


def test_spend(ctx):
    assert ctx["spend"]["arm_C"]["usd"] == pytest.approx(0.5)


# ------------------------------------------------------------------ figures (each function)
@pytest.mark.parametrize("key", ["improvement", "holdout", "learned", "gate_ledger", "red_team", "natural",
                                 "coverage", "latency"])
def test_each_figure_renders(ctx, tmp_path, key):
    figs = render_all(ctx, tmp_path / "figs")
    _png(figs[key])


def test_figures_render_with_empty_inputs(tmp_path):
    empty_lat = C.latency({})
    paths = [
        R.fig_improvement({}, tmp_path / "1.png", "s"),
        R.fig_holdout({}, {}, tmp_path / "2.png", "s"),
        R.fig_learned([], tmp_path / "3.png", "s"),
        R.fig_gate_ledger({}, tmp_path / "4.png", "s"),
        R.fig_red_team({"arms": {}, "gate": {}}, None, tmp_path / "5.png", "s"),
        R.fig_natural({"rows": [], "pooled": None}, tmp_path / "6.png", "s"),
        R.fig_coverage({"n": 0, "rows": []}, tmp_path / "7.png", "s"),
        R.fig_latency(empty_lat, tmp_path / "8.png", "s"),
    ]
    for p in paths:
        _png(p)


# ------------------------------------------------------------------ NUMBERS and dashboard
def test_numbers_trace_to_sources(ctx):
    nums = build_numbers(ctx)
    ids = {n["id"] for n in nums}
    for must in ("holdout.IMPH_C.rate", "holdout.IMPH_C.delta_vs_default", "imp.IMP_C.ledger", "rt.RT_C.decision",
                 "natural.pooled", "coverage.golden", "latency.per_action", "imp.spend_total"):
        assert must in ids, must
    for n in nums:
        assert n["source"], n
    pooled = next(n for n in nums if n["id"] == "natural.pooled")
    assert pooled["value"].startswith("1/23") and "Wilson" in pooled["ci"]


def test_main_end_to_end(env):
    assert main(["--imp-db", env["imp"], "--rerun-db", env["rerun"], "--spend-db", env["spend"],
                 "--rt-gate", env["gate"], "--out", env["out"]]) == 0
    out = Path(env["out"])
    md = (out / "NUMBERS.md").read_text()
    assert "| Headline | Value | n | CI | Source |" in md and "PROVISIONAL-NO-BIJECT" in md
    js = json.loads((out / "numbers.json").read_text())
    assert js["numbers"] and js["meta"]["imp_run_ids"] == ["IMP_C", "IMP_A"]
    html = (out / "dashboard.html").read_text()
    assert html.count("data:image/png;base64,") == 8
    assert "src='http" not in html and 'src="http' not in html and "<link" not in html
    assert "RED-TEAM" in html
    assert len(list((out / "figures").glob("*.png"))) == 8
    for text in (md, html):
        assert "\u2014" not in text  # no em dashes


def test_main_with_missing_sources(tmp_path):
    out = tmp_path / "out"
    assert main(["--imp-db", str(tmp_path / "nope.sqlite"), "--rerun-db", str(tmp_path / "nope2.sqlite"),
                 "--spend-db", str(tmp_path / "nope3.sqlite"), "--rt-gate", str(tmp_path / "x.json"),
                 "--out", str(out)]) == 0
    md = (out / "NUMBERS.md").read_text()
    assert "no data yet" in md
    assert (out / "dashboard.html").exists() and len(list((out / "figures").glob("*.png"))) == 8


def test_loader_is_read_only(env):
    before = Path(env["imp"]).read_bytes()
    s = load_store(env["imp"])
    assert s.coll("episodes") and s.events
    assert Path(env["imp"]).read_bytes() == before
    con = sqlite3.connect(f"file:{env['imp']}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO docs VALUES ('x','y','{}',0)")


def test_partial_flags():
    eps = [{"arm": "IMPH_default", "task_id": f"t{i}", "split": "s", "passed_pristine": True} for i in range(3)]
    g = C.holdout_groups(eps)
    C.mark_planned(g, lambda split: 40 if split == "s" else 0)
    assert g["IMPH_default"]["partial"] and g["IMPH_default"]["expected_n"] == 40
    nums = {n["id"]: n for n in build_numbers({**_empty_ctx(), "holdout": g})}
    assert "partial: 3 of 40" in nums["holdout.IMPH_default.rate"]["label"]


def _empty_ctx():
    return compute_all("/nonexistent/a.sqlite", "/nonexistent/b.sqlite", "/nonexistent/c.sqlite", "/nonexistent/d.json")
