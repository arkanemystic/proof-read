#!/bin/bash
# Section 9 IMP arms + default-genome capability holdout (D-F10). Resumable: rerun to continue.
cd .; source results/final/env_imp.sh; mkdir -p logs/final
for arm in C A; do
  conc=$([ $arm = C ] && echo 6 || echo 4)
  nohup uv run python -m proofread.evolve.orchestrator --arm $arm --db data/imp3.sqlite --generations 3 --candidates 3 \
    --model $IMP_AGENT --proposer-model $IMP_PROPOSER --split imp_training --concurrency $conc --run-id IMP_$arm \
    --genome results/final/genome_imp_default.json --cost-rule --deadline 2026-09-26T16:22:00Z >> logs/final/IMP3_$arm.log 2>&1 &
done
nohup uv run python -m baselines.runner --plan single --genome results/final/genome_imp_default.json --split imp_capability_holdout \
  --mode enforce --pristine --arm IMPH_default --budget-key imp_holdouts --store data/imp3.sqlite --model $IMP_AGENT \
  --concurrency 2 --deadline 2026-09-26T16:45:00Z >> logs/final/IMPH3_default.log 2>&1 &
