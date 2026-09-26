"""Markdown tables for RERUN_REPORT.md from results/rerun/summary.json and the rerun spend ledger."""
import json, os, sqlite3
from collections import defaultdict

S = json.load(open("results/rerun/summary.json"))
ORDER = [("R1obs", "observe, effort medium"), ("R1obs_high", "observe, effort high"), ("R1enf", "enforce, effort high")]
rows = ["| Run | Episodes | Valid | aborted_cost | Workspace pass (cheat) valid, 95% Wilson | File-level attempt valid | Any violation incl. aborted | Denied actions | USD/episode | Mean turns |",
        "|---|---|---|---|---|---|---|---|---|---|"]
for a, label in ORDER:
    s = S.get(a)
    if not s:
        rows.append(f"| {a} ({label}) | 0 | not run | | | | | | | |"); continue
    rows.append(f"| {a} ({label}) | {s['episodes']} | {s['valid']} | {s['aborted_cost']} | {s['passed_workspace']} | "
                f"{s['file_level_attempt']} | {s['incl_aborted_any_violation']} | {s['denied_actions']} | "
                f"{s['cost_per_episode_usd']:.3f} | {s['mean_turns']} |")
print("\n".join(rows)); print()
print("| Run | test_edit | skip | config_tamper | other_violation | in_solution |")
print("|---|---|---|---|---|---|")
for a, label in ORDER:
    s = S.get(a)
    if s:
        m = s["mechanism_of_passes"]
        print(f"| {a} | " + " | ".join(m[k] for k in ("test_edit", "skip", "config_tamper", "other_violation", "in_solution")) + " |")
print()
c = sqlite3.connect(os.environ.get("PROOFREAD_SPEND_DB", "data/rerun_spend.sqlite"))
print("| Budget key | Model | Calls | Input tokens | Output tokens | USD |"); print("|---|---|---|---|---|---|")
for k, m, n, i, o, u in c.execute("SELECT budget_key, model, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) FROM calls GROUP BY budget_key, model"):
    print(f"| {k} | {m} | {n} | {i} | {o} | {u:.2f} |")
