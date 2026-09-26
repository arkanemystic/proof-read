"""Optional real calls (tiny, < $0.01 each). Skipped when keys are absent."""

import shutil

import pytest
from dotenv import dotenv_values

from proofread.contracts import ChatMessage
from proofread.models import config
from proofread.models.client import make_client

pytestmark = pytest.mark.network
ENV = dotenv_values(config.ENV_PATH) if config.ENV_PATH.exists() else {}
MSG = [ChatMessage(role="user", content="Reply with the single word: pong")]


@pytest.mark.skipif(not ENV.get("AGENT_API_KEY"), reason="AGENT_API_KEY absent")
async def test_openrouter_agent_call(tmp_path):
    c = make_client("agent", config.SMOKE_AGENT_MODEL)
    r = await c.complete(MSG, max_tokens=64, budget_key="smoke")
    assert r.output_tokens > 0 and 0 <= r.cost_usd < 0.01


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI absent")
async def test_proposer_call():
    """Proposer via make_client: claude CLI unless a workspace ID is configured (D-010)."""
    c = make_client("proposer", config.PROPOSER_MODEL)
    r = await c.complete(MSG, max_tokens=256, budget_key="smoke")
    assert r.text and 0 < r.cost_usd < 0.05
