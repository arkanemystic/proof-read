"""Phase III smoke: 3 original + 3 impossible tasks, observe and enforce, one real model."""

import asyncio
import json
import sys

from proofread.agent.episode import make_episode_runner
from proofread.benchmarks.impossiblebench import list_tasks, load_task
from proofread.genome.schema import default_genome
from proofread.store.sqlite_store import SqliteStore

MODEL = sys.argv[1] if len(sys.argv) > 1 else "anthropic/claude-sonnet-5"


async def main():
    runner = make_episode_runner()
    store = SqliteStore("data/proofread.sqlite")
    genome = default_genome()
    ids = list_tasks("training")[:3] + list_tasks("selection_cheat")[:3]
    jobs = [(load_task(t), m) for t in ids for m in ("observe", "enforce")]
    res = await asyncio.gather(*[
        runner(t, genome, mode=m, model=MODEL, arm="smoke", budget_key="smoke", pristine=not t.impossible)
        for t, m in jobs])
    for r in res:
        store.put("episodes", r.episode_id, r.model_dump(mode="json"))
        print(json.dumps({k: getattr(r, k) for k in ("task_id", "mode", "passed_workspace", "passed_pristine",
                                                     "violations", "mechanism", "turns", "cost_usd", "error")}))
    print("total cost", round(sum(r.cost_usd for r in res), 3))


asyncio.run(main())
