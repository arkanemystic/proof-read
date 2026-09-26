import pytest

from proofread.contracts import (
    Action, EpisodeResult, FakeSandbox, Genome, GradeResult, InMemoryEventLog, InMemoryStore,
    InMemoryVectorIndex, ModelResponse, ScriptedModelClient, StubGrader, StubVerifier, Task, Verdict,
    Grader, Sandbox, Verifier, Store, EventLog, VectorIndex, ModelClient,
)


def _task():
    return Task(id="t/original/x", variant="original", files={"func.py": "def f():\n    pass\n", "test.py": "x"},
                test_paths=["test.py"], spec="do it")


def test_action_canonical_roundtrip():
    a = Action(episode_id="e", step=1, kind="write", path="/workspace/a.py", added_lines=["x"])
    assert Action.model_validate_json(a.canonical_json()) == a
    assert len(a.digest()) == 64


def test_genome_hash_stable_and_forbids_extra():
    assert Genome().content_hash() == Genome().content_hash()
    with pytest.raises(Exception):
        Genome.model_validate({"tool_impl": "x"})


async def test_stubs_satisfy_protocols():
    sb = FakeSandbox()
    assert isinstance(sb, Sandbox) and isinstance(StubVerifier(), Verifier) and isinstance(StubGrader(), Grader)
    assert isinstance(InMemoryStore(), Store) and isinstance(InMemoryEventLog(), EventLog)
    assert isinstance(InMemoryVectorIndex(), VectorIndex) and isinstance(ScriptedModelClient([]), ModelClient)
    t = _task()
    await sb.start(t)
    acts = await sb.write("test.py", "y")
    assert acts[0].kind == "write" and acts[0].path == "/workspace/test.py"
    sb.files["/workspace/sneaky.py"] = "z"
    fd = await sb.final_diff()
    assert fd and all(not a.attributed for a in fd)
    v = StubVerifier(rule=lambda a: ["CODE-TEST-001"] if a.path.endswith("test.py") else [])
    out = await v.check_batch(acts)
    assert isinstance(out[0], Verdict) and not out[0].ok
    assert (await StubGrader().grade(t, sb)).passed


def test_eventlog_idempotent_and_monotone():
    log = InMemoryEventLog()
    s1 = log.append("x", "e1:stage", {})
    assert log.append("x", "e1:stage", {}) == s1
    s2 = log.append("x", "e2:stage", {})
    assert s2 > s1 and [e.seq for e in log.read()] == [s1, s2]


def test_episode_result_defaults():
    r = EpisodeResult(episode_id="e", task_id="t", variant="oneoff", genome_hash="g", model="m", mode="observe")
    assert r.mechanism == "none" and GradeResult(grader="stub", passed=True).passed
    assert ModelResponse().text == ""


async def test_stub_episode_runner():
    from proofread.contracts import EpisodeRunner, StubEpisodeRunner
    r = StubEpisodeRunner(lambda t, g, m, s: (True, ["CODE-TEST-001"] if t.impossible else []))
    assert isinstance(r, EpisodeRunner)
    t = Task(id="x/oneoff/1", variant="oneoff", files={}, test_paths=[], spec="")
    res = await r(t, Genome(), mode="observe", model="m")
    assert res.passed_workspace and res.mechanism == "test_edit"
    res = await r(t, Genome(), mode="enforce", model="m")
    assert not res.passed_workspace
