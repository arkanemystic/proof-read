import asyncio
import json

import pytest

from baselines import runner as br
from proofread.contracts import BudgetExceeded, Genome, InMemoryStore, StubEpisodeRunner, Task

SPLITS = {
    "capability_holdout": [f"lcb/original/c_{i}" for i in range(6)],
    "cheat_holdout": [f"lcb/oneoff/h_{i}" for i in range(6)],
}


def load_task(tid):
    return Task(id=tid, variant=tid.split("/")[1], files={"func.py": "", "test.py": ""}, test_paths=["test.py"],
                spec="s")


def list_tasks(split):
    return SPLITS[split]


async def test_run_baseline_stores_every_episode():
    store, runner = InMemoryStore(), StubEpisodeRunner()
    s = await br.run_baseline("m1", "capability_holdout", "observe", runner, store, 3, True, "baselines",
                              genome=Genome(), list_tasks=list_tasks, load_task=load_task)
    assert (s.planned, s.completed, s.skipped_existing, s.errors) == (6, 6, 0, 0)
    eps = store.find("episodes", {"arm": "baseline", "model": "m1"})
    assert len(eps) == 6
    assert all(e["budget_key"] == "baselines" and e["passed_pristine"] is True for e in eps)


async def test_resume_skips_existing():
    store, runner = InMemoryStore(), StubEpisodeRunner()
    kw = dict(genome=Genome(), list_tasks=list_tasks, load_task=load_task)
    await br.run_baseline("m1", "cheat_holdout", "observe", runner, store, 2, False, "baselines", **kw)
    n = len(runner.calls)
    s = await br.run_baseline("m1", "cheat_holdout", "observe", runner, store, 2, False, "baselines", **kw)
    assert len(runner.calls) == n and s.skipped_existing == 6 and s.completed == 0
    # different mode is a different combo
    s = await br.run_baseline("m1", "cheat_holdout", "enforce", runner, store, 2, False, "baselines", **kw)
    assert s.completed == 6


async def test_errored_episode_retried_on_resume():
    store = InMemoryStore()
    boom = {"n": 0}

    class Flaky(StubEpisodeRunner):
        async def __call__(self, task, genome, **kw):
            if task.id.endswith("_0") and boom["n"] == 0:
                boom["n"] += 1
                raise RuntimeError("docker died")
            return await super().__call__(task, genome, **kw)

    kw = dict(genome=Genome(), list_tasks=list_tasks, load_task=load_task)
    s = await br.run_baseline("m", "cheat_holdout", "observe", Flaky(), store, 2, False, "baselines", **kw)
    assert s.errors == 1 and s.completed == 5
    s = await br.run_baseline("m", "cheat_holdout", "observe", Flaky(), store, 2, False, "baselines", **kw)
    assert s.completed == 1 and s.skipped_existing == 5


async def test_budget_exceeded_stops_launching():
    store = InMemoryStore()

    class Capped(StubEpisodeRunner):
        async def __call__(self, task, genome, **kw):
            if len(self.calls) >= 2:
                raise BudgetExceeded("baselines cap")
            return await super().__call__(task, genome, **kw)

    r = Capped()
    s = await br.run_baseline("m", "capability_holdout", "observe", r, store, 1, False, "baselines",
                              genome=Genome(), list_tasks=list_tasks, load_task=load_task)
    assert s.stopped and "BudgetExceeded" in s.stop_reason
    assert s.completed == 2 and s.not_launched == 4 and len(r.calls) == 2
    assert store.get("events", "workload_stopped:baselines:baseline")["reason"].startswith("BudgetExceeded")


async def test_budget_error_in_result_also_stops():
    store = InMemoryStore()

    class Capped(StubEpisodeRunner):
        async def __call__(self, task, genome, **kw):
            res = await super().__call__(task, genome, **kw)
            return res.model_copy(update={"error": "BudgetExceeded: cap"})

    s = await br.run_baseline("m", "capability_holdout", "observe", Capped(), store, 1, False, "baselines",
                              genome=Genome(), list_tasks=list_tasks, load_task=load_task)
    assert s.stopped and s.errors == 1 and s.not_launched == 5


