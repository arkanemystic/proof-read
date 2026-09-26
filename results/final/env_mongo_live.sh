# Section 10c live loop on Atlas: IMP-C settings plus the mongo_live budget key (1 USD cap).
source results/final/env_imp.sh
export PROOFREAD_BUDGET_CAPS='{"arm_C": 11, "arm_A": 9, "arm_C_noret": 5, "imp_holdouts": 8, "imp_select": 1, "rerun_RT": 9, "imp_replication": 3, "mongo_live": 1}'
unset PROOFREAD_STORE_BACKEND  # Atlas (MONGODB_URI from .env) is the only store for this run
export MONGODB_DB=proofread_runs
