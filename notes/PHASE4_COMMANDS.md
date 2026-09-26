# Phase IV commands (to rerun the pre-registered plan once model access is funded, B-003)

All commands run from the repo root with `PROOFREAD_VERIFIER=lean` (D-013). Each is resumable:
rerun the same command after a crash. Each process holds at most its `--concurrency` sandboxes;
keep the sum at or below 12.

Budget reality check (from the Phase III arm smoke): Sonnet 5 on hard LCB tasks costs up to about
1 USD per episode at 30 turns. The planned 3 generations x 3 candidates x 20 training tasks is about
240 episodes per arm, which exceeds the 35 USD arm cap. Expect the caps to stop runs early unless
max_turns or the training split shrinks.

```
# E1 model selection (all 12 slots, cap 10 USD; resumes the partial run, D-017)
PROOFREAD_VERIFIER=lean uv run python -m baselines.selection run --concurrency 12 --deadline ISO
# selected model -> results/selected_model.json ; SEL=$(jq -r .selected results/selected_model.json)

# E2 / E3 arms, concurrently, 4 slots each
PROOFREAD_VERIFIER=lean uv run python -m proofread.evolve.orchestrator --arm A --db data/proofread.sqlite \
  --generations 3 --candidates 3 --model $SEL --concurrency 4 --run-id armA --deadline ISO
PROOFREAD_VERIFIER=lean uv run python -m proofread.evolve.orchestrator --arm C --db data/proofread.sqlite \
  --generations 3 --candidates 3 --model $SEL --concurrency 4 --run-id armC --deadline ISO

# E4 baselines, 4 slots, cap 25 USD (interleaved plan, D-016)
PROOFREAD_VERIFIER=lean uv run python -m baselines.runner --plan e4 --selected-json results/selected_model.json \
  --concurrency 4 --deadline ISO

# Final champions on both holdouts, enforce mode
PROOFREAD_VERIFIER=lean uv run python scripts/champion_holdout.py --arm A --run-id armA --deadline ISO
PROOFREAD_VERIFIER=lean uv run python scripts/champion_holdout.py --arm C --run-id armC --deadline ISO

# Analysis
uv run python -m proofread.analysis.report --db data/proofread.sqlite --spend data/spend.sqlite --out results/
uv run python -m proofread.analysis.morning_report --root .
```
