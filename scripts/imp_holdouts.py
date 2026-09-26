"""Section 9 final-champion holdouts (D-F05, D-F10): export each IMP arm's final champion genome and print
the launch commands. usage: uv run python scripts/imp_holdouts.py [--db data/imp2.sqlite]

Writes results/final/champion_<RUN>.json and results/final/holdout_plan.json. An arm whose final champion
is the initial genome (no promotion) reuses the IMPH_default capability episodes; the one-off cheat check
then runs once for the default genome (arm IMPH_default_cheat)."""

import argparse
import json

from scripts.champion_holdout import champion_genome
from proofread.store.sqlite_store import SqliteEventLog, SqliteStore

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="data/imp3.sqlite")
a = ap.parse_args()
store, log = SqliteStore(a.db), SqliteEventLog(a.db)
plan, seen = [], {}
for run, label in (("IMP_C", "C"), ("IMP_A", "A")):
    try:
        vid, g, _ = champion_genome(store, log, run)
    except SystemExit:
        continue
    path = f"results/final/champion_{run}.json"
    open(path, "w").write(g.model_dump_json(indent=2))
    is_default = g.version == 1
    key = "default" if is_default else g.content_hash()
    entry = {"run": run, "champion": vid, "version": g.version, "genome": path, "is_default": is_default}
    if key in seen:
        entry["reuses"] = seen[key]
    else:
        seen[key] = run
        entry["capability_arm"] = "IMPH_default" if is_default else f"IMPH_{label}"
        entry["cheat_arm"] = "IMPH_default_cheat" if is_default else f"IMPH_{label}_cheat"
    plan.append(entry)
json.dump(plan, open("results/final/holdout_plan.json", "w"), indent=2)
print(json.dumps(plan, indent=1))
