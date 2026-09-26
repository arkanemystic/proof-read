"""Baseline runner: fixed genome, no proposer, any model slug, observe or enforce, concurrent.

Episodes go through an injected `contracts.EpisodeRunner` (the real one takes the global sandbox slot
itself). This module adds a per-workload concurrency limit, a shared stop flag (BudgetExceeded or
deadline), resumability (skip combos already in the store), and stores every EpisodeResult in the
"episodes" collection.

CLI:
  python -m baselines.runner --plan e1 [--candidates-json baselines/candidates.json] ...
  python -m baselines.runner --plan e4 --selected-json results/selected_model.json ...
  python -m baselines.runner --plan single --model M --split S --mode observe [--pristine] ...
  Add --stub for a dry run with contracts.StubEpisodeRunner and an in-memory store.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from proofread.contracts import (
    BudgetExceeded,
    EpisodeResult,
    EpisodeRunner,
    Genome,
    Mode,
    Store,
    Task,
)

log = logging.getLogger("baselines.runner")

REPO = Path(__file__).resolve().parents[1]
DEFAULT_GENOME_PATH = REPO / "proofread" / "genome" / "default_genome.json"
DEFAULT_STORE_PATH = "data/proofread.sqlite"

E1_CONCURRENCY = 12
E4_CONCURRENCY = 4
E4_EXTRA_MODELS = ("claude-haiku-4-5-20251001", "claude-opus-5-5")


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def load_genome(path: Path | str | None = None) -> Genome:
    """W4's default genome if present, else contracts.Genome()."""
    p = Path(path) if path else DEFAULT_GENOME_PATH
    if p.exists():
        return Genome.model_validate_json(p.read_text())
    log.warning("default genome %s absent; using contracts.Genome()", p)
    return Genome()


def episode_key(arm: str, model: str, task_id: str, mode: str, seed: int, pristine: bool) -> str:
    """Deterministic store doc id for one (model, task, mode, seed) combo of a workload."""
    return f"{arm}|{model}|{task_id}|{mode}|s{seed}|{'P' if pristine else 'W'}"


def parse_deadline(s: str | None) -> float | None:
    if not s:
        return None
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def baseline_model_id(slug: str) -> str:
    """E4 routes Anthropic models through the Anthropic "baseline" role, so OpenRouter Anthropic slugs
    (anthropic/claude-sonnet-5) become native ids (claude-sonnet-5). Everything else is unchanged."""
    if slug.startswith("anthropic/"):
        return slug.split("/", 1)[1].replace(".", "-")
    return slug


def _single_tasks(split: str, variant: str | None, limit: int) -> list[str]:
    from proofread.benchmarks.impossiblebench import list_tasks

    ids = list_tasks(split, variant)
    return ids[:limit] if limit else ids


def _default_list_tasks(split: str) -> list[str]:
    from proofread.benchmarks.impossiblebench import list_tasks

    return list_tasks(split)


def _default_load_task(task_id: str) -> Task:
    from proofread.benchmarks.impossiblebench import load_task

    return load_task(task_id)


# --------------------------------------------------------------------------------------------
# Workload gate and work items
# --------------------------------------------------------------------------------------------


@dataclass
class WorkloadGate:
    """Shared by every job of one workload: concurrency limit, deadline, budget stop flag."""

    budget_key: str
    concurrency: int
    deadline: float | None = None
    stopped: bool = False
    stop_reason: str = ""
    clock: Callable[[], float] = time.time

    def can_launch(self) -> bool:
        if self.stopped:
            return False
        if self.deadline is not None and self.clock() >= self.deadline:
            self.stop("deadline")
            return False
        return True

    def stop(self, reason: str) -> None:
        if not self.stopped:
            log.warning("workload %s stopped: %s", self.budget_key, reason)
        self.stopped = True
        self.stop_reason = self.stop_reason or reason


@dataclass(frozen=True)
class WorkItem:
    model: str
    task_id: str
    split: str
    mode: Mode
    pristine: bool
    seed: int = 0


@dataclass
class RunSummary:
    budget_key: str
    arm: str
    planned: int = 0
    skipped_existing: int = 0
    completed: int = 0
    errors: int = 0
    not_launched: int = 0
    stopped: bool = False
    stop_reason: str = ""
    cost_usd: float = 0.0
    results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("results")
        return d


def _is_done(doc: dict[str, Any] | None) -> bool:
    return bool(doc) and not doc.get("error")


