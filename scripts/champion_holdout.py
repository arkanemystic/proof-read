"""Run an arm's final champion on both holdouts in enforce mode (Phase IV tail).

usage: uv run python -m scripts.champion_holdout --arm A --run-id RID [--db data/proofread.sqlite]
       [--concurrency 4] [--deadline ISO] [--model SLUG]

Episodes are stored in collection "episodes" with candidate_id="final_champion" (D-008) under the key
"{run_id}:final_champion:{task_id}:{P|W}". Resumable: stored episodes without error are skipped.
Capability (pristine) and cheat (workspace) items are interleaved so a deadline cut stays balanced.
"""

import argparse
import asyncio
import json
import time
from datetime import datetime

from proofread.agent.episode import make_episode_runner
from proofread.contracts import Genome
from proofread.evolve.orchestrator import real_task_loader
from proofread.store.sqlite_store import SqliteEventLog, SqliteStore

FINAL = "final_champion"


def champion_genome(store: SqliteStore, log: SqliteEventLog, run_id: str) -> tuple[str, Genome, str]:
    """Latest champion of a run: arm.finished, else the last gen.done, else the initial champion."""
    init = log.get_by_key(f"{run_id}:init")
    if init is None:
        raise SystemExit(f"run {run_id} not found")
    champ = init.payload["champion"]
    for g in range(100):
        ev = log.get_by_key(f"{run_id}:g{g}:done")
        if ev is not None:
            champ = ev.payload["champion"]
    fin = log.get_by_key(f"{run_id}:finished")
    if fin is not None:
        champ = fin.payload["champion"]
    doc = store.get("harness_versions", champ)
    return champ, Genome.model_validate(doc["genome"]), init.payload["config"]["agent_model"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=["A", "C"])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--db", default="data/proofread.sqlite")
    ap.add_argument("--model", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--deadline", default=None)
    ap.add_argument("--limit", type=int, default=0, help="max tasks per holdout (0 = all)")
    a = ap.parse_args()
    deadline = datetime.fromisoformat(a.deadline.replace("Z", "+00:00")).timestamp() if a.deadline else None

    store = SqliteStore(a.db)
    vid, genome, arm_model = champion_genome(store, SqliteEventLog(a.db), a.run_id)
    model = a.model or arm_model
    cap = real_task_loader("capability_holdout")
    cheat = real_task_loader("cheat_holdout")
    if a.limit:
        cap, cheat = cap[:a.limit], cheat[:a.limit]
    items = []
    for i in range(max(len(cap), len(cheat))):
        if i < len(cheat):
            items.append((cheat[i], False))
        if i < len(cap):
            items.append((cap[i], True))
    print(json.dumps({"arm": a.arm, "champion": vid, "genome_hash": genome.content_hash(), "model": model,
                      "items": len(items)}), flush=True)

    runner = make_episode_runner()
    sem = asyncio.Semaphore(a.concurrency)
    stop = {"flag": False}
    budget_key = f"arm_{a.arm}"

    async def one(task, pristine):
        key = f"{a.run_id}:{FINAL}:{task.id}:{'P' if pristine else 'W'}"
        old = store.get("episodes", key)
        if old and not old.get("error"):
            return
        async with sem:
            if stop["flag"] or (deadline and time.time() > deadline):
                return
            r = await runner(task, genome, mode="enforce", model=model, arm=a.arm, budget_key=budget_key,
                             pristine=pristine, generation=-1, candidate_id=FINAL)
            doc = r.model_dump(mode="json") | {"run_id": a.run_id, "champion_version": vid,
                                               "split": "capability_holdout" if pristine else "cheat_holdout"}
            store.put("episodes", key, doc)
            if r.error and "BudgetExceeded" in r.error:
                stop["flag"] = True
            print(json.dumps({"task": task.id, "variant": r.variant, "ws": r.passed_workspace,
                              "pristine": r.passed_pristine, "violations": r.violations, "mech": r.mechanism,
                              "cost": r.cost_usd, "error": (r.error or "")[:120]}), flush=True)

    await asyncio.gather(*[one(t, p) for t, p in items])
    print("done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
