#!/bin/bash
# Launch final-champion holdouts from results/final/holdout_plan.json (enforce mode, deadline 16:45Z).
# usage: bash scripts/imp_holdouts_launch.sh CONC_PER_PROCESS
cd .; source results/final/env_imp.sh; C=${1:-3}
uv run python -m scripts.imp_holdouts > /dev/null
uv run python - <<'PY' > /tmp/imp_holdout_cmds.txt
import json
for e in json.load(open("results/final/holdout_plan.json")):
    if "reuses" in e:
        continue
    if e["capability_arm"] != "IMPH_default":
        print(f'{e["genome"]} imp_capability_holdout {e["capability_arm"]} --pristine')
    print(f'{e["genome"]} cheat_holdout {e["cheat_arm"]} --variant=oneoff --limit=15')
PY
while read genome split arm extra1 extra2; do
  nohup uv run python -m baselines.runner --plan single --genome $genome --split $split --mode enforce --arm $arm \
    --budget-key imp_holdouts --store data/imp3.sqlite --model $IMP_AGENT --concurrency $C \
    --deadline 2026-09-26T16:45:00Z $extra1 $extra2 >> logs/final/$arm.log 2>&1 &
  echo "launched $arm ($split)"
done < /tmp/imp_holdout_cmds.txt
