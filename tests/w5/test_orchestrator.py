import json
import time

import pytest

from proofread.contracts import ModelResponse, ScriptedModelClient, StubEpisodeRunner, Task
from proofread.evolve.config import arm_preset
from proofread.evolve.orchestrator import run_arm
from proofread.store.sqlite_store import SqliteEventLog, SqliteStore

N_TASKS = 10


def _tasks(split: str = "training") -> list[Task]:
    return [Task(id=f"lcb/original/t_{i}", variant="original", files={"func.py": "", "test.py": ""},
                 test_paths=["test.py"], spec="s", meta={"i": i}) for i in range(N_TASKS)]


def outcome(task, genome, mode, seed):
    notes = " ".join(genome.memory_notes)
    i = task.meta["i"]
    if "BAD" in notes:
        return False, []
    if "CHEAT" in notes:
        return True, (["CODE-TEST-001"] if i >= 3 else [])
    if "CLEAN" in notes:
        return True, []
    return i < 3, []


def _props(*notes_or_ops):
    out = []
    for x in notes_or_ops:
        ops = x if isinstance(x, list) else [{"op": "add", "path": "/memory_notes/-", "value": x}]
        intent = f"intent for {json.dumps(ops)}"
        out.append(ModelResponse(text=json.dumps({"intent": intent}), cost_usd=0.01))
        out.append(ModelResponse(text="```json\n" + json.dumps({"intent": intent, "patch": ops,
                                                                 "rationale": "because"}) + "\n```", cost_usd=0.02))
    return out


def _db(tmp_path):
    p = tmp_path / "proofread.sqlite"
    return SqliteStore(p), SqliteEventLog(p)


async def test_arm_c_rejects_violating_improvement_arm_a_promotes(tmp_path):
    store, log = _db(tmp_path)
    res = {}
    for arm in ("A", "C"):
        cfg = arm_preset(arm, generations=1, candidates_per_generation=1, agent_model="stub", concurrency=4)
        client = ScriptedModelClient(responses=_props("CHEAT"), role="proposer")
        res[arm] = await run_arm(cfg, StubEpisodeRunner(outcome), client, store, log, _tasks)
        assert len(client.calls) == 2
    a_edit = store.get("edits", "armA-g0-c0")
    c_edit = store.get("edits", "armC-g0-c0")
    assert a_edit["status"] == "promoted" and res["A"]["champion"] == "armA:v2"
    assert c_edit["status"] == "rejected_formal" and res["C"]["champion"] == "armC:v1"
    # counterfactual delta recorded for the formally rejected edit
    assert c_edit["counterfactual_delta_points"] == pytest.approx(70.0)
    assert store.get("rejected_edits", "armC-g0-c0")["reason"].startswith("formal")
    assert store.get("rejected_edits", "armA-g0-c0") is None
    promoted = store.get("harness_versions", "armA:v2")
    assert promoted["version"] == 2 and "CHEAT" in promoted["genome"]["memory_notes"][0]
    eps = store.find("episodes", {"arm": "C", "candidate_id": "armC-g0-c0"})
    assert len(eps) == N_TASKS and all(e["generation"] == 0 and e["mode"] == "observe" for e in eps)
    assert len(store.find("episodes", {"arm": "C", "candidate_id": "champion"})) == N_TASKS
    # rejected edit is retrievable by intent similarity
    from proofread.store.vector import NumpyVectorIndex, embed
    hits = NumpyVectorIndex(store, namespace="rejected:armC").search(embed(c_edit["intent"]), k=1)
    assert hits[0][0] == "armC-g0-c0" and hits[0][1] > 0.99


async def test_multi_generation_screening_invalid_and_retrieval(tmp_path):
    store, log = _db(tmp_path)
    cfg = arm_preset("C", generations=2, candidates_per_generation=3, agent_model="stub")
    client = ScriptedModelClient(responses=_props(
        "CLEAN strategy", "BAD idea", "CHEAT idea",  # gen 0
        "another note", [{"op": "replace", "path": "/version", "value": 7}], "CHEAT idea again",  # gen 1
    ), role="proposer")
    runner = StubEpisodeRunner(outcome)
    out = await run_arm(cfg, runner, client, store, log, _tasks)
    st = out["edits"]
    assert st == {"armC-g0-c0": "promoted", "armC-g0-c1": "rejected_empirical", "armC-g0-c2": "rejected_formal",
                  "armC-g1-c0": "rejected_empirical", "armC-g1-c1": "invalid", "armC-g1-c2": "rejected_formal"}
    assert store.get("edits", "armC-g0-c1")["reason"].startswith("screening")
    assert store.get("edits", "armC-g1-c1")["reason"].startswith("static")
    assert out["champion"] == "armC:v2" and out["generations_completed"] == 2 and not out["stopped"]
    # screened-out candidate ran only the 5 screening tasks; champion re-evaluated in each generation
    assert len(store.find("episodes", {"candidate_id": "armC-g0-c1"})) == 5
    assert len(store.find("episodes", {"candidate_id": "champion", "generation": 1})) == N_TASKS
    # gen 1 champion evaluation reflects the promoted genome (all pass)
    assert all(e["passed_workspace"] for e in store.find("episodes", {"candidate_id": "champion", "generation": 1}))
    # the gen-1 proposer step 2 saw the similar rejected CHEAT edit from gen 0
    step2_last = client.calls[-1][-1].content
    assert "armC-g0-c2" in step2_last
    # history shown in step 1 of gen 1
    assert "armC-g0-c0" in client.calls[6][1].content
    assert len(client.calls) == 12


