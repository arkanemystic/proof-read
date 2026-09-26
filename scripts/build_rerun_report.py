"""Assemble RERUN_REPORT.md from notes/rerun_report_head.md plus generated tables, RT gate and spend."""
import json
import sqlite3
import subprocess
from pathlib import Path

subprocess.run(["uv", "run", "python", "scripts/rerun_report.py"], check=True, capture_output=True)
subprocess.run(["uv", "run", "python", "scripts/rt_gate.py"], check=True, capture_output=True)
tables = subprocess.run(["uv", "run", "python", "scripts/rerun_tables.py"], check=True, capture_output=True,
                        text=True).stdout
gate = json.loads(Path("results/rerun/rt_gate.json").read_text())
S = json.loads(Path("results/rerun/summary.json").read_text())
rows = ["| Arm | Formal gate | Candidate eps (valid) | Champion eps | Candidate vs champion pass | Delta, 80% LB | Decision | Policies fired |",
        "|---|---|---|---|---|---|---|---|"]
for arm, g in gate.items():
    st = g["stats"]
    pols = sorted({p for v in g["violations"] for p in v["violations"]}) or ["none"]
    rows.append(f"| {arm} | {'on' if g['gated'] else 'off'} | {g['n_candidate']} ({g['n_candidate_valid']}) | "
                f"{g['n_champion']} | {st['candidate_rate']:.3f} vs {st['champion_rate']:.3f} | "
                f"{st['delta_points']:+.1f} pts, {st['lower_bound_points']:+.1f} | **{g['decision']}** | {', '.join(pols)} |")
c = sqlite3.connect("data/rerun_spend.sqlite")
tot = c.execute("SELECT SUM(cost_usd) FROM calls").fetchone()[0] or 0.0
per_key = c.execute("SELECT budget_key, SUM(cost_usd) FROM calls GROUP BY budget_key").fetchall()
cost = [f"Total rerun spend (all GPT-5, AGENT_API_KEY): **{tot:.2f} USD** of the 40 USD cap "
        f"({', '.join(f'{k} {v:.2f}' for k, v in per_key)}; caps R1 10, R1b 15, RT 8).", "",
        "| Run | Episodes | USD/episode | aborted_cost |", "|---|---|---|---|"]
for a, s in S.items():
    cost.append(f"| {a} | {s['episodes']} | {s['cost_per_episode_usd']:.3f} | {s['aborted_cost']} |")
head = Path("notes/rerun_report_head.md").read_text()
out = (head.replace("<!-- TABLES -->", tables.strip()).replace("<!-- RTGATE -->", "\n".join(rows))
       .replace("<!-- COST -->", "\n".join(cost)))
Path("RERUN_REPORT.md").write_text(out)
print("wrote RERUN_REPORT.md")
