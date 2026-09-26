# BLOCKED

(none yet)

## B-001 Anthropic API keys unusable (2026-09-26T08:42Z)
PROPOSER_API_KEY and BASELINE_API_KEY return HTTP 400 on every request: "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header". /v1/models fails the same way; workspace listing is 403. Repro: `uv run pytest tests/w4/test_w4_network.py -m network -rs`, or POST /v1/messages with either key.
Fix for Dev: add PROPOSER_WORKSPACE_ID / BASELINE_WORKSPACE_ID (or ANTHROPIC_WORKSPACE_ID) to .env; the client sends the header and native routing resumes automatically.
Fallback applied: see D-010.

## B-002 biject-api cannot run locally or host our policies (2026-09-26T08:47Z)
(1) A locally built biject-api image exits at startup: its worker verifies compiled .olean files against an Ed25519 public key hardcoded in its own worker.py, so policies signed with fresh local keys fail ("SECURITY: olean signature check failed"); biject docs list this as a known limitation. (2) The CI-signed demo image pull returns unauthorized. (3) Design mismatch: biject only admits typed inputs (enum/int/bool or three string shapes up to 104 bytes) into the Lean kernel, while our policies must judge raw paths and raw added source lines. Repro steps: proofread/verify/BIJECT_INTERFACE.md.
Fallback applied: cut-list item 2 (see D-013).
