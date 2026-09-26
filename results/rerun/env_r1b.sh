# Extension (section 8) environment for R1b and RT. Source before any R1b/RT command.
export PROOFREAD_VERIFIER=lean
export PROOFREAD_SPEND_DB=data/rerun_spend.sqlite
export PROOFREAD_BUDGET_TOTAL_USD=40
export PROOFREAD_BUDGET_CAPS='{"rerun_R1": 10, "rerun_R1b": 15, "rerun_RT": 8, "arm_A": 5, "arm_C": 5}'
export PROOFREAD_EPISODE_COST_CAP_USD=0.40
export PROOFREAD_CONTEXT_TOKENS=24000
export PROOFREAD_NO_TEMPERATURE=1
export PROOFREAD_REASONING_EFFORT_FILE=results/rerun/reasoning_effort_r1b.txt
export PROOFREAD_FEEDBACK_ROUNDS=3
export PROOFREAD_TURNS_PER_ROUND=8
export PROOFREAD_FEEDBACK_CHARS=2000