async def test_deadline_stops_launching():
    store, runner = InMemoryStore(), StubEpisodeRunner()
    s = await br.run_baseline("m", "capability_holdout", "observe", runner, store, 2, False, "baselines",
                              deadline=0.0, genome=Genome(), list_tasks=list_tasks, load_task=load_task)
    assert s.stopped and s.stop_reason == "deadline" and not runner.calls and s.not_launched == 6


async def test_concurrency_limit_respected():
    live = {"now": 0, "max": 0}

    class Slow(StubEpisodeRunner):
        async def __call__(self, task, genome, **kw):
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            await asyncio.sleep(0.01)
            live["now"] -= 1
            return await super().__call__(task, genome, **kw)

    await br.run_baseline("m", "cheat_holdout", "observe", Slow(), InMemoryStore(), 3, False, "baselines",
                          genome=Genome(), list_tasks=list_tasks, load_task=load_task)
    assert live["max"] == 3


def test_plan_e4_shape():
    items = br.plan_e4(["openai/o3", "anthropic/claude-sonnet-5", "claude-opus-5-5"], list_tasks)
    models = br.e4_models(["openai/o3", "anthropic/claude-sonnet-5", "claude-opus-5-5"])
    assert models == ["openai/o3", "claude-sonnet-5", "claude-opus-5-5", "claude-haiku-4-5-20251001"]
    assert len(items) == 4 * 6 * 3
    stages = [(i.split, i.mode, i.pristine) for i in items]
    assert stages[0] == ("cheat_holdout", "observe", False)
    assert stages[-1] == ("cheat_holdout", "enforce", False)
    assert sum(1 for s in stages if s == ("cheat_holdout", "observe", False)) == 24
    # interleaved: the first 12 items cover every stage x model once (task index 0)
    first = {(i.split, i.mode, i.model) for i in items[:12]}
    assert len(first) == 12


def test_plan_e1_interleaves_cheat_first():
    sp = {"selection_cheat": ["lcb/conflicting/a", "lcb/conflicting/b"], "selection_original": ["lcb/original/a"]}
    items = br.plan_e1(["x", "y"], sp.__getitem__)
    assert [(i.model, i.split) for i in items[:2]] == [("x", "selection_cheat"), ("y", "selection_cheat")]
    assert all(i.mode == "observe" and not i.pristine for i in items) and len(items) == 6


def test_baseline_model_id_and_deadline_parse():
    assert br.baseline_model_id("anthropic/claude-sonnet-5") == "claude-sonnet-5"
    assert br.baseline_model_id("openai/gpt-5") == "openai/gpt-5"
    assert br.parse_deadline("1970-01-01T00:00:10Z") == 10.0
    assert br.parse_deadline(None) is None


def test_load_genome_fallback(tmp_path):
    assert br.load_genome(tmp_path / "missing.json") == Genome()
    p = tmp_path / "g.json"
    p.write_text(Genome(version=7).model_dump_json())
    assert br.load_genome(p).version == 7


def test_cli_e4_stub(tmp_path, capsys, monkeypatch):
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"selected": "openai/o3", "candidates": ["openai/o3"]}))
    monkeypatch.setattr(br, "_default_list_tasks", list_tasks)
    monkeypatch.setattr(br, "_default_load_task", load_task)
    assert br.main(["--plan", "e4", "--stub", "--selected-json", str(sel)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["planned"] == 3 * 6 * 3 and out["completed"] == out["planned"]


@pytest.mark.network
def test_live_discovery_smoke():
    from baselines.selection import discover_candidates, fetch_models

    assert 1 <= len(discover_candidates(fetch_models())) <= 4
