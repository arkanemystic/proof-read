# PROGRESS (4.5h parallel sprint)

T0 = 2026-09-26T08:19:28Z  (DEADLINE_UTC 2026-09-26T12:49:15Z; T0+4:20 fits, no compression)

| Boundary | UTC |
|---|---|
| Phase I end (contracts) | 2026-09-26T08:44:28Z |
| Phase II end (parallel build) | 2026-09-26T10:04:28Z |
| Phase III end (integration, smoke) | 2026-09-26T10:34:28Z |
| E1 target end (~20 min) | 2026-09-26T10:54:28Z |
| Final champion holdouts start | 2026-09-26T11:54:28Z |
| Stop launching episodes | 2026-09-26T12:04:28Z |
| Phase IV end (experiments) | 2026-09-26T12:09:28Z |
| Phase V end (report, DONE) | 2026-09-26T12:39:28Z |

| Item | Status | Tests | Commit |
|---|---|---|---|
| P1 contracts | done | 6 passed (make test) | (this commit) |
| P2 launch W1..W7 | launched 08:23Z in one message | | |
| W2 benchmarks+graders | done | tests/w2 32 passed 1 skipped (docker e2e pending W1) | 0fc2863 |
| W6 baselines+selection | done | baselines/tests 25 passed offline (+1 network) | a683dd5 |
| W7 analysis | done | tests/w7 14 passed | e8abd5f |
| W5 store+evolve | done | tests/w5 33 passed | a8f6a28 |
| W4 agent+genome+models | done | tests/w4 68 passed incl network | a158846 |
| W1 sandbox+capture | done | golden_cheats+benign 152 passed (61 docker), 38 cheats flagged, 16 benign clean | 62a59c1 |
| W3 verifier | done (biject blocked B-002, Lean verifier default, CUT 2) | differential+failclosed 58 passed 1 skipped | 305f7dc |
| P3a make test | done | 391 passed 1 skipped | |
| P3b smoke 3 orig + 3 impossible x observe/enforce (sonnet-5) | done | 12/12 episodes ran; originals 6/6 pass ws+pristine; impossible 0/6 pass; 1 true-positive CODE-NET-001 (pip install); $1.85 | 1159c94 |
| P3c arm smoke (1 gen, 2 cand, 5 tasks, A and C, sonnet-5) | done (stopped early by B-003) | both arms ran champion eval, proposer, screening, full eval and gate: A c0 rejected_empirical (delta +0.0), C c0 rejected_empirical (delta -20); c1 not finished when credits ran out; $23.4 | 9543fbb |
| E1 model selection | PARTIAL, blocked B-003 | 19+ paid episodes stored (0 cheats); no selection made; free-model fallback D-017/D-018 picked nemotron-3-ultra:free | |
| P4 fallback arms freeA/freeC (nemotron-3-ultra:free, 1 gen x 2 cand x 8 tasks) | done 11:11Z | A: champion 6/8, c0 7/8 and c1 7/8 both rejected_empirical (delta +12.5, lb 0.0); C: champion 7/8, c0 8/8 rejected_empirical (delta +12.5, lb 0.0), c1 7/8 rejected (delta 0); final champion v1 in both; 1 fail-closed infra violation (D-019) | |
| P4 fallback baseline (default genome, 16 cheat holdout, observe) | done | 0/16 cheat, 0 violations (D-021) | |
| P4 final-champion holdouts (8 cheat + 8 capability, enforce) | done 11:31Z | A cheat 0/8, pristine 6/8; C cheat 0/8, pristine 8/8 (both v1) | |
| E2/E3/E4 pre-registered | NOT RUN (B-003) | see notes/PHASE4_COMMANDS.md | |
| P5 analysis + report | done 11:48Z | final make test: 392 passed, 1 skipped, 1 failed (tests/w4/test_w4_network.py::test_openrouter_agent_call, live paid call, HTTP 402 from B-003); tests/w7 16 passed | 1daff7c+ |
