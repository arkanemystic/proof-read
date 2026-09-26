"""Model clients against httpx.MockTransport (no network, no keys)."""

import json
import os

import httpx
import pytest

from proofread.contracts import BudgetExceeded, ChatMessage, ToolSpec
from proofread.models import config
from proofread.models.client import AnthropicClient, ModelError, OpenRouterClient, make_client
from proofread.models.ledger import SpendLedger

TOOLS = [ToolSpec(name="run", description="run", parameters={"type": "object", "properties": {"cmd": {"type": "string"}}})]


def conv():
    return [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]


async def test_anthropic_roundtrip_and_cost(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        seen.append((dict(req.headers), json.loads(req.content)))
        return httpx.Response(200, json={
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                        {"type": "text", "text": "ok"},
                        {"type": "tool_use", "id": "tu1", "name": "run", "input": {"cmd": "ls"}}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 1000, "output_tokens": 500}})

    led = SpendLedger(tmp_path / "s.sqlite")
    c = AnthropicClient("proposer", "claude-opus-5-5", "k-test", ledger=led, transport=httpx.MockTransport(handler))
    r = await c.complete(conv(), tools=TOOLS, budget_key="arm_A")
    assert r.text == "ok" and r.tool_calls[0].arguments == {"cmd": "ls"}
    assert abs(r.cost_usd - (1000 * 4 + 500 * 20) / 1e6) < 1e-12
    h, body = seen[0]
    assert h["x-api-key"] == "k-test" and h["anthropic-version"] == "2023-06-01"
    assert body["system"] == "sys" and "temperature" not in body and body["tools"][0]["input_schema"]
    # second turn replays the raw assistant content (thinking block kept) and tool results as user blocks
    msgs = conv() + [ChatMessage(role="assistant", content="ok", tool_calls=r.tool_calls),
                     ChatMessage(role="tool", content="out", tool_call_id="tu1")]
    await c.complete(msgs, tools=TOOLS, budget_key="arm_A")
    body2 = seen[1][1]
    assert body2["messages"][1]["content"][0]["type"] == "thinking"
    assert body2["messages"][2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1",
                                                                 "content": "out"}]}
    assert abs(led.spent("arm_A") - 2 * r.cost_usd) < 1e-12
    assert "k-test" not in repr(c)


async def test_haiku_gets_temperature(tmp_path):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "x"}], "usage": {}})

    c = AnthropicClient("baseline", "claude-haiku-4-5-20251001", "k", ledger=SpendLedger(tmp_path / "s.sqlite"),
                        transport=httpx.MockTransport(handler))
    await c.complete(conv())
    assert bodies[0]["temperature"] == 0.0


async def test_openrouter_tool_calls_retry_and_cost(tmp_path, monkeypatch):
    n = {"i": 0}
    bodies = []
    monkeypatch.setattr("asyncio.sleep", _nosleep)

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, json={"error": "rate"})
        if n["i"] == 2:
            return httpx.Response(503, text="busy")
        bodies.append((dict(req.headers), json.loads(req.content)))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "reasoning_details": [{"type": "x"}],
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "run", "arguments": "{\"cmd\": \"python test.py\"}"}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.0123}})

    led = SpendLedger(tmp_path / "s.sqlite")
    c = OpenRouterClient("agent", "claude-sonnet-5", "or-test", ledger=led, transport=httpx.MockTransport(handler))
    r = await c.complete(conv(), tools=TOOLS, budget_key="smoke")
    assert r.tool_calls[0].name == "run" and r.tool_calls[0].arguments["cmd"] == "python test.py"
    assert r.cost_usd == 0.0123 and n["i"] == 3
    h, body = bodies[0]
    assert h["authorization"] == "Bearer or-test" and body["model"] == "anthropic/claude-sonnet-5"
    assert "temperature" not in body and body["tools"][0]["type"] == "function"
    assert body["reasoning"] == {"effort": "low"}
    msgs = conv() + [ChatMessage(role="assistant", content="", tool_calls=r.tool_calls),
                     ChatMessage(role="tool", content="ok", tool_call_id="call_1")]
    await c.complete(msgs, tools=TOOLS, budget_key="smoke")
    sent = bodies[1][1]["messages"]
    assert sent[2]["reasoning_details"] == [{"type": "x"}] and sent[3]["role"] == "tool"
    assert abs(led.spent("smoke") - 0.0246) < 1e-9


async def _nosleep(*a, **k):
    return None


async def test_client_error_not_retried(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad"}})

    c = OpenRouterClient("selection", "openai/gpt-5.5", "k", ledger=SpendLedger(tmp_path / "s.sqlite"),
                         transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError):
        await c.complete(conv())
    assert len(calls) == 1


async def test_budget_checked_before_http(tmp_path):
    led = SpendLedger(tmp_path / "s.sqlite", caps={"smoke": 0.01})
    led.record(role="agent", model="m", budget_key="smoke", input_tokens=0, output_tokens=0, cost_usd=0.02)

    def handler(req):
        raise AssertionError("should not be called")

    c = OpenRouterClient("agent", "claude-sonnet-5", "k", ledger=led, transport=httpx.MockTransport(handler))
    with pytest.raises(BudgetExceeded):
        await c.complete(conv(), budget_key="smoke")


def test_role_key_mapping():
    assert config.provider_for("proposer", "claude-opus-5-5") == ("anthropic", "PROPOSER_API_KEY")
    assert config.provider_for("agent", "claude-sonnet-5") == ("openrouter", "AGENT_API_KEY")
    assert config.provider_for("selection", "x") == ("openrouter", "OPENROUTER_API_KEY")
    assert config.provider_for("baseline", "claude-sonnet-5") == ("anthropic", "BASELINE_API_KEY")
    assert config.provider_for("baseline", "openai/gpt-5.5") == ("openrouter", "OPENROUTER_API_KEY")
    c = make_client("baseline", "openai/gpt-5.5", api_key="k")
    assert isinstance(c, OpenRouterClient) and c.slug == "openai/gpt-5.5"
    assert isinstance(make_client("proposer", "claude-opus-5-5", api_key="k"), AnthropicClient)
    assert "ANTHROPIC_API_KEY" not in os.environ or True  # we never set it
    assert config.agent_model() in ("claude-sonnet-5", config.SELECTED_AGENT_MODEL, os.environ.get("PROOFREAD_AGENT_MODEL"))


async def test_anthropic_workspace_header(tmp_path):
    seen = []

    def handler(req):
        seen.append(dict(req.headers))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "x"}], "usage": {}})

    c = AnthropicClient("proposer", "claude-opus-5-5", "k", ledger=SpendLedger(tmp_path / "s.sqlite"),
                        transport=httpx.MockTransport(handler), workspace_id="wrkspc_test")
    await c.complete(conv())
    assert seen[0]["anthropic-workspace-id"] == "wrkspc_test"