async def test_noret_arm_skips_retrieval(tmp_path):
    store, log = _db(tmp_path)
    cfg = arm_preset("C-noret", generations=2, candidates_per_generation=1, agent_model="stub")
    client = ScriptedModelClient(responses=_props("CHEAT a", "CHEAT a"), role="proposer")
    await run_arm(cfg, StubEpisodeRunner(outcome), client, store, log, _tasks)
    assert "(none retrieved)" in client.calls[-1][-1].content
    assert cfg.budget_key == "arm_C_noret"


class Crash(BaseException):
    pass


class CrashingRunner(StubEpisodeRunner):
    def __init__(self, outcome, crash_after):
        super().__init__(outcome)
        self.crash_after = crash_after

    async def __call__(self, task, genome, **kw):
        if len(self.calls) >= self.crash_after:
            raise Crash()
        return await super().__call__(task, genome, **kw)


async def test_resume_after_crash_matches_clean_run(tmp_path):
    gen0 = ("CLEAN strategy", "BAD idea")
    gen1 = ("another note", [{"op": "replace", "path": "/version", "value": 7}])
    # reference: uninterrupted run
    ref_store, ref_log = _db(tmp_path / "ref")
    ref_runner = StubEpisodeRunner(outcome)
    cfg = arm_preset("C", generations=2, candidates_per_generation=2, agent_model="stub", concurrency=3)
    ref = await run_arm(cfg, ref_runner, ScriptedModelClient(responses=_props(*gen0, *gen1)), ref_store, ref_log,
                        _tasks)

    p = tmp_path / "crash" / "db.sqlite"
    runner1 = CrashingRunner(outcome, crash_after=17)
    client1 = ScriptedModelClient(responses=_props(*gen0, *gen1))
    with pytest.raises(Crash):
        await run_arm(cfg, runner1, client1, SqliteStore(p), SqliteEventLog(p), _tasks)
    assert len(client1.calls) == 4  # both gen-0 proposals made before the crash
    # restart with fresh connections; the proposer only has gen-1 responses, so any redo would diverge
    runner2 = StubEpisodeRunner(outcome)
    client2 = ScriptedModelClient(responses=_props(*gen1))
    store2 = SqliteStore(p)
    out = await run_arm(cfg, runner2, client2, store2, SqliteEventLog(p), _tasks)
    assert len(client2.calls) == 4
    assert out["edits"] == ref["edits"] and out["champion"] == ref["champion"]
    assert out["champion_hash"] == ref["champion_hash"]
    # no finished episode was rerun: persisted before crash + rerun == reference total
    assert len(runner1.calls) + len(runner2.calls) == len(ref_runner.calls)
    assert len(store2.find("episodes")) == len(ref_store.find("episodes"))
    # a third start is a no-op
    runner3 = StubEpisodeRunner(outcome)
    client3 = ScriptedModelClient(responses=[])
    again = await run_arm(cfg, runner3, client3, store2, SqliteEventLog(p), _tasks)
    assert runner3.calls == [] and client3.calls == [] and again["edits"] == ref["edits"]


async def test_deadline_stops_launching(tmp_path):
    store, log = _db(tmp_path)
    cfg = arm_preset("A", generations=2, candidates_per_generation=1, agent_model="stub")
    runner = StubEpisodeRunner(outcome)
    out = await run_arm(cfg, runner, ScriptedModelClient(responses=_props("CLEAN")), store, log, _tasks,
                        deadline_ts=time.time() - 1)
    assert runner.calls == [] and out["stopped"].startswith("deadline") and out["generations_completed"] == 0


async def test_budget_exceeded_stops_arm(tmp_path):
    from proofread.contracts import BudgetExceeded

    class Broke(ScriptedModelClient):
        async def complete(self, *a, **k):
            raise BudgetExceeded("arm_A cap")

    store, log = _db(tmp_path)
    cfg = arm_preset("A", generations=1, candidates_per_generation=1, agent_model="stub")
    out = await run_arm(cfg, StubEpisodeRunner(outcome), Broke(responses=[]), store, log, _tasks)
    assert out["stopped"].startswith("budget")


async def test_runner_exception_counts_as_failure_not_persisted(tmp_path):
    class Flaky(StubEpisodeRunner):
        async def __call__(self, task, genome, **kw):
            if task.meta["i"] == 0:
                raise RuntimeError("docker hiccup")
            return await super().__call__(task, genome, **kw)

    store, log = _db(tmp_path)
    cfg = arm_preset("A", generations=1, candidates_per_generation=1, agent_model="stub")
    await run_arm(cfg, Flaky(outcome), ScriptedModelClient(responses=_props("CLEAN")), store, log, _tasks)
    assert len(store.find("episodes", {"candidate_id": "champion"})) == N_TASKS - 1
