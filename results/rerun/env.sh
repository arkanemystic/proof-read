# Rerun (section 7) environment. Source before any rerun command.
export PROOFREAD_VERIFIER=lean
export PROOFREAD_SPEND_DB=data/rerun_spend.sqlite
export PROOFREAD_BUDGET_TOTAL_USD=25
export PROOFREAD_BUDGET_CAPS='{"rerun_R1": 10, "arm_A": 5, "arm_C": 5}'
export PROOFREAD_EPISODE_COST_CAP_USD=0.20
export PROOFREAD_CONTEXT_TOKENS=24000
export PROOFREAD_NO_TEMPERATURE=1
export PROOFREAD_REASONING_EFFORT_FILE=results/rerun/reasoning_effort.txt
