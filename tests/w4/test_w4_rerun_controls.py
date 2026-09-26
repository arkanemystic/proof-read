"""Rerun (section 7) cost controls: episode cost abort, context token cap, env budget caps."""

from proofread.agent.episode import make_episode_runner
from proofread.agent.loop import AgentLoop, Tracer
from proofread.contracts import (
    BudgetExceeded, ChatMessage, FakeSandbox, Genome, ModelResponse, ScriptedModelClient, StubGrader, StubVerifier,
    Task, ToolCall,
)
from proofread.models.ledger import SpendLedger

import pytest


def task():
    return Task(id="lcb/oneoff/t1", variant="oneoff", files={"func.py": "def f():\n    pass\n",
                "test.py": "from func import f\nassert f() == 2\n"}, test_paths=["test.py"], spec="Implement f.",
                test_command=["python", "test.py"])


async def test_episode_cost_abort(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFREAD_EPISODE_COST_CAP_USD", "0.20")
    script = [ModelResponse(tool_calls=[ToolCall(id=f"c{i}", name="list_dir", arguments={"path": "."})],
                            cost_usd=0.08) for i in range(10)]
    r = make_episode_runner(verifier_factory=StubVerifier, sandbox_factory=lambda e: FakeSandbox(e),
                            client_factory=lambda role, m: ScriptedModelClient(script),
                            workspace_grader=StubGrader(False), trace_dir=tmp_path)
    res = await r(task(), Genome(), mode="observe", model="m", arm="R1")
    assert res.error.startswith("aborted_cost") and res.turns == 3


async def test_context_cap_drops_oldest_tool_output(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFREAD_CONTEXT_TOKENS", "1500")
    sb = FakeSandbox("e1")
    loop = AgentLoop(task=task(), genome=Genome(), mode="observe", sandbox=sb, verifier=StubVerifier(),
                     client=ScriptedModelClient([]), episode_id="e1", tracer=Tracer(tmp_path / "t.jsonl"))
    loop.history = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="u")]
    for i in range(3):
        loop.history += [ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id=f"c{i}", name="run",
                                                                                         arguments={"cmd": "x"})]),
                         ChatMessage(role="tool", content=str(i) * 4000, tool_call_id=f"c{i}")]
    w = loop.window()
    tools = [m.content for m in w if m.role == "tool"]
    assert tools[0].startswith("[older") and tools[1].startswith("[older") and tools[2] == "2" * 4000


def test_env_budget_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOFREAD_BUDGET_TOTAL_USD", "25")
    monkeypatch.setenv("PROOFREAD_BUDGET_CAPS", '{"rerun_R1": 10}')
    led = SpendLedger(tmp_path / "s.sqlite")
    led.record(role="agent", model="m", budget_key="rerun_R1", input_tokens=1, output_tokens=1, cost_usd=10.0)
    with pytest.raises(BudgetExceeded):
        led.check("rerun_R1")
    led.check("rerun_R2")
    led.record(role="agent", model="m", budget_key="rerun_R2", input_tokens=1, output_tokens=1, cost_usd=15.0)
    with pytest.raises(BudgetExceeded):
        led.check("rerun_R2")
