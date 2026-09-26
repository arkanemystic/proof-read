# BLOCKED

(none yet)

## B-001 Anthropic API keys unusable (2026-09-26T08:42Z)
PROPOSER_API_KEY and BASELINE_API_KEY return HTTP 400 on every request: "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header". /v1/models fails the same way; workspace listing is 403. Repro: `uv run pytest tests/w4/test_w4_network.py -m network -rs`, or POST /v1/messages with either key.
Fix for Dev: add PROPOSER_WORKSPACE_ID / BASELINE_WORKSPACE_ID (or ANTHROPIC_WORKSPACE_ID) to .env; the client sends the header and native routing resumes automatically.
Fallback applied: see D-010.
