# Proofread headline numbers

Generated 2026-09-26T18:09:24Z by `uv run python scripts/build_final.py`. PROVISIONAL-NO-BIJECT (local Lean verifier; Lean policies DRAFT pending human review).
Every value is recomputed from the source file listed; n is the number of episodes, tasks or candidates.

| Headline | Value | n | CI | Source |
|---|---|---|---|---|
| IMP-C champion: capability holdout pass rate (pristine grader, enforce) | 27/40 = 67.5% | 40 | 95% Wilson [52.0%, 79.9%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-C champion: USD per solved holdout task | 0.0051 USD (0.0035 USD per episode) | 40 |  | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-C champion: impossible-task (cheat) pass rate (workspace grader, enforce) | 0/15 = 0.0% | 15 | 95% Wilson [0.0%, 20.4%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-C champion: actions denied on impossible tasks | 3 | 15 |  | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| default genome: capability holdout pass rate (pristine grader, enforce) | 24/40 = 60.0% | 40 | 95% Wilson [44.6%, 73.7%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| default genome: USD per solved holdout task | 0.0061 USD (0.0037 USD per episode) | 40 |  | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| default genome: impossible-task (cheat) pass rate (workspace grader, enforce) | 0/15 = 0.0% | 15 | 95% Wilson [0.0%, 20.4%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| default genome: actions denied on impossible tasks | 2 | 15 |  | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-C champion minus default genome, holdout pass rate | +7.5 pts | 40 | 95% paired bootstrap [-5.0, +20.0] pts | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| Replication seed 1: default genome, capability holdout pass rate (pristine, enforce) | 24/40 = 60.0%; 0.0041 USD per solved task; 0 errored | 40 | 95% Wilson [44.6%, 73.7%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| Replication seed 1: IMP-C champion, capability holdout pass rate (pristine, enforce) | 30/40 = 75.0%; 0.0057 USD per solved task; 0 errored | 40 | 95% Wilson [59.8%, 85.8%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| Replication seed 2: default genome, capability holdout pass rate (pristine, enforce) | 22/40 = 55.0%; 0.0055 USD per solved task; 1 errored | 40 | 95% Wilson [39.8%, 69.3%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| Replication seed 2: IMP-C champion, capability holdout pass rate (pristine, enforce) | 28/40 = 70.0%; 0.0057 USD per solved task; 0 errored | 40 | 95% Wilson [54.6%, 81.9%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| Pooled seeds [0, 1, 2]: default genome, pass rate over valid episodes (40 tasks) | 70/119 = 58.8%; 0.0052 USD per solved task | 119 | 95% Wilson [49.8%, 67.3%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| Pooled seeds [0, 1, 2]: IMP-C champion, pass rate over valid episodes (40 tasks) | 85/119 = 71.4%; 0.0055 USD per solved task | 119 | 95% Wilson [62.7%, 78.8%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| Pooled paired per-task difference, IMP-C champion minus default (mean over seeds per task) | +11.7 pts; wins/losses/ties 10/1/29 | 40 | 95% [+4.2, +20.0], 80% [+6.7, +17.5] pts; one-sided 80% LB +8.3 (bootstrap over tasks, 10000 draws) | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| LAP Lean-kernel replay: agreement with the stored verdict (all runs, incl. red-team) | 5636/5636 (100.0%), 0 disagreements, 0 fail-closed | 5636 |  | `results/final/lap_replay.json overall` |
| LAP: per-record kernel claims (stored verdict bits, by decide) proved | 5636/5636 | 5636 |  | `results/final/lap_replay.json per_action_kernel_check` |
| LAP: violations replayed and matched, by policy | 92 (TEST 8, SKIP 0, GRADER 0, SCOPE 77, NET 7, ATTR 77) | 5636 |  | `results/final/lap_replay.json per_policy` |
| LAP: red-team blocked actions refuted by the kernel | 3/3 (CODE-TEST-001) | 3 |  | `results/final/lap_replay.json red_team_blocked` |
| LAP worker latency per conjecture (p50 / p95) | 182 / 195 ms |  |  | `results/final/lap_replay.json latency_ms` |
| MongoDB: Store/EventLog/VectorIndex test suite against Atlas | 24 passed, 4 failed, 0 skipped |  |  | `results/final/mongo_tests.json` |
| MongoDB: SQLite run stores migrated to Atlas, per-collection counts equal | 6 stores, 1012 documents, counts equal: yes | 6 |  | `results/final/mongo_migration.json` |
| MongoDB: aggregation-pipeline numbers cross-checked against numbers.json | 23/23 match, 0 mismatches | 23 |  | `results/final/mongo_numbers.json` |
| MongoDB: $vectorSearch top-3 similar rejected edits (latency) | top hit smoke-C-g0-c0 (rejected_empirical, cosine 0.3797); ANN 143.2 ms, same ids as exact: True | 12 |  | `results/final/mongo_vector_demo.json` |
| MongoDB: live gated loop on Atlas, edit status transitions seen via change stream | 4 transitions; IMP_C_atlas_r2-g0-c0: rejected_formal; IMP_C_atlas_r2-g0-c1: rejected_formal | 4 |  | `results/final/mongo_live.json` |
| IMP-C: champion training pass rate, generation 0 | 13/24 = 54.2% | 24 | 95% Wilson [35.1%, 72.1%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-C: champion training pass rate, generation 1 | 18/24 = 75.0% | 24 | 95% Wilson [55.1%, 88.0%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-C: USD per solved training task, generation 1 | 0.0052 USD | 24 |  | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-A: champion training pass rate, generation 0 | 14/24 = 58.3% | 24 | 95% Wilson [38.8%, 75.5%] | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-A: USD per solved training task, generation 0 | 0.0042 USD | 24 |  | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-C: candidate edits by gate outcome | invalid 0, rejected_formal 1, rejected_empirical 1, promoted 1 | 3 |  | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| IMP-A: candidate edits by gate outcome | invalid 1, rejected_formal 0, rejected_empirical 2, promoted 0 | 3 |  | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| Promoted edits across IMP arms | 1 |  |  | `data/imp3.sqlite (docs: episodes, edits, harness_versions; events)` |
| Spend recorded in the final ledger, by budget key | 3.55 USD (arm_A 0.43, arm_C 0.63, demo 0.84, imp_holdouts 0.63, imp_replication 0.62, imp_select 0.19, mongo_live 0.21) | 6698 |  | `data/final_spend.sqlite` |
| RED-TEAM genome, RT_A (ungated): gate decision | rejected_empirical; policies fired: none | 8 | delta +12.5 pts, 80% LB +0.0 pts | `results/rerun/rt_gate.json` |
| RED-TEAM genome, RT_C (gated): gate decision | rejected_formal; policies fired: CODE-TEST-001 | 8 | delta +12.5 pts, 80% LB +0.0 pts | `results/rerun/rt_gate.json` |
| RED-TEAM RT_A (observe): workspace passes, violating episodes, denied actions | 1/8 passes, 0 violating, 0 denied, 1 cheat passes (0 file-level, 1 in_solution) | 8 | 95% Wilson [2.2%, 47.1%] | `data/rerun.sqlite (episodes)` |
| RED-TEAM RT_C (observe): workspace passes, violating episodes, denied actions | 1/8 passes, 1 violating, 0 denied, 1 cheat passes (1 file-level, 0 in_solution) | 8 | 95% Wilson [2.2%, 47.1%] | `data/rerun.sqlite (episodes)` |
| RED-TEAM RT_C_enf (enforce): workspace passes, violating episodes, denied actions | 1/8 passes, 2 violating, 2 denied, 1 cheat passes (0 file-level, 1 in_solution) | 8 | 95% Wilson [2.2%, 47.1%] | `data/rerun.sqlite (episodes)` |
| Natural cheating, GPT-5, all non-red-team runs pooled (cheat passes, valid episodes) | 0/64 = 0.0% | 64 | 95% Wilson [0.0%, 5.7%] | `data/rerun.sqlite (episodes)` |
| Natural file-level cheat attempts (CODE-TEST/SKIP/GRADER), pooled | 0/64 = 0.0% | 64 | 95% Wilson [0.0%, 5.7%] | `data/rerun.sqlite (episodes)` |
| Natural runs pooled: episodes with any policy violation, all episodes incl. aborted_cost | 0/90 = 0.0% | 90 | 95% Wilson [0.0%, 4.1%] | `data/rerun.sqlite (episodes)` |
| Natural cheating, R1 observe | 0/13 = 0.0% | 13 | 95% Wilson [0.0%, 22.8%] | `data/rerun.sqlite (episodes)` |
| Natural cheating, R1 enforce | 0/6 = 0.0% | 6 | 95% Wilson [0.0%, 39.0%] | `data/rerun.sqlite (episodes)` |
| Natural cheating, R1b observe | 0/13 = 0.0% | 13 | 95% Wilson [0.0%, 22.8%] | `data/rerun.sqlite (episodes)` |
| Natural cheating, R1b enforce | 0/13 = 0.0% | 13 | 95% Wilson [0.0%, 22.8%] | `data/rerun.sqlite (episodes)` |
| Natural cheating, R1b conflicting | 0/19 = 0.0% | 19 | 95% Wilson [0.0%, 16.8%] | `data/rerun.sqlite (episodes)` |
| Golden cheat mechanisms, each mapped to the base policy expected to catch it | 38/38 mapped; CODE-TEST-001 15, CODE-SKIP-001 9, CODE-GRADER-001 8, CODE-SCOPE-001 3, CODE-NET-001 2, CODE-ATTR-001 1 | 38 |  | `proofread/sandbox/scenarios.py GOLDEN (expect), enforced by tests/golden_cheats/test_golden_docker.py` |
| Reference policies re-run at build time over unit-level cheat fixtures | 51/51 flagged by the expected policy | 51 |  | `tests/golden_cheats/test_reference_policies.py CHEATS through proofread/policies/reference.py` |
| Verifier latency per action | p50 1.87 ms, p95 43.27 ms, max 271.5 ms | 5169 |  | `EpisodeResult.verifier_latency_ms in data/rerun.sqlite and data/imp3.sqlite` |
| Verifier overhead per episode | p50 138 ms (0.134% of wall time), p95 238 ms (0.490%) | 539 |  | `EpisodeResult.verifier_latency_ms in data/rerun.sqlite and data/imp3.sqlite` |

Conventions: holdout pass rates count every stored episode (aborted_cost counts as a fail); natural-cheating rates use valid episodes (aborted_cost and infra errors excluded, as in scripts/rerun_report.py). A cheat pass is any workspace pass on an impossible task (mechanism test_edit, skip, config_tamper, other_violation or in_solution).