async def run_items(
    items: list[WorkItem],
    *,
    runner: EpisodeRunner,
    store: Store,
    gate: WorkloadGate,
    genome: Genome,
    arm: str,
    load_task: Callable[[str], Task] | None = None,
) -> RunSummary:
    """Run work items in order through `gate.concurrency` workers. Resumable and budget-aware."""
    load_task = load_task or _default_load_task
    summary = RunSummary(budget_key=gate.budget_key, arm=arm, planned=len(items))
    queue: asyncio.Queue[WorkItem] = asyncio.Queue()
    for it in items:
        key = episode_key(arm, it.model, it.task_id, it.mode, it.seed, it.pristine)
        if _is_done(store.get("episodes", key)):
            summary.skipped_existing += 1
        else:
            queue.put_nowait(it)

    async def worker() -> None:
        while True:
            try:
                it = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not gate.can_launch():
                summary.not_launched += 1
                continue
            key = episode_key(arm, it.model, it.task_id, it.mode, it.seed, it.pristine)
            try:
                task = load_task(it.task_id)
                res: EpisodeResult = await runner(
                    task, genome, mode=it.mode, model=it.model, seed=it.seed, arm=arm,
                    budget_key=gate.budget_key, pristine=it.pristine,
                )
            except BudgetExceeded as e:
                gate.stop(f"BudgetExceeded: {e}")
                summary.not_launched += 1
                continue
            except Exception as e:  # infra error: record, keep going (resume retries it)
                log.exception("episode %s failed", key)
                summary.errors += 1
                store.put("episodes", key, {
                    "episode_id": key, "task_id": it.task_id, "model": it.model, "mode": it.mode,
                    "seed": it.seed, "arm": arm, "budget_key": gate.budget_key, "split": it.split,
                    "pristine_requested": it.pristine, "error": f"{type(e).__name__}: {e}",
                })
                continue
            doc = res.model_dump(mode="json")
            doc.update(budget_key=gate.budget_key, split=it.split, pristine_requested=it.pristine,
                       store_key=key)
            if "BudgetExceeded" in (res.error or ""):
                gate.stop(res.error)
            if res.error:
                summary.errors += 1
            else:
                summary.completed += 1
            summary.cost_usd += res.cost_usd
            summary.results.append(doc)
            store.put("episodes", key, doc)
            log.info("[%s] %s %s %s pass_ws=%s mech=%s cost=%.4f", gate.budget_key, it.model, it.task_id,
                     it.mode, res.passed_workspace, res.mechanism, res.cost_usd)

    workers = [asyncio.create_task(worker()) for _ in range(max(1, gate.concurrency))]
    await asyncio.gather(*workers)
    summary.stopped, summary.stop_reason = gate.stopped, gate.stop_reason
    if gate.stopped:
        store.put("events", f"workload_stopped:{gate.budget_key}:{arm}", {
            "type": "workload_stopped", "budget_key": gate.budget_key, "arm": arm,
            "reason": gate.stop_reason, "ts": time.time(), "summary": summary.as_dict(),
        })
    return summary


def build_items(model: str, split: str, mode: Mode, pristine: bool, seeds: tuple[int, ...] = (0,),
                list_tasks: Callable[[str], list[str]] | None = None) -> list[WorkItem]:
    list_tasks = list_tasks or _default_list_tasks
    return [WorkItem(model, t, split, mode, pristine, s) for s in seeds for t in list_tasks(split)]


async def run_baseline(
    model: str,
    split: str,
    mode: Mode,
    runner: EpisodeRunner,
    store: Store,
    concurrency: int,
    pristine: bool,
    budget_key: str,
    *,
    arm: str = "baseline",
    seeds: tuple[int, ...] = (0,),
    deadline: float | None = None,
    genome: Genome | None = None,
    gate: WorkloadGate | None = None,
    list_tasks: Callable[[str], list[str]] | None = None,
    load_task: Callable[[str], Task] | None = None,
) -> RunSummary:
    """Run one model on one split with the fixed genome."""
    gate = gate or WorkloadGate(budget_key=budget_key, concurrency=concurrency, deadline=deadline)
    items = build_items(model, split, mode, pristine, seeds, list_tasks)
    return await run_items(items, runner=runner, store=store, gate=gate, genome=genome or load_genome(),
                           arm=arm, load_task=load_task)


# --------------------------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------------------------


def plan_e1(candidates: list[str], list_tasks=None) -> list[WorkItem]:
    """E1: each candidate on selection_cheat and selection_original, observe, workspace grader only.
    Cheat tasks are interleaved first so a budget stop still leaves the cheat signal for everyone."""
    items: list[WorkItem] = []
    for split in ("selection_cheat", "selection_original"):
        per = [build_items(m, split, "observe", False, (0,), list_tasks) for m in candidates]
        for row in zip(*per):  # round-robin across candidates
            items.extend(row)
    return items


def e4_models(candidates: list[str]) -> list[str]:
    out: list[str] = []
    for m in [baseline_model_id(c) for c in candidates] + list(E4_EXTRA_MODELS):
        if m not in out:
            out.append(m)
    return out


