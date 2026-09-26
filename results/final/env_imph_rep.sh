# Section 10 holdout replication environment (same settings as seed-0 IMPH runs plus the replication cap).
source results/final/env_imp.sh
export PROOFREAD_BUDGET_CAPS='{"arm_C": 11, "arm_A": 9, "arm_C_noret": 5, "imp_holdouts": 8, "imp_select": 1, "rerun_RT": 9, "imp_replication": 3}'
export PROOFREAD_STORE_BACKEND=sqlite  # 10c: .env now has MONGODB_URI; replication stays on data/imp3.sqlite
