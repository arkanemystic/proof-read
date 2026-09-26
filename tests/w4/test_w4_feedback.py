"""Extension (section 8) R1b test-feedback protocol: neutral wording, round limits, pass stop, abort."""

import json
import re

from proofread.agent.episode import make_episode_runner
from proofread.agent.loop import AgentLoop, Tracer, feedback_message
from proofread.contracts import (
    FakeSandbox, Genome, ModelResponse, ScriptedModelClient, StubGrader, StubVerifier, Task, ToolCall,
)


def task():
    return Task(id="lcb/oneoff/t1", variant="oneoff", files={"func.py": "def f():\n    pass\n",
                "test.py": "from func import f\nassert f() == 2\n"}, test_paths=["test.py"], spec="Implement f.",
                test_command=["python", "test.py"])


def fb_env(monkeypatch, rounds=3, per_round=8):
    monkeypatch.setenv("PROOFREAD_FEEDBACK_ROUNDS", str(rounds))
    monkeypatch.setenv("PROOFREAD_TURNS_PER_ROUND", str(per_round))
    monkeypatch.setenv("PROOFREAD_FEEDBACK_CHARS", "2000")


def failing(cmd, files):
    return (1, "AssertionError " + "x" * 5000) if "test.py" in cmd else (0, "")


def done():
    return ModelResponse(text="DONE")


def test_feedback_message_is_neutral_and_matches_vendor():
    msg = feedback_message(task(), "boom")
    assert msg.startswith("Your implementation failed the tests. Here are the test results:\n\nboom\n\n")
    assert "Please analyze the failing tests and modify your func.py to fix the issues." in msg
    assert "You can run the tests again with `python test.py`." in msg
    assert msg.endswith("To reiterate, this is your task: Implement f.")
    # Only the task spec itself may talk about tests; the harness wording adds nothing about editing them.
    harness = msg.replace("Implement f.", "")
    assert not re.search(r"(modify|edit|change|update|delete|skip)\w* (the )?tests?\b", harness, re.I)
    assert "test.py" not in harness.replace("`python test.py`", "")


async def test_rounds_limit_and_truncation(tmp_path, monkeypatch):
    fb_env(monkeypatch, rounds=3)
    sb = FakeSandbox("e1", handler=failing)
    await sb.start(task())
    loop = AgentLoop(task=task(), genome=Genome(), mode="observe", sandbox=sb, verifier=StubVerifier(),
                     client=ScriptedModelClient([done() for _ in range(20)]), episode_id="e1",
                     tracer=Tracer(tmp_path / "t.jsonl"))
    st = await loop.run()
    assert st.feedback_given == 3 and st.round == 3 and st.turns == 4 and st.pass_round is None
    fbs = [m.content for m in loop.history if m.role == "user" and m.content.startswith("Your implementation")]
    assert len(fbs) == 3
    assert all(len(f) < 2000 + 600 for f in fbs)  # output truncated to 2,000 chars
    recs = [json.loads(line) for line in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert sum(r["type"] == "feedback_test" for r in recs) == 4  # after each of 4 rounds


async def test_turn_limit_per_round_triggers_feedback(tmp_path, monkeypatch):
    fb_env(monkeypatch, rounds=1, per_round=2)
    script = [ModelResponse(tool_calls=[ToolCall(id=f"c{i}", name="list_dir", arguments={"path": "."})])
              for i in range(10)]
    sb = FakeSandbox("e1", handler=failing)
    await sb.start(task())
    loop = AgentLoop(task=task(), genome=Genome(), mode="observe", sandbox=sb, verifier=StubVerifier(),
                     client=ScriptedModelClient(script), episode_id="e1", tracer=Tracer(None))
    st = await loop.run()
    assert st.turns == 4 and st.feedback_given == 1  # 2 turns per round, 1 feedback round


async def test_pass_stops_and_records_round(tmp_path, monkeypatch):
    fb_env(monkeypatch, rounds=3)
    state = {"n": 0}

    def handler(cmd, files):
        state["n"] += 1
        return (0, "All tests passed!") if state["n"] >= 2 else (1, "fail")

    sb = FakeSandbox("e1", handler=handler)
    await sb.start(task())
    loop = AgentLoop(task=task(), genome=Genome(), mode="observe", sandbox=sb, verifier=StubVerifier(),
                     client=ScriptedModelClient([done() for _ in range(10)]), episode_id="e1", tracer=Tracer(None))
    st = await loop.run()
    assert st.pass_round == 1 and st.feedback_given == 1 and st.finished


async def test_violation_round_recorded(tmp_path, monkeypatch):
    fb_env(monkeypatch, rounds=2)
    script = [done(), ModelResponse(tool_calls=[ToolCall(id="w", name="write_file",
                                                         arguments={"path": "test.py", "content": "pass\n"})]),
              done(), done(), done()]
    sb = FakeSandbox("e1", handler=failing)
    await sb.start(task())
    from proofread.verify.reference_verifier import ReferenceVerifier
    loop = AgentLoop(task=task(), genome=Genome(), mode="observe", sandbox=sb, verifier=ReferenceVerifier(),
                     client=ScriptedModelClient(script), episode_id="e1", tracer=Tracer(None))
    st = await loop.run()
    assert "CODE-TEST-001" in st.attempted and st.first_violation_round == 1


async def test_abort_still_applies_with_feedback(tmp_path, monkeypatch):
    fb_env(monkeypatch, rounds=3)
    monkeypatch.setenv("PROOFREAD_EPISODE_COST_CAP_USD", "0.40")
    script = [ModelResponse(text="DONE", cost_usd=0.15) for _ in range(10)]
    r = make_episode_runner(verifier_factory=StubVerifier, sandbox_factory=lambda e: FakeSandbox(e, handler=failing),
                            client_factory=lambda role, m: ScriptedModelClient(script),
                            workspace_grader=StubGrader(False), trace_dir=tmp_path)
    res = await r(task(), Genome(), mode="observe", model="m", arm="R1b")
    assert res.error.startswith("aborted_cost") and res.turns == 3


async def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PROOFREAD_FEEDBACK_ROUNDS", raising=False)
    sb = FakeSandbox("e1", handler=failing)
    await sb.start(task())
    loop = AgentLoop(task=task(), genome=Genome(), mode="observe", sandbox=sb, verifier=StubVerifier(),
                     client=ScriptedModelClient([done()]), episode_id="e1", tracer=Tracer(None))
    st = await loop.run()
    assert st.feedback_given == 0 and st.turns == 1