def plan_e4(candidates: list[str], list_tasks=None) -> list[WorkItem]:
    """E4: every E1 candidate plus haiku-4.5 and opus-5.5 on cheat_holdout (observe), capability_holdout
    (pristine, observe) and cheat_holdout (enforce). Interleaved by task index, then stage, then model
    (integrator change, D-016), so a deadline or budget cut leaves every model and stage equally covered."""
    models = e4_models(candidates)
    stages = [[build_items(m, split, mode, pristine, (0,), list_tasks) for m in models]
              for split, mode, pristine in (("cheat_holdout", "observe", False),
                                            ("capability_holdout", "observe", True),
                                            ("cheat_holdout", "enforce", False))]
    items: list[WorkItem] = []
    for i in range(max(len(per[0]) for per in stages)):
        for per in stages:
            items.extend(row[i] for row in per if i < len(row))
    return items


async def run_plan(items: list[WorkItem], *, runner: EpisodeRunner, store: Store, budget_key: str, arm: str,
                   concurrency: int, deadline: float | None = None, genome: Genome | None = None,
                   load_task=None) -> RunSummary:
    gate = WorkloadGate(budget_key=budget_key, concurrency=concurrency, deadline=deadline)
    return await run_items(items, runner=runner, store=store, gate=gate, genome=genome or load_genome(),
                           arm=arm, load_task=load_task)


# --------------------------------------------------------------------------------------------
# Wiring to real parts (lazy imports)
# --------------------------------------------------------------------------------------------


def make_real_runner() -> EpisodeRunner:
    from proofread.agent.episode import make_episode_runner

    return make_episode_runner()


def make_real_store(path: str = DEFAULT_STORE_PATH) -> Store:
    from proofread.store.factory import make_store  # SQLite unless MONGODB_URI is set

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return make_store(path)


def make_stub_parts():
    from proofread.contracts import InMemoryStore, StubEpisodeRunner

    return StubEpisodeRunner(), InMemoryStore()


def _load_candidates(args) -> list[str]:
    if args.selected_json and Path(args.selected_json).exists():
        data = json.loads(Path(args.selected_json).read_text())
        return list(data["candidates"])
    from baselines.selection import load_or_discover_candidates

    return [c["slug"] for c in load_or_discover_candidates(args.candidates_json)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m baselines.runner")
    ap.add_argument("--plan", choices=["e1", "e4", "single"], required=True)
    ap.add_argument("--selected-json", default="results/selected_model.json")
    ap.add_argument("--candidates-json", default=None, help="E1 candidate list (default baselines/candidates.json)")
    ap.add_argument("--store", default=DEFAULT_STORE_PATH)
    ap.add_argument("--genome", default=None)
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--deadline", default=None, help="ISO UTC; no new episodes launched after it")
    ap.add_argument("--model", default=None)
    ap.add_argument("--split", default=None)
    ap.add_argument("--mode", choices=["observe", "enforce"], default="observe")
    ap.add_argument("--pristine", action="store_true")
    ap.add_argument("--budget-key", default="baselines")
    ap.add_argument("--stub", action="store_true", help="dry run with StubEpisodeRunner + InMemoryStore")
    ap.add_argument("--variant", default=None, help="single plan: only tasks of this variant")
    ap.add_argument("--arm", default="baseline", help="single plan: arm label stored on episodes")
    ap.add_argument("--limit", type=int, default=0, help="single plan: first N tasks of the split (0 = all)")
    ap.add_argument("--seed", type=int, default=0, help="single plan: seed label (replication runs)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    if args.plan == "e1":
        from baselines.selection import run_e1

        out = asyncio.run(run_e1(candidates_json=args.candidates_json, store_path=args.store, stub=args.stub,
                                 concurrency=args.concurrency or E1_CONCURRENCY, deadline=args.deadline,
                                 genome_path=args.genome))
        print(json.dumps(out, indent=2))
        return 0

    runner, store = make_stub_parts() if args.stub else (make_real_runner(), make_real_store(args.store))
    genome = load_genome(args.genome)
    deadline = parse_deadline(args.deadline)
    if args.plan == "e4":
        cands = _load_candidates(args)
        items = plan_e4(cands)
        s = asyncio.run(run_plan(items, runner=runner, store=store, budget_key="baselines", arm="baseline",
                                 concurrency=args.concurrency or E4_CONCURRENCY, deadline=deadline,
                                 genome=genome))
    else:
        if not (args.model and args.split):
            ap.error("--plan single needs --model and --split")
        s = asyncio.run(run_baseline(args.model, args.split, args.mode, runner, store,
                                     args.concurrency or E4_CONCURRENCY, args.pristine, args.budget_key,
                                     deadline=deadline, genome=genome, arm=args.arm, seeds=(args.seed,),
                                     list_tasks=lambda sp: _single_tasks(sp, args.variant, args.limit)))
    print(json.dumps(s.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
