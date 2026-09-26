# Section 9 (final push) environment for IMP arms and holdouts. Source before any IMP command.
export PROOFREAD_VERIFIER=lean
export PROOFREAD_SPEND_DB=data/final_spend.sqlite
export PROOFREAD_BUDGET_TOTAL_USD=45
export PROOFREAD_BUDGET_CAPS='{"arm_C": 11, "arm_A": 9, "arm_C_noret": 5, "imp_holdouts": 8, "imp_select": 1, "rerun_RT": 9}'
export PROOFREAD_EPISODE_COST_CAP_USD=0.08
export PROOFREAD_CONTEXT_TOKENS=24000
unset PROOFREAD_FEEDBACK_ROUNDS PROOFREAD_TURNS_PER_ROUND PROOFREAD_FEEDBACK_CHARS PROOFREAD_REASONING_EFFORT_FILE PROOFREAD_NO_TEMPERATURE
export IMP_AGENT=qwen/qwen3.7-flash
export IMP_PROPOSER=deepseek/deepseek-v4-pro
export PROOFREAD_AGENT_REASONING_EFFORT=none
