# BLOCKED

(none yet)

## B-001 Anthropic API keys unusable (2026-09-26T08:42Z)
PROPOSER_API_KEY and BASELINE_API_KEY return HTTP 400 on every request: "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header". /v1/models fails the same way; workspace listing is 403. Repro: `uv run pytest tests/w4/test_w4_network.py -m network -rs`, or POST /v1/messages with either key.
Fix for Dev: add PROPOSER_WORKSPACE_ID / BASELINE_WORKSPACE_ID (or ANTHROPIC_WORKSPACE_ID) to .env; the client sends the header and native routing resumes automatically.
Fallback applied: see D-010.

## B-002 biject-api cannot run locally or host our policies (2026-09-26T08:47Z)
(1) A locally built biject-api image exits at startup: its worker verifies compiled .olean files against an Ed25519 public key hardcoded in its own worker.py, so policies signed with fresh local keys fail ("SECURITY: olean signature check failed"); biject docs list this as a known limitation. (2) The CI-signed demo image pull returns unauthorized. (3) Design mismatch: biject only admits typed inputs (enum/int/bool or three string shapes up to 104 bytes) into the Lean kernel, while our policies must judge raw paths and raw added source lines. Repro steps: proofread/verify/BIJECT_INTERFACE.md.
Fallback applied: cut-list item 2 (see D-013).

## B-003 OpenRouter credits exhausted (2026-09-26T09:33Z)
AGENT_API_KEY and OPENROUTER_API_KEY belong to the same OpenRouter account, which had 30 USD of credits in total (the sprint plan assumed 150 USD). By 09:33Z usage was 30.46 USD, matching our ledger (30.69 USD): selection 4.40, smoke 2.89, Phase III arm smoke 23.4 (arm_A 10.2, arm_C 13.2; hard tasks run up to 30 turns with growing context, about 1 USD per Sonnet 5 episode). Every paid request now returns HTTP 402 "requires more credits". Repro: GET https://openrouter.ai/api/v1/credits with either key -> total_credits 30, total_usage >= 30.
Native Anthropic keys are also unusable (B-001), so no paid inner-agent or baseline model is reachable.
Fix for Dev: add OpenRouter credits (or set the Anthropic workspace IDs from B-001), then rerun E1 to E4 with the commands in notes/PHASE4_COMMANDS.md.
Fallback applied: D-017.
