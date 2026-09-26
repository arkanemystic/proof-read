#!/bin/bash
# D-F07 headroom probe: 4 candidate inner models on the 10 selection_original tasks.
cd /home/dev/work/proofread; source results/final/env_imp.sh
for m in ${PROBE_MODELS:-qwen/qwen3-coder-30b-a3b-instruct openai/gpt-oss-20b mistralai/ministral-14b-2512 qwen/qwen3.5-9b}; do
  tag=$(echo $m | tr '/.' '__')
  nohup uv run python -m baselines.runner --plan single --genome results/final/genome_imp_default.json --split selection_original \
    --mode observe --arm IMPSEL_$tag --budget-key imp_select --store data/imp.sqlite --model $m \
    --concurrency 3 --deadline 2026-09-26T15:22:00Z > logs/final/IMPSEL_$tag.log 2>&1 &
done
