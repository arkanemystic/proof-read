"""ClaudeCliClient (D-010) against a fake `claude` executable, plus make_client routing."""

import json
import os
import sys

import httpx
import pytest

from proofread.contracts import BudgetExceeded, ChatMessage, ToolSpec
from proofread.models import client as client_mod
from proofread.models import config
from proofread.models.cli_client import ClaudeCliClient, ClaudeCliError, flatten
from proofread.models.client import AnthropicClient, OpenRouterClient, make_client
from proofread.models.ledger import SpendLedger

FAKE = r'''
import json, os, sys
prompt = sys.stdin.read()
state = os.environ["FAKE_STATE"]
n = int(open(state).read()) if os.path.exists(state) else 0
open(state, "w").write(str(n + 1))
if n < int(os.environ.get("FAKE_FAILS", "0")):
    sys.exit(3)
json.dump({"prompt": prompt, "argv": sys.argv[1:], "cwd": os.getcwd(),
           "has_key": "ANTHROPIC_API_KEY" in os.environ or "PROPOSER_API_KEY" in os.environ}, open(state + ".log", "w"))
print(json.dumps({"type": "result", "is_error": False, "result": "PATCH", "total_cost_usd": 0.0123,
                  "stop_reason": "end_turn",
                  "usage": {"input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10,
                            "output_tokens": 7}}))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE)
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    return [sys.executable, str(script)], tmp_path


async def test_cli_client_parses_and_records(fake):
    cmd, tmp = fake
    led = SpendLedger(tmp / "s.sqlite")
    c = ClaudeCliClient("proposer", "claude-opus-5-5", ledger=led, command=cmd, cwd=tmp / "cwd")
    r = await c.complete([ChatMessage(role="system", content="S"), ChatMessage(role="user", content="U")],
                         budget_key="arm_A")
    assert r.text == "PATCH" and r.cost_usd == 0.0123 and r.input_tokens == 112 and r.output_tokens == 7
    assert abs(led.spent("arm_A") - 0.0123) < 1e-12
    log = json.loads((tmp / "state.log").read_text())
    assert "<system>\nS\n</system>" in log["prompt"] and "<user>\nU\n</user>" in log["prompt"]
    assert not log["has_key"] and log["cwd"] == str(tmp / "cwd")


async def test_cli_client_retries_then_fails(fake, monkeypatch):
    cmd, tmp = fake
    monkeypatch.setenv("FAKE_FAILS", "2")
    c = ClaudeCliClient("proposer", "m", ledger=SpendLedger(tmp / "s.sqlite"), command=cmd, cwd=tmp, retries=2)
    assert (await c.complete([ChatMessage(role="user", content="x")])).text == "PATCH"
    (tmp / "state").unlink()
    monkeypatch.setenv("FAKE_FAILS", "5")
    c = ClaudeCliClient("proposer", "m", ledger=SpendLedger(tmp / "s2.sqlite"), command=cmd, cwd=tmp, retries=1)
    with pytest.raises(ClaudeCliError):
        await c.complete([ChatMessage(role="user", content="x")])


async def test_cli_client_rejects_tools_and_checks_budget(fake):
    cmd, tmp = fake
    led = SpendLedger(tmp / "s.sqlite", caps={"arm_A": 0.01})
    c = ClaudeCliClient("proposer", "m", ledger=led, command=cmd, cwd=tmp)
    with pytest.raises(ClaudeCliError):
        await c.complete([ChatMessage(role="user", content="x")],
                         tools=[ToolSpec(name="t", description="", parameters={})])
    led.record(role="proposer", model="m", budget_key="arm_A", input_tokens=0, output_tokens=0, cost_usd=0.02)
    with pytest.raises(BudgetExceeded):
        await c.complete([ChatMessage(role="user", content="x")], budget_key="arm_A")
    assert not (tmp / "state").exists()  # never spawned


def test_default_command_and_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    c = ClaudeCliClient("proposer", "claude-opus-5-5")
    assert c.command == ["claude", "-p", "--model", "claude-opus-5-5", "--output-format", "json",
                         "--strict-mcp-config", "--tools", ""]
    assert "ANTHROPIC_API_KEY" not in c.child_env()
    assert flatten([ChatMessage(role="user", content="only")]) == "only"


def test_make_client_routing(monkeypatch):
    keys = {"OPENROUTER_API_KEY": "or", "BASELINE_API_KEY": "b", "PROPOSER_API_KEY": "p", "AGENT_API_KEY": "a"}
    monkeypatch.setattr(client_mod, "load_key", lambda n: keys[n])
    monkeypatch.setattr(client_mod, "load_workspace_id", lambda n: "")
    assert isinstance(make_client("proposer", "claude-opus-5-5"), ClaudeCliClient)
    for native, slug in [("claude-haiku-4-5-20251001", "anthropic/claude-haiku-4.5"),
                         ("claude-opus-5-5", "anthropic/claude-opus-5.5"), ("claude-sonnet-5", "anthropic/claude-sonnet-5")]:
        c = make_client("baseline", native)
        assert isinstance(c, OpenRouterClient) and c.slug == slug and c._key == "or" and c.model == native
    c = make_client("agent", "claude-sonnet-5")
    assert isinstance(c, OpenRouterClient) and c._key == "a"
    monkeypatch.setattr(client_mod, "load_workspace_id", lambda n: "wrkspc_x")
    p = make_client("proposer", "claude-opus-5-5")
    assert isinstance(p, AnthropicClient) and p._key == "p" and p.workspace_id == "wrkspc_x"
    b = make_client("baseline", "claude-sonnet-5")
    assert isinstance(b, AnthropicClient) and b._key == "b"


def test_reasoning_effort_defaults(monkeypatch):
    monkeypatch.delenv("PROOFREAD_REASONING_EFFORT", raising=False)
    assert config.reasoning_effort("agent", "anthropic/claude-sonnet-5") == "low"
    assert config.reasoning_effort("selection", "openai/gpt-5.5") == "low"
    assert config.reasoning_effort("baseline", "x") == "low"
    monkeypatch.setitem(config.OPENROUTER_REASONING_EFFORT, "x", "high")
    assert config.reasoning_effort("baseline", "x") == "high"
    monkeypatch.setenv("PROOFREAD_REASONING_EFFORT", "")
    assert config.reasoning_effort("agent", "x") == ""
